#!/usr/bin/env python3
"""Analyze llm-proxy JSONL logs to find what's causing cache inefficiency.

Reads a captured request log and produces three reports:

  1. Size profile per turn — system / tools / messages bytes. Tells us
     where bytes are concentrated.
  2. Cross-turn drift — for each pair of consecutive turns, hashes
     every section (system, tools, each prior message position) and
     surfaces which mutated. Mutations in historic positions, not
     just appends, are cache killers — that was reasoning_content's
     signature; whatever's left after we stripped it shows up here.
  3. Cache-miss heatmap — top-N slowest turns with their drift
     findings, so we can see which mutation type correlates with
     multi-thousand-token reprocesses upstream.

Input: JSONL produced by llm-proxy's request logger (one row per
chat-completions request). Fields used: timestamp, n_messages,
n_tools, messages, tools, params, prompt_char_count, ttft_ms,
upstream_lag_ms.

Usage:
    ./analyze-prompts.py /tmp/proxy.jsonl
    ./analyze-prompts.py /tmp/proxy.jsonl --top 20
    ./analyze-prompts.py /tmp/proxy.jsonl --section-detail
    ./analyze-prompts.py /tmp/proxy.jsonl --diff-turn 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def stable_hash(obj: Any) -> str:
    """Deterministic SHA over JSON-encodable objects. sort_keys is the
    point — without it, dict serialization order varies and drift would
    look fake. 12 hex chars is plenty for human-readable diffing."""
    blob = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def load_jsonl(path: Path) -> list[dict]:
    """One row per line. Skip blank lines and lines that don't parse —
    rotated logs sometimes carry partial trailing rows."""
    rows: list[dict] = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"warn: line {n}: {exc}", file=sys.stderr)
    return rows


def section_sizes(entry: dict) -> dict[str, int]:
    """Char-count breakdown of one request body. Mirrors what
    llm-proxy itself records for `prompt_char_count` but per-section
    so we can see where the bytes live."""
    msgs = entry.get("messages") or []
    tools = entry.get("tools") or []

    system_bytes = 0
    msg_bytes = 0
    if msgs:
        m0 = msgs[0]
        if (m0.get("role") == "system"):
            system_bytes = len(json.dumps(m0, default=str))
            msg_bytes = sum(len(json.dumps(m, default=str)) for m in msgs[1:])
        else:
            msg_bytes = sum(len(json.dumps(m, default=str)) for m in msgs)

    tools_bytes = sum(len(json.dumps(t, default=str)) for t in tools)
    return {
        "system": system_bytes,
        "tools": tools_bytes,
        "messages": msg_bytes,
        "total": system_bytes + tools_bytes + msg_bytes,
    }


def section_hashes(entry: dict) -> dict[str, str]:
    """Stable hashes of each section. The `messages_at` map keys each
    message position to its content hash so we can spot when an OLD
    position mutates between turns (the cache-killer signature)."""
    msgs = entry.get("messages") or []
    tools = entry.get("tools") or []

    out: dict[str, str] = {}
    if msgs and msgs[0].get("role") == "system":
        out["system"] = stable_hash(msgs[0])
        body = msgs[1:]
    else:
        out["system"] = stable_hash(None)
        body = msgs
    out["tools"] = stable_hash(tools)
    out["messages_count"] = str(len(body))
    for i, m in enumerate(body):
        out[f"msg[{i}]"] = stable_hash(m)
    return out


def report_size_profile(rows: list[dict]) -> None:
    """Per-turn size table — what's in the prompt, broken down."""
    print("\n=== size profile (bytes per section) ===")
    print(f"{'#':>3}  {'time':>12}  {'msgs':>4}  {'tools':>4}  "
          f"{'sys':>7}  {'tools':>7}  {'msgs':>9}  {'total':>9}  "
          f"{'ttft':>5}  {'upstr':>5}")
    for n, e in enumerate(rows):
        ts = (e.get("timestamp", "") or "")[11:23]
        sizes = section_sizes(e)
        ttft = e.get("ttft_ms")
        upstr = e.get("upstream_lag_ms")
        print(f"{n:>3}  {ts}  "
              f"{e.get('n_messages', 0):>4}  {e.get('n_tools', 0):>4}  "
              f"{sizes['system']:>7}  {sizes['tools']:>7}  "
              f"{sizes['messages']:>9}  {sizes['total']:>9}  "
              f"{str(ttft) if ttft is not None else '-':>5}  "
              f"{str(upstr) if upstr is not None else '-':>5}")


def report_drift(rows: list[dict]) -> dict[int, list[str]]:
    """Cross-turn drift detection. Returns {turn_index → list_of_drift_labels}
    so the caller can correlate with timing. Prints a concise table."""
    print("\n=== drift between consecutive turns ===")
    print(f"{'#':>3}  {'time':>12}  {'system':>6}  {'tools':>6}  "
          f"{'msgs Δ':>6}  drift detail")
    drift_by_turn: dict[int, list[str]] = {}
    prev: dict[str, str] | None = None
    for n, e in enumerate(rows):
        cur = section_hashes(e)
        ts = (e.get("timestamp", "") or "")[11:23]
        labels: list[str] = []
        if prev is not None:
            if cur["system"] != prev["system"]:
                labels.append("system")
            if cur["tools"] != prev["tools"]:
                labels.append("tools")
            # historic-message mutations: compare positions present in BOTH
            common = min(int(cur["messages_count"]), int(prev["messages_count"]))
            mutated_positions: list[int] = []
            for i in range(common):
                k = f"msg[{i}]"
                if cur.get(k) != prev.get(k):
                    mutated_positions.append(i)
            if mutated_positions:
                labels.append(f"hist:{mutated_positions[:5]}"
                              + ("…" if len(mutated_positions) > 5 else ""))
        drift_by_turn[n] = labels

        prev_count = int(prev["messages_count"]) if prev else 0
        delta = int(cur["messages_count"]) - prev_count
        sys_diff = "Δ" if (prev and cur["system"] != prev["system"]) else "ok"
        tools_diff = "Δ" if (prev and cur["tools"] != prev["tools"]) else "ok"
        print(f"{n:>3}  {ts}  "
              f"{sys_diff:>6}  {tools_diff:>6}  {delta:>+6d}  "
              f"{', '.join(labels) if labels else '(append-only)'}")
        prev = cur
    return drift_by_turn


def report_slow_turns(
    rows: list[dict],
    drift_by_turn: dict[int, list[str]],
    top_n: int,
) -> None:
    """Slowest N turns by upstream_lag_ms, with their drift findings. The
    point is to see whether slow turns correlate with hist-message
    mutations vs system/tools drift vs neither (raw eval cost)."""
    print(f"\n=== top {top_n} slow turns (by upstream_lag_ms) ===")
    print(f"{'#':>3}  {'time':>12}  {'upstr':>6}  {'ttft':>6}  "
          f"{'chars':>7}  drift")
    indexed = list(enumerate(rows))
    indexed.sort(
        key=lambda p: (p[1].get("upstream_lag_ms") or 0),
        reverse=True,
    )
    for n, e in indexed[:top_n]:
        ts = (e.get("timestamp", "") or "")[11:23]
        upstr = e.get("upstream_lag_ms") or 0
        ttft = e.get("ttft_ms") or 0
        chars = e.get("prompt_char_count") or 0
        labels = drift_by_turn.get(n, [])
        print(f"{n:>3}  {ts}  {upstr:>6}  {ttft:>6}  {chars:>7}  "
              f"{', '.join(labels) if labels else '(append-only)'}")


def report_section_detail(rows: list[dict], turn_idx: int) -> None:
    """Full content dump of one turn's system + first message of tools.
    For when drift report points at a specific turn and we need to see
    what's actually different."""
    if not (0 <= turn_idx < len(rows)):
        print(f"turn {turn_idx} out of range (0–{len(rows)-1})", file=sys.stderr)
        return
    e = rows[turn_idx]
    msgs = e.get("messages") or []
    tools = e.get("tools") or []
    print(f"\n=== turn {turn_idx} detail ===")
    print(f"timestamp: {e.get('timestamp')}")
    print(f"n_messages: {e.get('n_messages')}, n_tools: {e.get('n_tools')}")
    print(f"prompt_char_count: {e.get('prompt_char_count')}")
    print(f"upstream_lag_ms: {e.get('upstream_lag_ms')}")
    print()
    if msgs and msgs[0].get("role") == "system":
        print("--- system prompt (first 800 chars) ---")
        print(json.dumps(msgs[0], default=str)[:800])
        print(f"... ({len(json.dumps(msgs[0], default=str))} chars total)")
    print()
    print(f"--- tools array ({len(tools)} tools) ---")
    if tools:
        names = [(t.get("function") or {}).get("name", "?") for t in tools]
        print(", ".join(names))


def diff_turns(rows: list[dict], a_idx: int, b_idx: int) -> None:
    """Show what's different between two turns. Useful when the drift
    report flags a slow turn and we want the actual changing bytes."""
    if not (0 <= a_idx < len(rows) and 0 <= b_idx < len(rows)):
        print(f"turn index out of range", file=sys.stderr)
        return
    a, b = rows[a_idx], rows[b_idx]
    a_msgs = a.get("messages") or []
    b_msgs = b.get("messages") or []
    a_tools = a.get("tools") or []
    b_tools = b.get("tools") or []
    print(f"\n=== diff turn {a_idx} → {b_idx} ===")
    if a_msgs and b_msgs and a_msgs[0].get("role") == "system" and b_msgs[0].get("role") == "system":
        if a_msgs[0] != b_msgs[0]:
            print("system prompt differs.")
            sa, sb = json.dumps(a_msgs[0], default=str), json.dumps(b_msgs[0], default=str)
            for i in range(min(len(sa), len(sb))):
                if sa[i] != sb[i]:
                    start = max(0, i - 40)
                    print(f"first divergence at char {i}:")
                    print(f"  a: ...{sa[start:i+80]}...")
                    print(f"  b: ...{sb[start:i+80]}...")
                    break
        else:
            print("system prompt: identical")
    if stable_hash(a_tools) != stable_hash(b_tools):
        print(f"tools differ. count a={len(a_tools)} b={len(b_tools)}")
        a_names = {(t.get("function") or {}).get("name") for t in a_tools}
        b_names = {(t.get("function") or {}).get("name") for t in b_tools}
        added = b_names - a_names
        removed = a_names - b_names
        if added: print(f"  added: {sorted(added)}")
        if removed: print(f"  removed: {sorted(removed)}")
    else:
        print("tools: identical")

    common = min(len(a_msgs), len(b_msgs))
    mutated: list[int] = []
    for i in range(common):
        if stable_hash(a_msgs[i]) != stable_hash(b_msgs[i]):
            mutated.append(i)
    if mutated:
        print(f"historic messages differ at positions: {mutated}")
        # Surface the first one
        i = mutated[0]
        print(f"\nposition {i} (role={a_msgs[i].get('role')} a vs role={b_msgs[i].get('role')} b):")
        print(f"  a keys: {sorted(a_msgs[i].keys())}")
        print(f"  b keys: {sorted(b_msgs[i].keys())}")
        for key in sorted(set(a_msgs[i].keys()) | set(b_msgs[i].keys())):
            va, vb = a_msgs[i].get(key), b_msgs[i].get(key)
            if va != vb:
                print(f"  '{key}' differs (a len={len(json.dumps(va, default=str))}, "
                      f"b len={len(json.dumps(vb, default=str))})")
    else:
        print("historic messages: identical (clean append)")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("path", type=Path, help="JSONL log path")
    parser.add_argument("--top", type=int, default=10, help="slow-turn top-N")
    parser.add_argument("--section-detail", type=int, metavar="TURN",
                        help="dump full system/tools content for this turn index")
    parser.add_argument("--diff", nargs=2, type=int, metavar=("A", "B"),
                        help="diff two turn indices in detail")
    args = parser.parse_args(argv)

    rows = load_jsonl(args.path)
    if not rows:
        print("(no rows)")
        return 1
    print(f"loaded {len(rows)} request(s) from {args.path}")

    report_size_profile(rows)
    drift = report_drift(rows)
    report_slow_turns(rows, drift, args.top)

    if args.section_detail is not None:
        report_section_detail(rows, args.section_detail)
    if args.diff:
        diff_turns(rows, args.diff[0], args.diff[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

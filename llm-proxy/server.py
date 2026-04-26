"""Transparent OpenAI-compatible proxy with full request logging.

Sits between an agent framework (OpenClaw, Cline, etc.) and a downstream
inference server (llama.cpp, vLLM, ...) and:

  - passes through every request unchanged (preserving SSE streaming)
  - logs the full request body to JSONL on a PVC for offline analysis

The point is visibility: when a turn feels slow or the model behaves
oddly, we can replay/diff the exact prompts the agent sent. It's also
the natural seam for swapping between agent frameworks — the proxy
defines a stable contract regardless of what's upstream or downstream.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

UPSTREAM_URL = os.environ.get(
    "UPSTREAM_URL",
    "http://llama-chat.llama-cpp.svc.cluster.local:8080",
)
LOG_DIR = Path(os.environ.get("LOG_DIR", "/var/log/llm-proxy"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _log_path() -> Path:
    """JSONL file rotates daily by UTC date — easy to grep, cheap to retain."""
    return LOG_DIR / f"requests-{datetime.now(UTC).strftime('%Y-%m-%d')}.jsonl"


def _log_event(event: dict) -> None:
    """Append-only JSONL write. Tiny rows; buffer flushing is the OS's job."""
    with _log_path().open("a") as f:
        f.write(json.dumps(event, default=str) + "\n")


def _strip_reasoning_content(messages: list) -> int:
    """Remove `reasoning_content` from assistant messages, in place.

    Thinking-model servers (Qwen3, DeepSeek, ...) emit a `reasoning_content`
    field carrying the model's chain-of-thought. Clients like OpenClaw store
    it and replay it in subsequent requests. The Qwen chat template then
    decides per-render whether to wrap each prior message with `<think>`
    blocks based on `last_query_index` — so the same message position
    serializes differently turn-to-turn, blowing llama.cpp's prompt cache.

    Stripping it before forwarding makes the rendered prefix immutable
    across turns. The current turn still generates fresh thinking in its
    response (which the client/UI displays); we only suppress *replay* of
    prior turns' thinking.
    """
    stripped = 0
    for m in messages:
        if m.get("role") == "assistant" and "reasoning_content" in m:
            del m["reasoning_content"]
            stripped += 1
    return stripped


def _summarize_request(req: dict) -> dict:
    """Pull interesting bits out of a parsed OpenAI chat-completions body.
    We log the whole thing — messages, tools, params — so future analysis
    can diff consecutive turns to find what's changing in the prompt
    prefix and triggering llama.cpp cache misses.
    """
    msgs = req.get("messages") or []
    tools = req.get("tools") or []
    other_params = {k: v for k, v in req.items() if k not in {"messages", "tools"}}
    return {
        "model": req.get("model"),
        "stream": bool(req.get("stream")),
        "n_messages": len(msgs),
        "n_tools": len(tools),
        "messages": msgs,
        "tools": tools,
        "params": other_params,
        # Raw byte-cost proxy. A real tokenizer would be more accurate but
        # this lets us do diff/trend analysis without dragging in a model.
        "prompt_char_count": (
            sum(len(json.dumps(m, default=str)) for m in msgs)
            + sum(len(json.dumps(t, default=str)) for t in tools)
        ),
    }


# Persistent client lets us reuse TCP connections to upstream. Timeout=None
# because long-prompt eval can take minutes; if it gets pathological we'd
# rather hold the connection open than retry-storm the upstream.
client = httpx.AsyncClient(
    timeout=httpx.Timeout(None, connect=10.0),
    base_url=UPSTREAM_URL,
)

app = FastAPI()


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def proxy(request: Request, path: str) -> Response:
    body = await request.body()
    # Strip hop-by-hop / per-request headers — let httpx set Host and the
    # actual Content-Length for the upstream connection.
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length", "transfer-encoding"}
    }

    request_id = str(uuid.uuid4())
    start = time.monotonic()

    log_base: dict = {
        "request_id": request_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "method": request.method,
        "path": f"/v1/{path}",
    }

    forward_body = body
    if body and request.headers.get("content-type", "").startswith("application/json"):
        try:
            parsed = json.loads(body)
            log_base.update(_summarize_request(parsed))
            # Mutate request before forwarding: drop replayed reasoning_content
            # so the upstream sees a stable rendered prefix turn-to-turn.
            # Only meaningful for chat completions; harmless elsewhere.
            if isinstance(parsed.get("messages"), list):
                stripped = _strip_reasoning_content(parsed["messages"])
                if stripped:
                    log_base["reasoning_stripped"] = stripped
                    forward_body = json.dumps(parsed).encode()
        except json.JSONDecodeError:
            log_base["body_raw"] = body[:1024].decode(errors="replace")

    is_streaming = bool(log_base.get("stream", False))

    upstream_req = client.build_request(
        request.method,
        f"/v1/{path}",
        headers=headers,
        content=forward_body,
        params=dict(request.query_params),
    )

    # Latency budget breakdown:
    #   forward_lag_ms = receive → upstream send  (proxy's own overhead:
    #                    JSON parse, reasoning strip, reserialize, build_request)
    #   upstream_lag_ms = upstream send → first byte from llama-cpp
    #                     (queue + prompt eval + first-token gen)
    #   ttft_ms = receive → first byte (sum of the two)
    # Splitting them lets us tell "proxy is slow" from "llama is slow"
    # from "OpenClaw was slow before calling us at all".
    t_send = time.monotonic()

    if is_streaming:
        # SSE pass-through. Capture time-to-first-token (TTFT) — that's the
        # number that actually matters for "agent feels slow." We don't
        # parse SSE chunks; the upstream's own logs have the token-level
        # detail.
        async def stream_response():
            ttft_ms: int | None = None
            upstream_lag_ms: int | None = None
            chunk_count = 0
            try:
                resp = await client.send(upstream_req, stream=True)
                async for chunk in resp.aiter_raw():
                    if chunk and ttft_ms is None:
                        now = time.monotonic()
                        ttft_ms = int((now - start) * 1000)
                        upstream_lag_ms = int((now - t_send) * 1000)
                    chunk_count += 1
                    yield chunk
                await resp.aclose()
            finally:
                _log_event({
                    **log_base,
                    "forward_lag_ms": int((t_send - start) * 1000),
                    "upstream_lag_ms": upstream_lag_ms,
                    "ttft_ms": ttft_ms,
                    "total_ms": int((time.monotonic() - start) * 1000),
                    "chunk_count": chunk_count,
                })

        return StreamingResponse(
            stream_response(),
            media_type="text/event-stream",
        )

    # Non-streaming JSON response. Read fully so we can capture the
    # `usage` block (prompt_tokens, completion_tokens) which the upstream
    # only emits at the end. For SSE responses we'd have to parse chunks
    # to recover the same numbers; not worth the complexity right now.
    resp = await client.send(upstream_req)
    upstream_lag_ms = int((time.monotonic() - t_send) * 1000)
    total_ms = int((time.monotonic() - start) * 1000)
    log_record: dict = {
        **log_base,
        "forward_lag_ms": int((t_send - start) * 1000),
        "upstream_lag_ms": upstream_lag_ms,
        "total_ms": total_ms,
        "status": resp.status_code,
    }
    try:
        response_json = resp.json()
        usage = response_json.get("usage") or {}
        log_record["prompt_tokens"] = usage.get("prompt_tokens")
        log_record["completion_tokens"] = usage.get("completion_tokens")
        choices = response_json.get("choices") or []
        if choices:
            log_record["finish_reason"] = choices[0].get("finish_reason")
    except (json.JSONDecodeError, ValueError):
        pass
    _log_event(log_record)

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in {"content-encoding", "transfer-encoding"}
        },
        media_type=resp.headers.get("content-type"),
    )


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        log_level="info",
        # Disable access logs — we have our own structured JSONL logging.
        # The default access log would double-print every request.
        access_log=False,
    )

#!/usr/bin/env python3
"""Cluster manager for the GPU workstation homelab.

Single CLI wrapping the full lifecycle:
  init-fork    one-time: rewrite repoURL placeholder in Argo manifests
  prep-node    per-node: apt upgrade, hostname, NVIDIA if GPU
  bootstrap    whole-cluster: k3s + Argo CD
  pull-model   runtime: pull an Ollama model
  status       runtime: cluster/node/pod summary

Runs from your workstation. Shells out to ansible-playbook for prep/bootstrap.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:
    import typer
    from rich.console import Console
except ImportError:
    print("Missing dependencies. Install with:")
    print("  pip install typer rich")
    sys.exit(1)

REPO_DIR = Path(__file__).resolve().parent.parent
ANSIBLE_DIR = REPO_DIR / "ansible"
CLUSTERS_DIR = REPO_DIR / "clusters"

console = Console()
app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=True)


def _run(cmd: list[str], cwd: Path | None = None) -> int:
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    return subprocess.run(cmd, cwd=cwd).returncode


def _require_ansible() -> None:
    if subprocess.run(["which", "ansible-playbook"], capture_output=True).returncode != 0:
        console.print("[red]ansible-playbook not found.[/red] Install: brew install ansible")
        raise typer.Exit(1)


def _require_inventory() -> None:
    if not (ANSIBLE_DIR / "inventory.ini").exists():
        console.print("[red]ansible/inventory.ini not found.[/red]")
        console.print("  cp ansible/inventory.ini.example ansible/inventory.ini")
        raise typer.Exit(1)


def _require_fork_initialized() -> None:
    result = subprocess.run(
        ["grep", "-rq", "repoURL: REPO_URL", str(CLUSTERS_DIR)],
        capture_output=True,
    )
    if result.returncode == 0:
        console.print("[red]Argo manifests still contain REPO_URL placeholder.[/red]")
        console.print("  ./scripts/cluster_manager.py init-fork")
        raise typer.Exit(1)


@app.command("init-fork")
def init_fork(
    repo_url: str = typer.Argument(
        None, help="Explicit repo URL. Defaults to `git remote get-url origin`."
    ),
) -> None:
    """One-time: rewrite the REPO_URL placeholder in Argo Application manifests."""
    if repo_url is None:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, cwd=REPO_DIR,
        )
        if result.returncode != 0 or not result.stdout.strip():
            console.print("[red]Could not auto-detect git remote. Pass URL explicitly.[/red]")
            raise typer.Exit(1)
        repo_url = result.stdout.strip()

    # Normalize SSH → HTTPS, strip trailing .git
    if repo_url.startswith("git@github.com:"):
        repo_url = "https://github.com/" + repo_url[len("git@github.com:"):]
    if repo_url.endswith(".git"):
        repo_url = repo_url[:-4]

    console.print(f"Setting repoURL to: [cyan]{repo_url}[/cyan]")

    replaced = 0
    for yaml_path in CLUSTERS_DIR.rglob("*.yaml"):
        text = yaml_path.read_text()
        new_text = text.replace("repoURL: REPO_URL", f"repoURL: {repo_url}")
        if new_text != text:
            yaml_path.write_text(new_text)
            replaced += 1
            console.print(f"  [green]✓[/green] {yaml_path.relative_to(REPO_DIR)}")

    if replaced == 0:
        console.print("[yellow]No placeholder found. Already initialized?[/yellow]")
    else:
        console.print(f"[green]Updated {replaced} file(s).[/green] Commit + push to your fork.")


@app.command("prep-node")
def prep_node(
    host: str = typer.Argument(..., help="Inventory hostname to prep (e.g. k3s-gpu)."),
    extra: list[str] = typer.Argument(None, help="Extra args passed through to ansible-playbook."),
) -> None:
    """Per-node prep: apt upgrade, utilities, hostname, NVIDIA on GPU nodes.

    The host must already be in ansible/inventory.ini. Idempotent.
    """
    _require_ansible()
    _require_inventory()
    cmd = [
        "ansible-playbook",
        "-i", "inventory.ini",
        "prep.yml",
        "--limit", host,
        "--ask-become-pass",
    ]
    if extra:
        cmd.extend(extra)
    raise typer.Exit(_run(cmd, cwd=ANSIBLE_DIR))


@app.command("bootstrap")
def bootstrap(
    extra: list[str] = typer.Argument(None, help="Extra args passed through to ansible-playbook."),
) -> None:
    """Cluster bootstrap: install k3s on all nodes and Argo CD on control."""
    _require_ansible()
    _require_inventory()
    _require_fork_initialized()
    cmd = [
        "ansible-playbook",
        "-i", "inventory.ini",
        "cluster.yml",
        "--ask-become-pass",
    ]
    if extra:
        cmd.extend(extra)
    raise typer.Exit(_run(cmd, cwd=ANSIBLE_DIR))


@app.command("pull-model")
def pull_model(
    model: str = typer.Argument(..., help="Model tag, e.g. llama3.3:70b"),
    host: str = typer.Option(
        "localhost:31434",
        "--host", "-h",
        help="Ollama NodePort endpoint (any cluster node:31434).",
    ),
) -> None:
    """Pull a model into the running Ollama server."""
    url = f"http://{host}/api/pull"
    console.print(f"Pulling [cyan]{model}[/cyan] from [dim]{url}[/dim]")
    rc = subprocess.run(
        ["curl", "-fsSN", url, "-d", f'{{"name":"{model}"}}']
    ).returncode
    if rc == 0:
        console.print("\n[green]Done.[/green]")
    raise typer.Exit(rc)


@app.command("status")
def status(
    control: str = typer.Option(
        None, "--control", "-c",
        help="Control node host. Defaults to the first host in [control] in inventory.",
    ),
) -> None:
    """Quick cluster snapshot: nodes + pods across all namespaces."""
    if control is None:
        inv = ANSIBLE_DIR / "inventory.ini"
        if not inv.exists():
            console.print("[red]Pass --control or create inventory.ini[/red]")
            raise typer.Exit(1)
        host = None
        in_control = False
        for line in inv.read_text().splitlines():
            line = line.strip()
            if line.startswith("["):
                in_control = line == "[control]"
                continue
            if in_control and line and not line.startswith("#"):
                host = line.split()[0]
                break
        if not host:
            console.print("[red]No [control] host found in inventory.[/red]")
            raise typer.Exit(1)
        control = host

    console.print(f"[dim]via {control}[/dim]\n")
    for args in (["get", "nodes", "-o", "wide"], ["get", "pods", "-A"]):
        _run(["ssh", control, "sudo", "k3s", "kubectl", *args])
        console.print()


if __name__ == "__main__":
    app()

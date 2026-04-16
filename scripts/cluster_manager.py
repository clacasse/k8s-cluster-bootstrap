#!/usr/bin/env python3
"""Cluster manager for k8s-cluster-bootstrap.

Single CLI wrapping the full lifecycle:
  init-fork       one-time: rewrite REPO_URL + APPS_DOMAIN placeholders
  prep-node       per-node: add to inventory, apt upgrade, hostname, NVIDIA if GPU
  bootstrap       whole-cluster: k3s + Argo CD
  pull-model      runtime: pull an Ollama model
  status          runtime: cluster/node/pod summary
  sync-upstream   pull upstream changes into your instance repo

Runs from your workstation. Shells out to ansible-playbook for prep/bootstrap.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

try:
    import typer
    from rich.console import Console
except ImportError:
    print("Missing dependencies. Install with:")
    print("  pip install -r requirements.txt")
    sys.exit(1)

REPO_DIR = Path(__file__).resolve().parent.parent
ANSIBLE_DIR = REPO_DIR / "ansible"
CLUSTERS_DIR = REPO_DIR / "clusters"

DEFAULT_APPS_DOMAIN = "apps.localdomain"
VALID_ROLES = ("control", "worker", "gpu")

INVENTORY_SKELETON = """\
[control]

[workers]

[gpu]

[agents:children]
workers
gpu

[all:vars]
ansible_user={user}
ansible_python_interpreter=/usr/bin/python3
"""

console = Console()
app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    return subprocess.run(cmd, cwd=cwd, capture_output=capture, text=capture)


def _require_ansible() -> None:
    if subprocess.run(["which", "ansible-playbook"], capture_output=True).returncode != 0:
        console.print("[red]ansible-playbook not found.[/red] Install: brew install ansible")
        raise typer.Exit(1)


def _require_inventory() -> None:
    if not (ANSIBLE_DIR / "inventory.ini").exists():
        console.print("[red]ansible/inventory.ini not found.[/red]")
        console.print("  Run prep-node to create it, or: cp ansible/inventory.ini.example ansible/inventory.ini")
        raise typer.Exit(1)


def _require_fork_initialized() -> None:
    for placeholder in ("repoURL: REPO_URL", "APPS_DOMAIN"):
        result = subprocess.run(
            ["grep", "-rq", placeholder, str(CLUSTERS_DIR)],
            capture_output=True,
        )
        if result.returncode == 0:
            console.print(f"[red]Cluster manifests still contain '{placeholder}' placeholder.[/red]")
            console.print("  ./scripts/cluster_manager.py init-fork")
            raise typer.Exit(1)


def _get_repo_url() -> str:
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, cwd=REPO_DIR,
    )
    if result.returncode != 0 or not result.stdout.strip():
        console.print("[red]Could not detect git remote origin.[/red]")
        raise typer.Exit(1)
    url = result.stdout.strip()
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url[len("git@github.com:"):]
    if url.endswith(".git"):
        url = url[:-4]
    return url


def _get_apps_domain() -> str:
    """Detect the current apps domain from already-initialized manifests."""
    for yaml_path in CLUSTERS_DIR.rglob("*.yaml"):
        for line in yaml_path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("- host:") and "." in stripped:
                host = stripped.split("- host:", 1)[1].strip()
                parts = host.split(".", 1)
                if len(parts) == 2 and parts[1] != "APPS_DOMAIN":
                    return parts[1]
    return DEFAULT_APPS_DOMAIN


def _authorize_host_key(host: str) -> None:
    """Add the host's SSH key to known_hosts if not already present."""
    known_hosts = Path.home() / ".ssh" / "known_hosts"
    known_hosts.parent.mkdir(mode=0o700, exist_ok=True)

    result = subprocess.run(
        ["ssh-keyscan", "-H", host],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        console.print(f"[yellow]Could not scan SSH host key for {host}[/yellow]")
        return

    new_keys = [l for l in result.stdout.strip().splitlines() if l and not l.startswith("#")]
    if not new_keys:
        return

    with known_hosts.open("a") as f:
        for key in new_keys:
            f.write(key + "\n")
    console.print(f"  [green]✓[/green] SSH host key authorized for {host}")


def _role_to_group(role: str) -> str:
    """Map a user-facing role name to the inventory group name."""
    return "workers" if role == "worker" else role


def _ensure_inventory(user: str) -> Path:
    """Create inventory.ini from skeleton if it doesn't exist. Returns its path."""
    inv = ANSIBLE_DIR / "inventory.ini"
    if not inv.exists():
        inv.write_text(INVENTORY_SKELETON.format(user=user))
        console.print(f"  [green]✓[/green] Created {inv.relative_to(REPO_DIR)}")
    return inv


def _add_to_inventory(inv: Path, hostname: str, ip: str, role: str, user: str) -> None:
    """Add a host entry to the correct group in inventory.ini. Idempotent."""
    text = inv.read_text()
    entry = f"{hostname} ansible_host={ip}"

    # Check if this host is already present (by hostname or IP)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(hostname + " ") or stripped == hostname:
            console.print(f"  [dim]{hostname} already in inventory[/dim]")
            return
        if f"ansible_host={ip}" in stripped:
            console.print(f"  [yellow]{ip} already in inventory as {stripped.split()[0]}[/yellow]")
            return

    group = _role_to_group(role)
    group_header = f"[{group}]"

    # Update ansible_user if different
    if f"ansible_user={user}" not in text:
        text = re.sub(r"ansible_user=\S+", f"ansible_user={user}", text)

    lines = text.splitlines()
    result = []
    inserted = False
    for i, line in enumerate(lines):
        result.append(line)
        if line.strip() == group_header and not inserted:
            result.append(entry)
            inserted = True

    if not inserted:
        console.print(f"[red]Could not find [{group}] section in inventory.[/red]")
        raise typer.Exit(1)

    inv.write_text("\n".join(result) + "\n")
    console.print(f"  [green]✓[/green] Added {entry} to \\[{group}]")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.command("init-fork")
def init_fork(
    repo_url: str = typer.Argument(
        None, help="Explicit repo URL. Defaults to `git remote get-url origin`."
    ),
    apps_domain: str = typer.Option(
        DEFAULT_APPS_DOMAIN,
        "--apps-domain",
        help="Wildcard DNS domain for app Ingress hostnames. Must match the "
             "*.<domain> A record you created on your router.",
    ),
) -> None:
    """One-time: rewrite REPO_URL and APPS_DOMAIN placeholders in cluster manifests."""
    if repo_url is None:
        repo_url = _get_repo_url()

    if apps_domain == DEFAULT_APPS_DOMAIN:
        apps_domain = typer.prompt(
            "Wildcard DNS domain for app Ingress hostnames (*.X on your router)",
            default=DEFAULT_APPS_DOMAIN,
        )

    console.print(f"Setting repoURL to:     [cyan]{repo_url}[/cyan]")
    console.print(f"Setting apps domain to: [cyan]{apps_domain}[/cyan]")

    replacements = {
        "repoURL: REPO_URL": f"repoURL: {repo_url}",
        "APPS_DOMAIN": apps_domain,
    }

    touched = 0
    for yaml_path in CLUSTERS_DIR.rglob("*.yaml"):
        text = yaml_path.read_text()
        new_text = text
        for old, new in replacements.items():
            new_text = new_text.replace(old, new)
        if new_text != text:
            yaml_path.write_text(new_text)
            touched += 1
            console.print(f"  [green]✓[/green] {yaml_path.relative_to(REPO_DIR)}")

    if touched == 0:
        console.print("[yellow]No placeholders found. Already initialized?[/yellow]")
    else:
        console.print(f"[green]Updated {touched} file(s).[/green] Commit + push.")


@app.command("sync-upstream")
def sync_upstream(
    remote: str = typer.Option(
        "upstream",
        "--remote", "-r",
        help="Name of the upstream git remote.",
    ),
    branch: str = typer.Option(
        "main",
        "--branch", "-b",
        help="Upstream branch to merge.",
    ),
) -> None:
    """Pull upstream changes into your instance repo.

    Fetches from the upstream remote, merges, then re-runs init-fork to
    rewrite any new REPO_URL/APPS_DOMAIN placeholders that arrived with
    the merge. Commits the result if any placeholders were replaced.
    """
    result = subprocess.run(
        ["git", "remote", "get-url", remote],
        capture_output=True, text=True, cwd=REPO_DIR,
    )
    if result.returncode != 0:
        console.print(f"[red]Remote '{remote}' not found.[/red] Add it first:")
        console.print(f"  git remote add {remote} https://github.com/<upstream-owner>/k8s-cluster-bootstrap.git")
        raise typer.Exit(1)

    console.print(f"Fetching [cyan]{remote}/{branch}[/cyan]...")
    rc = _run(["git", "fetch", remote], cwd=REPO_DIR).returncode
    if rc != 0:
        raise typer.Exit(rc)

    console.print(f"Merging [cyan]{remote}/{branch}[/cyan]...")
    result = _run(
        ["git", "merge", f"{remote}/{branch}", "--no-edit", "--no-ff"],
        cwd=REPO_DIR, capture=True,
    )
    if result.returncode != 0:
        console.print(result.stdout)
        console.print(result.stderr)
        if "CONFLICT" in (result.stdout or "") or "CONFLICT" in (result.stderr or ""):
            console.print("\n[yellow]Merge conflicts detected. Resolve them, then run:[/yellow]")
            console.print("  ./scripts/cluster_manager.py init-fork")
            console.print("  git commit")
        raise typer.Exit(result.returncode)

    console.print("[green]Merge successful.[/green]")

    repo_url = _get_repo_url()
    apps_domain = _get_apps_domain()

    console.print(f"\nRe-applying placeholders (repoURL={repo_url}, domain={apps_domain})...")
    replacements = {
        "repoURL: REPO_URL": f"repoURL: {repo_url}",
        "APPS_DOMAIN": apps_domain,
    }

    touched = 0
    for yaml_path in CLUSTERS_DIR.rglob("*.yaml"):
        text = yaml_path.read_text()
        new_text = text
        for old, new in replacements.items():
            new_text = new_text.replace(old, new)
        if new_text != text:
            yaml_path.write_text(new_text)
            touched += 1
            console.print(f"  [green]✓[/green] {yaml_path.relative_to(REPO_DIR)}")

    if touched > 0:
        _run(["git", "add", str(CLUSTERS_DIR)], cwd=REPO_DIR)
        _run(["git", "commit", "-m", "Replace upstream placeholders after sync"], cwd=REPO_DIR)
        console.print(f"[green]Committed {touched} placeholder replacement(s).[/green]")
    else:
        console.print("[dim]No new placeholders to replace.[/dim]")

    console.print("\n[green]Sync complete.[/green] Push when ready: git push")


@app.command("prep-node")
def prep_node(
    ip: str = typer.Argument(..., help="IP address of the node to prepare."),
    hostname: str = typer.Option(
        None, "--hostname", "-n",
        help="Hostname to assign (e.g. k3s-control). Prompted if not provided.",
    ),
    role: str = typer.Option(
        None, "--role", "-r",
        help="Node role: control, worker, or gpu. Prompted if not provided.",
    ),
    user: str = typer.Option(
        "ubuntu", "--user", "-u",
        help="SSH user on the node.",
    ),
    extra: list[str] = typer.Argument(None, help="Extra args passed through to ansible-playbook."),
) -> None:
    """Prep a new node: add to inventory, authorize SSH key, apt upgrade, set hostname.

    Adds the node to ansible/inventory.ini (creating it if needed), authorizes
    the SSH host key, then runs the Ansible prep playbook against it.
    """
    _require_ansible()

    if hostname is None:
        hostname = typer.prompt("Hostname to assign to this node (e.g. k3s-control)")
    if role is None:
        import click
        role = typer.prompt(
            "Node role (control, worker, gpu)",
            type=click.Choice(VALID_ROLES, case_sensitive=False),
        )
    role = role.lower()
    if role not in VALID_ROLES:
        console.print(f"[red]Invalid role '{role}'. Must be one of: {', '.join(VALID_ROLES)}[/red]")
        raise typer.Exit(1)

    console.print(f"\nPrepping [cyan]{ip}[/cyan] as [cyan]{hostname}[/cyan] (role: {role})")

    inv = _ensure_inventory(user)
    _add_to_inventory(inv, hostname, ip, role, user)
    _authorize_host_key(ip)

    cmd = [
        "ansible-playbook",
        "-i", "inventory.ini",
        "prep.yml",
        "--limit", hostname,
        "--ask-become-pass",
    ]
    if extra:
        cmd.extend(extra)
    raise typer.Exit(_run(cmd, cwd=ANSIBLE_DIR).returncode)


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
    raise typer.Exit(_run(cmd, cwd=ANSIBLE_DIR).returncode)


@app.command("pull-model")
def pull_model(
    model: str = typer.Argument(..., help="Model tag, e.g. llama3.3:70b"),
    host: str = typer.Option(
        f"ollama.{DEFAULT_APPS_DOMAIN}",
        "--host", "-h",
        help="Ollama endpoint host[:port]. Defaults to the Ingress hostname.",
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

#!/usr/bin/env python3
"""Cluster manager for k8s-cluster-bootstrap.

Single CLI wrapping the full lifecycle:
  init-fork       one-time: rewrite REPO_URL + APPS_DOMAIN placeholders
  prep-node       per-node: add to inventory, apt upgrade, hostname, NVIDIA if GPU
  bootstrap       whole-cluster: k3s + Argo CD
  setup-secrets   one-time: create TLS cert, OpenClaw token, initial model
  models          runtime: list, pull, set, remove Ollama models
  status          runtime: cluster/node/pod summary
  sync-upstream   pull upstream changes into your instance repo

Runs from your workstation. Shells out to ansible-playbook for prep/bootstrap.
"""

from __future__ import annotations

import base64
import functools
import json
import re
import shlex
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

DEFAULT_APPS_DOMAIN = "apps"
VALID_ROLES = ("control", "worker", "gpu", "storage")

INVENTORY_SKELETON = """\
[control]

[workers]

[gpu]

[storage]

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

def _ssh(control: str, cmd: str, *, capture: bool = False, check: bool = False) -> subprocess.CompletedProcess:
    """Run a shell command on the control node via SSH."""
    return subprocess.run(["ssh", control, cmd], capture_output=capture, text=capture, check=check)


def _kubectl(control: str, *args: str, capture: bool = False, check: bool = False) -> subprocess.CompletedProcess:
    """Run kubectl on the control node via SSH (list args, no shell injection risk)."""
    return subprocess.run(
        ["ssh", control, "sudo", "k3s", "kubectl", *args],
        capture_output=capture, text=capture, check=check,
    )


def _kubectl_exists(control: str, namespace: str, resource_type: str, name: str) -> bool:
    """Check if a k8s resource exists."""
    result = _kubectl(control, "-n", namespace, "get", resource_type, name,
                      "--ignore-not-found", "-o", "name", capture=True)
    return bool(result.stdout.strip())


def _ensure_namespace(control: str, namespace: str) -> None:
    """Create a namespace idempotently."""
    _ssh(control,
        f"sudo k3s kubectl create namespace {_q(namespace)} --dry-run=client -o yaml"
        f" | sudo k3s kubectl apply -f -",
        capture=True,
    )


def _patch_secret(control: str, namespace: str, name: str, data: dict[str, str]) -> None:
    """Patch a k8s Secret with base64-encoded key/value pairs. Use None values to delete keys."""
    encoded = {}
    for k, v in data.items():
        if v is None:
            encoded[k] = None
        else:
            encoded[k] = base64.b64encode(v.encode()).decode()

    import json
    patch = json.dumps({"data": encoded})
    _ssh(control,
        f"sudo k3s kubectl -n {_q(namespace)} patch secret {_q(name)}"
        f" --type merge -p {_q(patch)}"
    )


def _restart_deployment(control: str, namespace: str, name: str) -> None:
    """Restart a deployment via rollout restart."""
    _kubectl(control, "-n", namespace, "rollout", "restart", f"deployment/{name}")


def _q(s: str) -> str:
    """Shell-quote a string for safe interpolation into SSH commands."""
    return shlex.quote(s)


@functools.lru_cache(maxsize=None)
def _get_apps_domain_cached() -> str:
    return _get_apps_domain()


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
    for placeholder in ("repoURL: REPO_URL", "APPS_DOMAIN", "NFS_SERVER"):
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


def _role_to_groups(role: str) -> list[str]:
    """Map a user-facing role name to inventory group name(s)."""
    if role == "worker":
        return ["workers"]
    if role == "storage":
        return ["workers", "storage"]
    return [role]


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

    groups = _role_to_groups(role)

    # Update ansible_user if different
    if f"ansible_user={user}" not in text:
        text = re.sub(r"ansible_user=\S+", f"ansible_user={user}", text)

    lines = text.splitlines()
    result = []
    inserted_groups = set()
    for line in lines:
        result.append(line)
        for group in groups:
            if line.strip() == f"[{group}]" and group not in inserted_groups:
                result.append(entry)
                inserted_groups.add(group)

    missing = set(groups) - inserted_groups
    if missing:
        console.print(f"[red]Could not find sections: {', '.join(f'[{g}]' for g in missing)}[/red]")
        raise typer.Exit(1)

    inv.write_text("\n".join(result) + "\n")
    for group in groups:
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
    """One-time: rewrite REPO_URL, APPS_DOMAIN, and NFS_SERVER placeholders in cluster manifests."""
    if repo_url is None:
        repo_url = _get_repo_url()

    if apps_domain == DEFAULT_APPS_DOMAIN:
        apps_domain = typer.prompt(
            "Wildcard DNS domain for app Ingress hostnames (*.X on your router)",
            default=DEFAULT_APPS_DOMAIN,
        )

    nfs_server = typer.prompt(
        "Storage node hostname (NFS server, or 'none' to skip)",
        default="none",
    )

    console.print(f"Setting repoURL to:     [cyan]{repo_url}[/cyan]")
    console.print(f"Setting apps domain to: [cyan]{apps_domain}[/cyan]")
    if nfs_server != "none":
        console.print(f"Setting NFS server to:  [cyan]{nfs_server}[/cyan]")

    replacements = {
        "repoURL: REPO_URL": f"repoURL: {repo_url}",
        "APPS_DOMAIN": apps_domain,
    }
    if nfs_server != "none":
        replacements["NFS_SERVER"] = nfs_server

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

    # Detect NFS server from existing manifests
    nfs_server = None
    for yaml_path in CLUSTERS_DIR.rglob("*.yaml"):
        for line in yaml_path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("server:") and "NFS_SERVER" not in stripped:
                nfs_server = stripped.split("server:", 1)[1].strip()
                break
        if nfs_server:
            break

    console.print(f"\nRe-applying placeholders (repoURL={repo_url}, domain={apps_domain})...")
    replacements = {
        "repoURL: REPO_URL": f"repoURL: {repo_url}",
        "APPS_DOMAIN": apps_domain,
    }
    if nfs_server:
        replacements["NFS_SERVER"] = nfs_server

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
        None, "--user", "-u",
        help="SSH user on the node. Prompted if not provided.",
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
    if user is None:
        user = typer.prompt("SSH user on the node")
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
        "--become",
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
        "--become",
    ]
    if extra:
        cmd.extend(extra)
    raise typer.Exit(_run(cmd, cwd=ANSIBLE_DIR).returncode)


@app.command("remove-node")
def remove_node(
    hostname: str = typer.Argument(..., help="Hostname of the node to remove (e.g. k3s-storage)."),
    control: str = typer.Option(
        None, "--control", "-c",
        help="Control node host. Auto-detected from inventory if not provided.",
    ),
) -> None:
    """Remove a node from the cluster and inventory.

    Drains the node, deletes it from k8s, attempts to uninstall k3s on
    the node (skipped if unreachable), and removes it from inventory.ini.
    """
    if control is None:
        control = _get_control_host()

    if hostname == control.split()[0]:
        console.print("[red]Cannot remove the control node.[/red]")
        raise typer.Exit(1)

    if not typer.confirm(f"This will remove {hostname} from the cluster and inventory. Continue?"):
        raise typer.Exit(0)

    console.print(f"[dim]via {control}[/dim]\n")

    # Drain (tolerates node being NotReady)
    console.print(f"Draining {hostname}...")
    _kubectl(control, "drain", hostname,
             "--ignore-daemonsets", "--delete-emptydir-data",
             "--force", "--timeout=60s")

    # Delete from k8s
    console.print(f"Deleting node from cluster...")
    _kubectl(control, "delete", "node", hostname)

    # Try to uninstall k3s on the node (may fail if node is down)
    inv = ANSIBLE_DIR / "inventory.ini"
    node_host = None
    if inv.exists():
        for line in inv.read_text().splitlines():
            if line.strip().startswith(hostname):
                parts = line.strip().split()
                for part in parts:
                    if part.startswith("ansible_host="):
                        node_host = part.split("=", 1)[1]
                break

    if node_host:
        console.print(f"Uninstalling k3s on {hostname} ({node_host})...")
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", f"clacasse@{node_host}",
             "sudo /usr/local/bin/k3s-agent-uninstall.sh"],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            console.print(f"  [green]✓[/green] k3s uninstalled on {hostname}")
        else:
            console.print(f"  [yellow]Could not reach {hostname} — k3s not uninstalled (node may be down)[/yellow]")
    else:
        console.print(f"  [yellow]Could not find IP for {hostname} in inventory — skipping k3s uninstall[/yellow]")

    # Remove from inventory
    if inv.exists():
        lines = inv.read_text().splitlines()
        new_lines = [l for l in lines if not l.strip().startswith(hostname)]
        inv.write_text("\n".join(new_lines) + "\n")
        console.print(f"  [green]✓[/green] Removed {hostname} from inventory")

    console.print(f"\n[green]{hostname} removed from the cluster.[/green]")


def _get_control_host() -> str:
    """Read the first host in [control] from inventory."""
    inv = ANSIBLE_DIR / "inventory.ini"
    if not inv.exists():
        console.print("[red]inventory.ini not found. Run prep-node first.[/red]")
        raise typer.Exit(1)
    in_control = False
    for line in inv.read_text().splitlines():
        line = line.strip()
        if line.startswith("["):
            in_control = line == "[control]"
            continue
        if in_control and line and not line.startswith("#"):
            return line.split()[0]
    console.print("[red]No [control] host found in inventory.[/red]")
    raise typer.Exit(1)


@app.command("setup-secrets")
def setup_secrets(
    control: str = typer.Option(
        None, "--control", "-c",
        help="Control node host. Auto-detected from inventory if not provided.",
    ),
) -> None:
    """Create Kubernetes secrets required by cluster apps.

    Generates:
    - Wildcard TLS certificate for *.APPS_DOMAIN (used by Traefik for HTTPS)
    - OpenClaw gateway token

    Run once after bootstrap. Safe to re-run (skips existing secrets).
    """
    import secrets as secrets_mod
    import tempfile

    if control is None:
        control = _get_control_host()

    apps_domain = _get_apps_domain()
    console.print(f"[dim]via {control}[/dim]\n")

    # --- Wildcard TLS cert for *.apps_domain ---
    if _kubectl_exists(control, "kube-system", "secret", "wildcard-apps-tls"):
        console.print(f"[dim]wildcard-apps-tls already exists, skipping.[/dim]")
    else:
        console.print(f"Generating wildcard TLS cert for [cyan]*.{apps_domain}[/cyan]...")
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = Path(tmpdir) / "tls.key"
            cert_path = Path(tmpdir) / "tls.crt"
            subprocess.run([
                "openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
                "-days", "3650",
                "-keyout", str(key_path),
                "-out", str(cert_path),
                "-subj", f"/CN=*.{apps_domain}",
                "-addext", f"subjectAltName=DNS:*.{apps_domain},DNS:{apps_domain}",
            ], check=True, capture_output=True)

            # Copy to control node in a secure temp dir and create secret
            _ssh(control, "mkdir -p -m 700 /tmp/tls-setup", check=True, capture=True)
            subprocess.run(["scp", str(key_path), str(cert_path),
                           f"{control}:/tmp/tls-setup/"], check=True, capture_output=True)
            _ssh(control,
                "sudo k3s kubectl -n kube-system create secret tls wildcard-apps-tls"
                " --cert=/tmp/tls-setup/tls.crt --key=/tmp/tls-setup/tls.key"
                " ; rm -rf /tmp/tls-setup",
                check=True,
            )
        console.print(f"[green]Wildcard TLS cert created in kube-system/wildcard-apps-tls.[/green]")
        console.print(f"[yellow]This is a self-signed cert — your browser will show a warning on first visit.[/yellow]\n")

    # --- OpenClaw gateway token ---
    if _kubectl_exists(control, "openclaw", "secret", "openclaw-secrets"):
        console.print("[dim]openclaw-secrets already exists, skipping.[/dim]")
    else:
        token = secrets_mod.token_urlsafe(32)
        _ensure_namespace(control, "openclaw")
        _kubectl(control, "-n", "openclaw", "create", "secret", "generic", "openclaw-secrets",
                 f"--from-literal=gateway-token={token}")
        console.print(f"\n[green]OpenClaw gateway token created.[/green]")
        console.print(f"[bold]Save this token — you'll need it to log into the OpenClaw web UI:[/bold]")
        console.print(f"\n  [cyan]{token}[/cyan]\n")

    # --- OpenClaw active model ConfigMap ---
    if _kubectl_exists(control, "openclaw", "configmap", "openclaw-model"):
        console.print("[dim]openclaw-model ConfigMap already exists, skipping.[/dim]")
    else:
        model = typer.prompt("Default model for OpenClaw (e.g. gemma4:26b)")
        _kubectl(control, "-n", "openclaw", "create", "configmap", "openclaw-model",
                 f"--from-literal=active-model={model}")
        console.print(f"[green]Active model set to {model}.[/green]")

    # --- OpenClaw config ConfigMap ---
    if _kubectl_exists(control, "openclaw", "configmap", "openclaw-config"):
        console.print("[dim]openclaw-config ConfigMap already exists, skipping.[/dim]")
    else:
        disable_device_auth = typer.confirm(
            "Disable device auth for OpenClaw? (required for reverse proxy access)",
            default=True,
        )
        auth_value = "true" if disable_device_auth else "false"
        _kubectl(control, "-n", "openclaw", "create", "configmap", "openclaw-config",
                 f"--from-literal=disable-device-auth={auth_value}")
        console.print(f"[green]OpenClaw config created (disable-device-auth={auth_value}).[/green]")

    # --- Grafana admin secret ---
    if _kubectl_exists(control, "monitoring", "secret", "grafana-admin"):
        console.print("[dim]grafana-admin secret already exists, skipping.[/dim]")
    else:
        grafana_password = secrets_mod.token_urlsafe(24)
        _ensure_namespace(control, "monitoring")
        _kubectl(control, "-n", "monitoring", "create", "secret", "generic", "grafana-admin",
                 f"--from-literal=admin-user=admin",
                 f"--from-literal=admin-password={grafana_password}")
        console.print(f"\n[green]Grafana admin password created.[/green]")
        console.print(f"[bold]Save this — login at https://grafana.apps with user 'admin':[/bold]")
        console.print(f"\n  [cyan]{grafana_password}[/cyan]\n")


@app.command("setup-slack")
def setup_slack(
    control: str = typer.Option(
        None, "--control", "-c",
        help="Control node host. Auto-detected from inventory if not provided.",
    ),
) -> None:
    """Configure Slack integration for OpenClaw.

    Prompts for Slack Bot Token and App Token, stores them in the
    openclaw-secrets Secret, and restarts the OpenClaw pod. Safe to
    re-run — overwrites existing tokens.
    """
    if control is None:
        control = _get_control_host()

    console.print(f"[dim]via {control}[/dim]\n")
    console.print("Get these from your Slack app at https://api.slack.com/apps\n")

    bot_token = typer.prompt("Slack Bot Token (xoxb-...)")
    app_token = typer.prompt("Slack App Token (xapp-...)")

    if not bot_token.startswith("xoxb-"):
        console.print("[yellow]Warning: Bot token usually starts with xoxb-[/yellow]")
    if not app_token.startswith("xapp-"):
        console.print("[yellow]Warning: App token usually starts with xapp-[/yellow]")

    _patch_secret(control, "openclaw", "openclaw-secrets", {
        "slack-bot-token": bot_token,
        "slack-app-token": app_token,
    })
    _restart_deployment(control, "openclaw", "openclaw")
    console.print(f"\n[green]Slack tokens configured. OpenClaw restarting.[/green]")
    console.print(f"\nOnce someone messages the bot in Slack, approve them with:")
    console.print(f"  ./scripts/cluster_manager.py approve-pairing slack <CODE>")


@app.command("remove-slack")
def remove_slack(
    control: str = typer.Option(
        None, "--control", "-c",
        help="Control node host. Auto-detected from inventory if not provided.",
    ),
) -> None:
    """Remove Slack integration from OpenClaw.

    Deletes the Slack tokens from the cluster Secret and restarts OpenClaw.
    """
    if control is None:
        control = _get_control_host()

    if not typer.confirm("This will remove Slack integration and delete the API tokens. Continue?"):
        raise typer.Exit(0)

    console.print(f"[dim]via {control}[/dim]\n")

    _patch_secret(control, "openclaw", "openclaw-secrets", {
        "slack-bot-token": None,
        "slack-app-token": None,
    })
    _restart_deployment(control, "openclaw", "openclaw")
    console.print(f"[green]Slack tokens removed. OpenClaw restarting.[/green]")


@app.command("setup-telegram")
def setup_telegram(
    control: str = typer.Option(
        None, "--control", "-c",
        help="Control node host. Auto-detected from inventory if not provided.",
    ),
) -> None:
    """Configure Telegram integration for OpenClaw.

    Prompts for the Telegram Bot Token (from @BotFather). Stores it in
    the cluster Secret and restarts the OpenClaw pod.
    """

    if control is None:
        control = _get_control_host()

    console.print(f"[dim]via {control}[/dim]\n")
    console.print("Get your bot token from @BotFather on Telegram\n")

    bot_token = typer.prompt("Telegram Bot Token")

    _patch_secret(control, "openclaw", "openclaw-secrets", {
        "telegram-bot-token": bot_token,
    })
    _restart_deployment(control, "openclaw", "openclaw")
    console.print(f"\n[green]Telegram bot configured. OpenClaw restarting.[/green]")
    console.print(f"\nOnce someone messages the bot on Telegram, approve them with:")
    console.print(f"  ./scripts/cluster_manager.py approve-pairing telegram <CODE>")


@app.command("remove-telegram")
def remove_telegram(
    control: str = typer.Option(
        None, "--control", "-c",
        help="Control node host. Auto-detected from inventory if not provided.",
    ),
) -> None:
    """Remove Telegram integration from OpenClaw.

    Deletes the Telegram token from the cluster Secret and restarts OpenClaw.
    """
    if control is None:
        control = _get_control_host()

    if not typer.confirm("This will remove Telegram integration and delete the bot token. Continue?"):
        raise typer.Exit(0)

    console.print(f"[dim]via {control}[/dim]\n")

    _patch_secret(control, "openclaw", "openclaw-secrets", {
        "telegram-bot-token": None,
    })
    _restart_deployment(control, "openclaw", "openclaw")
    console.print(f"[green]Telegram token removed. OpenClaw restarting.[/green]")


@app.command("setup-obsidian")
def setup_obsidian(
    control: str = typer.Option(
        None, "--control", "-c",
        help="Control node host. Auto-detected from inventory if not provided.",
    ),
) -> None:
    """Configure Obsidian Sync for the workspace.

    Prompts for your Obsidian auth token and vault name. The token is
    obtained by running:
      docker run --rm -it --entrypoint get-token ghcr.io/belphemur/obsidian-headless-sync-docker:latest

    Stores the token in the cluster Secret and vault name in a ConfigMap,
    then restarts the sync pod.
    """

    if control is None:
        control = _get_control_host()

    console.print(f"[dim]via {control}[/dim]\n")
    console.print("First, get your auth token by running this on your workstation:")
    console.print("  [cyan]docker run --rm -it --entrypoint get-token ghcr.io/belphemur/obsidian-headless-sync-docker:latest[/cyan]\n")

    auth_token = typer.prompt("Obsidian auth token")
    vault_name = typer.prompt("Obsidian vault name (exact match)")

    _patch_secret(control, "openclaw", "openclaw-secrets", {
        "obsidian-auth-token": auth_token,
    })

    # Create or update the vault name ConfigMap
    _ssh(control,
        f"sudo k3s kubectl -n openclaw create configmap obsidian-config"
        f" --from-literal=vault-name={_q(vault_name)}"
        f" --dry-run=client -o yaml"
        f" | sudo k3s kubectl apply -f -"
    )

    _restart_deployment(control, "openclaw", "obsidian-sync")

    console.print(f"\n[green]Obsidian Sync configured for vault '{vault_name}'.[/green]")
    console.print("The sync pod will start pulling your vault shortly.")


@app.command("approve-pairing")
def approve_pairing(
    channel: str = typer.Argument(..., help="Channel type (e.g. slack, telegram, whatsapp)."),
    code: str = typer.Argument(..., help="Pairing code shown to the user."),
    control: str = typer.Option(
        None, "--control", "-c",
        help="Control node host. Auto-detected from inventory if not provided.",
    ),
) -> None:
    """Approve a user's pairing request for an OpenClaw channel."""
    if control is None:
        control = _get_control_host()
    _ssh(control,
        f"sudo k3s kubectl -n openclaw exec deploy/openclaw --"
        f" openclaw pairing approve {_q(channel)} {_q(code)}"
    )


def _ollama_url() -> str:
    apps_domain = _get_apps_domain()
    return f"https://ollama.{apps_domain}"


models_app = typer.Typer(name="models", help="Manage Ollama models and OpenClaw model selection.", no_args_is_help=True)
app.add_typer(models_app)


@models_app.command("list")
def models_list() -> None:
    """Show models available in Ollama (already pulled)."""
    import json
    url = f"{_ollama_url()}/api/tags"
    result = subprocess.run(
        ["curl", "-sk", url], capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]Could not reach Ollama at {url}[/red]")
        raise typer.Exit(1)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        console.print(f"[red]Unexpected response from Ollama[/red]")
        raise typer.Exit(1)

    models = data.get("models", [])
    if not models:
        console.print("[dim]No models pulled yet.[/dim]")
        return

    # Show active model
    control = _get_control_host()
    active = _kubectl(control, "-n", "openclaw",
        "get", "configmap", "openclaw-model",
        "-o", "jsonpath={.data.active-model}",
        "--ignore-not-found", capture=True,
    ).stdout.strip()

    for m in models:
        name = m.get("name", m.get("model", "unknown"))
        size_gb = m.get("size", 0) / 1e9
        marker = " [green](active)[/green]" if name == active else ""
        console.print(f"  {name}  [dim]{size_gb:.1f} GB[/dim]{marker}")


@models_app.command("pull")
def models_pull(
    model: str = typer.Argument(..., help="Model tag, e.g. gemma4:26b"),
) -> None:
    """Pull a model into Ollama."""
    url = f"{_ollama_url()}/api/pull"
    console.print(f"Pulling [cyan]{model}[/cyan] from [dim]{url}[/dim]")
    rc = subprocess.run(
        ["curl", "-fsSNk", url, "-d", f'{{"name":"{model}"}}']
    ).returncode
    if rc == 0:
        console.print("\n[green]Done.[/green]")
    raise typer.Exit(rc)


@models_app.command("set")
def models_set(
    model: str = typer.Argument(..., help="Model tag to set as active, e.g. gemma4:26b"),
) -> None:
    """Set the active model for OpenClaw. Restarts the OpenClaw pod."""
    control = _get_control_host()

    console.print(f"Setting active model to [cyan]{model}[/cyan]")
    _ssh(control,
        f"sudo k3s kubectl -n openclaw create configmap openclaw-model"
        f" --from-literal=active-model={_q(model)}"
        f" --dry-run=client -o yaml"
        f" | sudo k3s kubectl apply -f -"
    )
    _restart_deployment(control, "openclaw", "openclaw")
    console.print(f"[green]Active model set to {model}. OpenClaw restarting.[/green]")


@models_app.command("remove")
def models_remove(
    model: str = typer.Argument(..., help="Model tag to remove, e.g. llama3.2:3b"),
) -> None:
    """Delete a model from Ollama."""
    url = f"{_ollama_url()}/api/delete"
    console.print(f"Removing [cyan]{model}[/cyan]")
    rc = subprocess.run(
        ["curl", "-fsk", "-X", "DELETE", url, "-d", f'{{"name":"{model}"}}']
    ).returncode
    if rc == 0:
        console.print("[green]Done.[/green]")
    else:
        console.print("[red]Failed to remove model.[/red]")
    raise typer.Exit(rc)


@app.command("restart")
def restart(
    control: str = typer.Option(
        None, "--control", "-c",
        help="Control node host. Auto-detected from inventory if not provided.",
    ),
    wipe_rag: bool = typer.Option(
        False, "--wipe-rag",
        help="Delete ChromaDB data and re-index from scratch.",
    ),
) -> None:
    """Restart the full application stack in the correct order.

    Restarts: ChromaDB → RAG indexer → RAG MCP → OpenClaw.
    With --wipe-rag, also deletes ChromaDB data for a clean re-index.
    """
    if control is None:
        control = _get_control_host()

    console.print(f"[dim]via {control}[/dim]\n")

    if wipe_rag:
        console.print("Wiping ChromaDB data...")
        _kubectl(control, "-n", "openclaw", "scale", "deployment/chromadb", "--replicas=0")
        _kubectl(control, "-n", "openclaw", "delete", "pvc", "chromadb-data", "--ignore-not-found")
        _kubectl(control, "-n", "openclaw", "scale", "deployment/chromadb", "--replicas=1")
        console.print("Waiting for ChromaDB to recreate...")
        _kubectl(control, "-n", "openclaw", "wait", "--for=condition=Ready",
                 "pod", "-l", "app=chromadb", "--timeout=120s")

    steps = [
        ("ChromaDB", "chromadb"),
        ("RAG Indexer", "rag-indexer"),
        ("RAG MCP Server", "rag-mcp"),
        ("OpenClaw", "openclaw"),
    ]

    for name, deployment in steps:
        console.print(f"Restarting {name}...")
        _restart_deployment(control, "openclaw", deployment)

    console.print("\nWaiting for pods to come up...")
    _kubectl(control, "-n", "openclaw", "wait", "--for=condition=Ready",
             "pod", "-l", "app=openclaw", "--timeout=180s")
    console.print("[green]Stack restarted.[/green]")


@app.command("status")
def status(
    control: str = typer.Option(
        None, "--control", "-c",
        help="Control node host. Defaults to the first host in [control] in inventory.",
    ),
) -> None:
    """Quick cluster snapshot: nodes + pods across all namespaces."""
    if control is None:
        control = _get_control_host()

    console.print(f"[dim]via {control}[/dim]\n")
    for args in (["get", "nodes", "-o", "wide"], ["get", "pods", "-A"]):
        console.print(f"[dim]$ kubectl {' '.join(args)}[/dim]")
        _kubectl(control, *args)
        console.print()


# ---------------------------------------------------------------------------
# Infrastructure secrets: Garage tokens + layout init. Consumer apps create
# their own Secrets + buckets in their own namespaces.
# ---------------------------------------------------------------------------

@app.command("bootstrap-infra-secrets")
def bootstrap_infra_secrets(
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Generate Garage's auth tokens, create garage-auth Secret, apply the
    single-node layout. Run once after Argo first syncs the garage app.
    Idempotent — skips existing secrets and detects a layout that's already
    applied.
    """
    import secrets as secrets_mod

    if control is None:
        control = _get_control_host()

    console.print(f"[dim]via {control}[/dim]\n")

    # --- garage-auth Secret ---
    if _kubectl_exists(control, "garage", "secret", "garage-auth"):
        console.print("[dim]garage-auth already exists, skipping secret creation.[/dim]")
    else:
        _ensure_namespace(control, "garage")
        rpc_secret = secrets_mod.token_hex(32)      # Garage expects hex
        admin_token = secrets_mod.token_urlsafe(32)
        metrics_token = secrets_mod.token_urlsafe(32)

        _kubectl(control, "-n", "garage", "create", "secret", "generic", "garage-auth",
                 f"--from-literal=rpc_secret={rpc_secret}",
                 f"--from-literal=admin_token={admin_token}",
                 f"--from-literal=metrics_token={metrics_token}")
        console.print("[green]✓[/green] garage-auth Secret created.")
        console.print(f"\n[bold]Admin token (save it — grants full Garage admin API access):[/bold]")
        console.print(f"  [cyan]{admin_token}[/cyan]\n")

        # Restart StatefulSet so any crashlooping pod picks up the new Secret.
        _kubectl(control, "-n", "garage", "rollout", "restart", "statefulset/garage")

    # --- wait for garage-0 ready ---
    console.print("Waiting for garage-0 to be ready...")
    rc = _kubectl(control, "-n", "garage", "wait", "--for=condition=ready",
                  "pod/garage-0", "--timeout=120s").returncode
    if rc != 0:
        console.print("[red]garage-0 did not become ready. Check: kubectl -n garage logs garage-0[/red]")
        raise typer.Exit(1)

    # --- layout init (idempotent) ---
    # garage layout show lists assigned roles. If the single node already has a
    # capacity assigned, skip; otherwise assign + apply.
    layout_show = _kubectl(control, "-n", "garage", "exec", "garage-0", "--",
                           "/garage", "layout", "show", capture=True).stdout
    if "capacity" in layout_show.lower() and "role" in layout_show.lower():
        console.print("[dim]Garage layout already applied, skipping.[/dim]")
    else:
        console.print("Applying Garage single-node layout...")
        node_id = _kubectl(control, "-n", "garage", "exec", "garage-0", "--",
                           "/garage", "node", "id", "-q", capture=True).stdout.strip()
        # Output may be "<full_id>@<addr>"; take the hex prefix.
        node_id_short = node_id.split("@", 1)[0][:16]
        _kubectl(control, "-n", "garage", "exec", "garage-0", "--",
                 "/garage", "layout", "assign", "-z", "dc1", "-c", "50G",
                 node_id_short)
        _kubectl(control, "-n", "garage", "exec", "garage-0", "--",
                 "/garage", "layout", "apply", "--version", "1")
        console.print("[green]✓[/green] Garage layout applied.")

    console.print()
    console.print("[green]Infra secrets bootstrap complete.[/green]")
    console.print("  S3 endpoint (in-cluster): [cyan]http://garage-s3.garage.svc:3900[/cyan]")
    console.print("  Admin API (in-cluster):   [cyan]http://garage-s3.garage.svc:3903[/cyan]")
    console.print("  Consumer apps: create your own access keys via the admin API and")
    console.print("                 store them in your app's own namespace Secret.")


# ---------------------------------------------------------------------------
# Private apps repo: Argo CD watches a separate private repo for the operator's
# private applications, keeping them out of the public bootstrap repo entirely.
# ---------------------------------------------------------------------------

PRIVATE_APPS_TEMPLATE_DIR = REPO_DIR / "scripts" / "private_apps_template"

private_apps_app = typer.Typer(
    name="private-apps",
    help="Set up Argo CD to watch a private apps repo for your non-shareable workloads.",
    no_args_is_help=True,
)
app.add_typer(private_apps_app)


def _apply_yaml(control: str, yaml_text: str) -> None:
    """kubectl apply a YAML document supplied via stdin."""
    subprocess.run(
        ["ssh", control, "sudo", "k3s", "kubectl", "apply", "-f", "-"],
        input=yaml_text, text=True, check=True,
    )


PRIVATE_APPS_MANAGED_LABEL = "cluster-manager/private-apps"


def _derive_project_name(repo_url: str) -> str:
    """Derive a project name from a git SSH URL.

    git@github.com:clacasse/fieldstone-private-apps.git  →  fieldstone-private-apps
    ssh://git@host/org/repo.git                          →  repo
    """
    match = re.search(r"[:/]([^/]+?)(?:\.git)?/?$", repo_url)
    if not match:
        raise ValueError(f"can't derive project name from {repo_url!r}")
    return match.group(1)


def _list_private_apps_projects(control: str) -> list[dict[str, str]]:
    """Return every private-apps registration on the cluster.

    Discovers via the `cluster-manager/private-apps=true` label on AppProject.
    For each one, pulls repoURL + sync/health from the matching Application.
    """
    proj_result = _kubectl(
        control, "-n", "argocd", "get", "appproject",
        "-l", f"{PRIVATE_APPS_MANAGED_LABEL}=true",
        "-o", "name",
        capture=True,
    )
    # kubectl -o name returns "appproject.argoproj.io/<name>" per line.
    names = [
        line.split("/", 1)[-1]
        for line in (proj_result.stdout or "").strip().splitlines()
        if line
    ]

    entries: list[dict[str, str]] = []
    for name in names:
        # Literal `|` between the three jsonpath expressions (outside {}),
        # then split in Python. Using `{'|'}` inside jsonpath doesn't work
        # across kubectl versions; double-quoted `{"|"}` does but plain
        # literal chars are simpler.
        app_result = _kubectl(
            control, "-n", "argocd", "get", "application", f"{name}-root",
            "--ignore-not-found",
            "-o",
            "jsonpath={.spec.source.repoURL}|{.status.sync.status}|{.status.health.status}",
            capture=True,
        )
        stdout = (app_result.stdout or "").strip()
        if not stdout:
            entries.append({
                "project": name, "repo_url": "?", "sync": "Unknown", "health": "Unknown",
            })
            continue
        parts = (stdout.split("|") + ["", "", ""])[:3]
        entries.append({
            "project": name,
            "repo_url": parts[0] or "?",
            "sync": parts[1] or "Unknown",
            "health": parts[2] or "Unknown",
        })
    return entries


@private_apps_app.command("scaffold")
def private_apps_scaffold(
    path: Path = typer.Argument(..., help="Directory to create for the new private apps repo."),
) -> None:
    """Scaffold a starter private apps repo directory on disk."""
    import shutil

    if path.exists() and any(path.iterdir()):
        console.print(f"[red]{path} already exists and is not empty.[/red]")
        raise typer.Exit(1)

    shutil.copytree(PRIVATE_APPS_TEMPLATE_DIR, path, dirs_exist_ok=True)
    console.print(f"  [green]✓[/green] Scaffolded private apps repo at {path}")
    console.print()
    console.print("Next steps:")
    console.print(f"  cd {path}")
    console.print("  git init && git add . && git commit -m 'Initial scaffold'")
    console.print("  gh repo create <name> --private --source . --push")
    console.print("  cluster_manager.py private-apps setup --repo-url git@github.com:you/<name>.git")


@private_apps_app.command("setup")
def private_apps_setup(
    repo_url: str = typer.Option(..., "--repo-url", help="SSH URL of the private apps repo (git@...)."),
    ssh_key_path: Path = typer.Option(
        None, "--ssh-key-path",
        help="Path for the Argo CD SSH deploy key. Default: ~/.ssh/argocd-<project>.key",
    ),
    project_name: str = typer.Option(
        None, "--project-name",
        help="Argo CD AppProject name. Default: derived from the repo URL (e.g. "
             "git@github.com:you/my-apps.git → 'my-apps').",
    ),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Register a private apps repo with Argo CD.

    Generates a per-project SSH deploy key, prompts you to add the public key to
    the repo, then applies three labeled manifests: Repository Secret, AppProject,
    and a root app-of-apps Application watching <repo>/apps. Multiple private
    repos can coexist — each gets its own project + key.

    Collision check: if an existing project of the same name points at a
    different repo URL, we refuse and list current registrations so you can
    pick a different --project-name or unregister the conflicting one first.
    """
    if not re.match(r"^(git@|ssh://)", repo_url):
        console.print(f"[red]--repo-url must be SSH (git@... or ssh://...). Got: {repo_url}[/red]")
        raise typer.Exit(1)

    if control is None:
        control = _get_control_host()

    if project_name is None:
        project_name = _derive_project_name(repo_url)
        console.print(f"  [dim]Derived project name: [cyan]{project_name}[/cyan][/dim]")

    # Collision check.
    existing = _list_private_apps_projects(control)
    for entry in existing:
        if entry["project"] == project_name and entry["repo_url"] not in ("", repo_url):
            console.print(
                f"[red]Project [cyan]{project_name}[/cyan] already exists pointing at "
                f"{entry['repo_url']}.[/red]"
            )
            console.print("Either pick a different --project-name, or unregister first:")
            console.print(f"  cluster_manager.py private-apps unregister --project-name {project_name}")
            raise typer.Exit(1)

    if ssh_key_path is None:
        ssh_key_path = Path.home() / ".ssh" / f"argocd-{project_name}.key"
    pub_key_path = ssh_key_path.with_suffix(ssh_key_path.suffix + ".pub")

    # 1. SSH keypair
    if not ssh_key_path.exists():
        ssh_key_path.parent.mkdir(mode=0o700, exist_ok=True)
        console.print(f"Generating SSH deploy key at [cyan]{ssh_key_path}[/cyan]")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "",
             "-C", f"argocd-{project_name}@{_get_repo_url().rsplit('/', 1)[-1]}",
             "-f", str(ssh_key_path)],
            check=True,
        )
    else:
        console.print(f"  [dim]Reusing existing key at {ssh_key_path}[/dim]")

    pub_key = pub_key_path.read_text().strip()
    private_key = ssh_key_path.read_text()

    # 2. Prompt for deploy-key addition
    console.print()
    console.print("[bold]Add this as a deploy key (read-only) on your private repo:[/bold]")
    console.print()
    console.print(f"  [cyan]{pub_key}[/cyan]")
    console.print()
    gh_url = repo_url.replace('git@github.com:', 'https://github.com/').replace('.git', '')
    console.print(f"  GitHub UI: {gh_url}/settings/keys/new")
    console.print()
    typer.prompt("Press Enter once the deploy key is added", default="", show_default=False)

    # 3. Repository Secret (labeled so `list` can find us).
    #
    # json.dumps() produces a properly quoted YAML string value — critical
    # here because git SSH URLs (git@host:path) contain a colon that plain-
    # scalar YAML parsers can misinterpret as a mapping separator.
    secret_name = f"{project_name}-repo"
    indented_key = "\n".join("    " + line for line in private_key.splitlines())
    url_yaml = json.dumps(repo_url)
    project_yaml = json.dumps(project_name)
    repo_secret = f"""\
apiVersion: v1
kind: Secret
metadata:
  name: {secret_name}
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
    {PRIVATE_APPS_MANAGED_LABEL}: "true"
    cluster-manager/private-apps-project: {project_yaml}
stringData:
  type: git
  url: {url_yaml}
  project: {project_yaml}
  sshPrivateKey: |
{indented_key}
"""
    console.print(f"Applying Repository Secret [cyan]{secret_name}[/cyan]")
    _apply_yaml(control, repo_secret)

    # 4. AppProject
    app_project = f"""\
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: {project_name}
  namespace: argocd
  labels:
    {PRIVATE_APPS_MANAGED_LABEL}: "true"
    cluster-manager/private-apps-project: {project_yaml}
spec:
  description: Private apps repository ({project_name})
  sourceRepos:
    - '*'
  destinations:
    - namespace: '*'
      server: https://kubernetes.default.svc
  clusterResourceWhitelist:
    - group: '*'
      kind: '*'
  namespaceResourceWhitelist:
    - group: '*'
      kind: '*'
"""
    console.print(f"Applying AppProject [cyan]{project_name}[/cyan]")
    _apply_yaml(control, app_project)

    # 5. Root Application (app-of-apps)
    root_app = f"""\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {project_name}-root
  namespace: argocd
  labels:
    {PRIVATE_APPS_MANAGED_LABEL}: "true"
    cluster-manager/private-apps-project: {project_yaml}
spec:
  project: {project_name}
  source:
    repoURL: {url_yaml}
    targetRevision: HEAD
    path: apps
    directory:
      recurse: true
  destination:
    server: https://kubernetes.default.svc
    namespace: argocd
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
"""
    console.print(f"Applying root Application [cyan]{project_name}-root[/cyan]")
    _apply_yaml(control, root_app)

    console.print()
    console.print(
        f"[green]✓[/green] Registered [cyan]{project_name}[/cyan] → {repo_url}"
    )
    console.print(f"  Commit anything under [cyan]apps/[/cyan] and Argo will sync it.")
    console.print(f"  Inspect: cluster_manager.py private-apps list")


@private_apps_app.command("list")
def private_apps_list(
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Show every private apps repo currently registered with Argo CD."""
    if control is None:
        control = _get_control_host()

    entries = _list_private_apps_projects(control)
    if not entries:
        console.print("[dim]No private apps projects registered.[/dim]")
        return

    name_w = max(len("PROJECT"), max(len(e["project"]) for e in entries))
    url_w = max(len("REPO URL"), max(len(e["repo_url"]) for e in entries))
    console.print(f"[bold]{'PROJECT'.ljust(name_w)}  {'REPO URL'.ljust(url_w)}  SYNC     HEALTH[/bold]")
    for e in entries:
        console.print(
            f"{e['project'].ljust(name_w)}  {e['repo_url'].ljust(url_w)}  "
            f"{e['sync'].ljust(7)}  {e['health']}"
        )


@private_apps_app.command("unregister")
def private_apps_unregister(
    project_name: str = typer.Option(..., "--project-name",
                                      help="Project to tear down (see `private-apps list`)."),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Remove the Argo CD Application, AppProject, and Repository Secret for a
    private apps project. Does NOT delete the SSH key on disk or the deploy key
    on the git host — remove those manually if you no longer need them.
    """
    if control is None:
        control = _get_control_host()

    existing = _list_private_apps_projects(control)
    names = [e["project"] for e in existing]
    if project_name not in names:
        if not names:
            console.print(f"[red]No private apps projects registered. Nothing to do.[/red]")
        else:
            console.print(f"[red]No project named [cyan]{project_name}[/cyan]. Did you mean one of:[/red]")
            for n in names:
                console.print(f"  {n}")
        raise typer.Exit(1)

    for kind, name in [
        ("application", f"{project_name}-root"),
        ("appproject", project_name),
        ("secret", f"{project_name}-repo"),
    ]:
        console.print(f"Deleting [cyan]{kind}/{name}[/cyan]")
        _kubectl(control, "-n", "argocd", "delete", kind, name, "--ignore-not-found")

    console.print()
    console.print(f"[green]✓[/green] Unregistered private apps project '{project_name}'.")
    console.print(f"[dim]Reminder: remove the deploy key on your git host if no longer needed.[/dim]")
    console.print(f"[dim]SSH key ~/.ssh/argocd-{project_name}.key left on disk for safety — delete manually if unwanted.[/dim]")


if __name__ == "__main__":
    app()

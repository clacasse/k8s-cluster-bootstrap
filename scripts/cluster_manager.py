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

def _ssh_cmd(control: str, cmd: str) -> subprocess.CompletedProcess:
    """Run a shell command on the control node via SSH. Quotes are the caller's responsibility."""
    return subprocess.run(["ssh", control, cmd])


def _ssh_cmd_capture(control: str, cmd: str) -> subprocess.CompletedProcess:
    """Run a shell command on the control node via SSH, capturing output."""
    return subprocess.run(["ssh", control, cmd], capture_output=True, text=True)


def _q(s: str) -> str:
    """Shell-quote a string for safe interpolation into SSH commands."""
    return shlex.quote(s)


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
    result = subprocess.run(
        ["ssh", control, "sudo", "k3s", "kubectl", "-n", "kube-system",
         "get", "secret", "wildcard-apps-tls", "--ignore-not-found", "-o", "name"],
        capture_output=True, text=True,
    )
    if result.stdout.strip():
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
            subprocess.run(["ssh", control, "mkdir -p -m 700 /tmp/tls-setup"],
                          check=True, capture_output=True)
            subprocess.run(["scp", str(key_path), str(cert_path),
                           f"{control}:/tmp/tls-setup/"], check=True, capture_output=True)
            subprocess.run([
                "ssh", control,
                "sudo k3s kubectl -n kube-system create secret tls wildcard-apps-tls"
                " --cert=/tmp/tls-setup/tls.crt --key=/tmp/tls-setup/tls.key"
                " ; rm -rf /tmp/tls-setup",
            ], check=True)
        console.print(f"[green]Wildcard TLS cert created in kube-system/wildcard-apps-tls.[/green]")
        console.print(f"[yellow]This is a self-signed cert — your browser will show a warning on first visit.[/yellow]\n")

    # --- OpenClaw gateway token ---
    result = subprocess.run(
        ["ssh", control, "sudo", "k3s", "kubectl", "-n", "openclaw",
         "get", "secret", "openclaw-secrets", "--ignore-not-found", "-o", "name"],
        capture_output=True, text=True,
    )
    if result.stdout.strip():
        console.print("[dim]openclaw-secrets already exists, skipping.[/dim]")
    else:
        token = secrets_mod.token_urlsafe(32)
        subprocess.run([
            "ssh", control,
            "sudo k3s kubectl create namespace openclaw --dry-run=client -o yaml"
            " | sudo k3s kubectl apply -f -",
        ], capture_output=True)
        _ssh_cmd(control,
            f"sudo k3s kubectl -n openclaw create secret generic openclaw-secrets"
            f" --from-literal=gateway-token={_q(token)}"
        )
        console.print(f"\n[green]OpenClaw gateway token created.[/green]")
        console.print(f"[bold]Save this token — you'll need it to log into the OpenClaw web UI:[/bold]")
        console.print(f"\n  [cyan]{token}[/cyan]\n")

    # --- OpenClaw active model ConfigMap ---
    result = subprocess.run(
        ["ssh", control, "sudo", "k3s", "kubectl", "-n", "openclaw",
         "get", "configmap", "openclaw-model", "--ignore-not-found", "-o", "name"],
        capture_output=True, text=True,
    )
    if result.stdout.strip():
        console.print("[dim]openclaw-model ConfigMap already exists, skipping.[/dim]")
    else:
        model = typer.prompt("Default model for OpenClaw (e.g. gemma4:26b)")
        _ssh_cmd(control,
            f"sudo k3s kubectl -n openclaw create configmap openclaw-model"
            f" --from-literal=active-model={_q(model)}"
        )
        console.print(f"[green]Active model set to {model}.[/green]")

    # --- OpenClaw config ConfigMap ---
    result = subprocess.run(
        ["ssh", control, "sudo", "k3s", "kubectl", "-n", "openclaw",
         "get", "configmap", "openclaw-config", "--ignore-not-found", "-o", "name"],
        capture_output=True, text=True,
    )
    if result.stdout.strip():
        console.print("[dim]openclaw-config ConfigMap already exists, skipping.[/dim]")
    else:
        disable_device_auth = typer.confirm(
            "Disable device auth for OpenClaw? (required for reverse proxy access)",
            default=True,
        )
        auth_value = "true" if disable_device_auth else "false"
        _ssh_cmd(control,
            f"sudo k3s kubectl -n openclaw create configmap openclaw-config"
            f" --from-literal=disable-device-auth={_q(auth_value)}"
        )
        console.print(f"[green]OpenClaw config created (disable-device-auth={auth_value}).[/green]")

    # --- Grafana admin secret ---
    result = subprocess.run(
        ["ssh", control, "sudo", "k3s", "kubectl", "-n", "monitoring",
         "get", "secret", "grafana-admin", "--ignore-not-found", "-o", "name"],
        capture_output=True, text=True,
    )
    if result.stdout.strip():
        console.print("[dim]grafana-admin secret already exists, skipping.[/dim]")
    else:
        grafana_password = secrets_mod.token_urlsafe(24)
        subprocess.run([
            "ssh", control,
            "sudo k3s kubectl create namespace monitoring --dry-run=client -o yaml"
            " | sudo k3s kubectl apply -f -",
        ], capture_output=True)
        _ssh_cmd(control,
            f"sudo k3s kubectl -n monitoring create secret generic grafana-admin"
            f" --from-literal=admin-user=admin"
            f" --from-literal=admin-password={_q(grafana_password)}"
        )
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

    # Patch the existing secret with Slack tokens
    subprocess.run([
        "ssh", control,
        f"sudo k3s kubectl -n openclaw get secret openclaw-secrets -o json"
        f" | sudo k3s kubectl apply -f - --dry-run=client -o yaml"
        f" | sudo k3s kubectl apply -f -",
    ], capture_output=True)

    # Use kubectl patch to add the new keys
    import base64
    bot_b64 = base64.b64encode(bot_token.encode()).decode()
    app_b64 = base64.b64encode(app_token.encode()).decode()
    patch = f'{{"data":{{"slack-bot-token":"{bot_b64}","slack-app-token":"{app_b64}"}}}}'
    _ssh_cmd(control,
        f"sudo k3s kubectl -n openclaw patch secret openclaw-secrets"
        f" --type merge -p {_q(patch)}"
    )

    # Restart OpenClaw to pick up new env vars
    subprocess.run([
        "ssh", control,
        "sudo k3s kubectl -n openclaw rollout restart deployment/openclaw",
    ])
    console.print(f"\n[green]Slack tokens configured. OpenClaw restarting.[/green]")
    console.print(f"\nOnce someone messages the bot in Slack, approve them with:")
    console.print(f"  ./scripts/cluster_manager.py approve-pairing slack <CODE>")


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
    import base64

    if control is None:
        control = _get_control_host()

    console.print(f"[dim]via {control}[/dim]\n")
    console.print("Get your bot token from @BotFather on Telegram\n")

    bot_token = typer.prompt("Telegram Bot Token")

    # Patch the token into openclaw-secrets
    token_b64 = base64.b64encode(bot_token.encode()).decode()
    patch = f'{{"data":{{"telegram-token":"{token_b64}"}}}}'
    _ssh_cmd(control,
        f"sudo k3s kubectl -n openclaw patch secret openclaw-secrets"
        f" --type merge -p {_q(patch)}"
    )

    # Restart OpenClaw
    subprocess.run([
        "ssh", control,
        "sudo k3s kubectl -n openclaw rollout restart deployment/openclaw",
    ])
    console.print(f"\n[green]Telegram bot configured. OpenClaw restarting.[/green]")
    console.print(f"\nOnce someone messages the bot on Telegram, approve them with:")
    console.print(f"  ./scripts/cluster_manager.py approve-pairing telegram <CODE>")


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
    import base64

    if control is None:
        control = _get_control_host()

    console.print(f"[dim]via {control}[/dim]\n")
    console.print("First, get your auth token by running this on your workstation:")
    console.print("  [cyan]docker run --rm -it --entrypoint get-token ghcr.io/belphemur/obsidian-headless-sync-docker:latest[/cyan]\n")

    auth_token = typer.prompt("Obsidian auth token")
    vault_name = typer.prompt("Obsidian vault name (exact match)")

    # Patch the auth token into openclaw-secrets
    token_b64 = base64.b64encode(auth_token.encode()).decode()
    patch = f'{{"data":{{"obsidian-auth-token":"{token_b64}"}}}}'
    _ssh_cmd(control,
        f"sudo k3s kubectl -n openclaw patch secret openclaw-secrets"
        f" --type merge -p {_q(patch)}"
    )

    # Create or update the vault name ConfigMap
    _ssh_cmd(control,
        f"sudo k3s kubectl -n openclaw create configmap obsidian-config"
        f" --from-literal=vault-name={_q(vault_name)}"
        f" --dry-run=client -o yaml"
        f" | sudo k3s kubectl apply -f -"
    )

    # Restart the sync pod to pick up new config
    subprocess.run([
        "ssh", control,
        "sudo k3s kubectl -n openclaw rollout restart deployment/obsidian-sync",
    ], capture_output=True)

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
    subprocess.run([
        "ssh", control,
        f"sudo k3s kubectl -n openclaw exec deploy/openclaw --"
        f" openclaw pairing approve {_q(channel)} {_q(code)}",
    ])


def _ollama_url() -> str:
    apps_domain = _get_apps_domain()
    return f"https://ollama.{apps_domain}"


def _kubectl_ssh(control: str, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", control, "sudo", "k3s", "kubectl", *args],
        capture_output=capture, text=capture,
    )


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
    active = subprocess.run(
        ["ssh", control, "sudo", "k3s", "kubectl", "-n", "openclaw",
         "get", "configmap", "openclaw-model", "-o", "jsonpath={.data.active-model}",
         "--ignore-not-found"],
        capture_output=True, text=True,
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
    _ssh_cmd(control,
        f"sudo k3s kubectl -n openclaw create configmap openclaw-model"
        f" --from-literal=active-model={_q(model)}"
        f" --dry-run=client -o yaml"
        f" | sudo k3s kubectl apply -f -"
    )

    # Restart OpenClaw to pick up the new model
    subprocess.run([
        "ssh", control,
        "sudo k3s kubectl -n openclaw rollout restart deployment/openclaw",
    ])
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
        _ssh_cmd(control, "sudo k3s kubectl -n openclaw scale deployment/chromadb --replicas=0")
        _ssh_cmd(control, "sudo k3s kubectl -n openclaw delete pvc chromadb-data --ignore-not-found")
        _ssh_cmd(control, "sudo k3s kubectl -n openclaw scale deployment/chromadb --replicas=1")
        console.print("Waiting for ChromaDB to recreate...")
        _ssh_cmd(control,
            "sudo k3s kubectl -n openclaw wait --for=condition=Ready pod -l app=chromadb --timeout=120s"
        )

    steps = [
        ("ChromaDB", "chromadb"),
        ("RAG Indexer", "rag-indexer"),
        ("RAG MCP Server", "rag-mcp"),
        ("OpenClaw", "openclaw"),
    ]

    for name, deployment in steps:
        console.print(f"Restarting {name}...")
        _ssh_cmd(control, f"sudo k3s kubectl -n openclaw rollout restart deployment/{deployment}")

    console.print("\nWaiting for pods to come up...")
    _ssh_cmd(control,
        "sudo k3s kubectl -n openclaw wait --for=condition=Ready pod -l app=openclaw --timeout=180s"
    )
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
        _run(["ssh", control, "sudo", "k3s", "kubectl", *args])
        console.print()


if __name__ == "__main__":
    app()

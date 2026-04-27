#!/usr/bin/env python3
"""Cluster manager for k8s-cluster-bootstrap.

Single CLI wrapping the full lifecycle:
  init-fork       one-time: rewrite REPO_URL + APPS_DOMAIN placeholders
  prep-node       per-node: add to inventory, apt upgrade, hostname, NVIDIA if GPU
  bootstrap       whole-cluster: k3s + Argo CD
  setup-secrets   one-time: create TLS cert, OpenClaw token, Grafana creds
  llama           model management: setup / list / set-chat / set-embed
  status          runtime: cluster/node/pod summary
  sync-upstream   pull upstream changes into your instance repo

Runs from your workstation. Shells out to ansible-playbook for prep/bootstrap.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

try:
    import typer
    import yaml
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
    """Run kubectl on the control node via SSH.

    Each arg is shlex-quoted before being handed to ssh. Without this, args
    containing spaces / quotes / braces (e.g., a JSON patch for `kubectl patch
    -p <json>`) get re-tokenized by the remote shell — ssh concatenates argv
    with spaces into a single command line, which the remote shell then word-
    splits. Plain values like `-n` or a namespace name are unaffected since
    shlex.quote is a no-op for shell-safe strings.
    """
    return subprocess.run(
        ["ssh", control, "sudo", "k3s", "kubectl", *(shlex.quote(a) for a in args)],
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
    """Repo URL in HTTPS form, .git stripped. Used for human-readable
    citations + the deploy-key labels on private-apps secrets where a
    short, URL-bar-pasteable form is wanted. For the actual URL Argo CD
    will use to fetch git contents (which respects SSH vs HTTPS), use
    `_get_repo_remote_url`.
    """
    url = _get_repo_remote_url()
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url[len("git@github.com:"):]
    if url.endswith(".git"):
        url = url[:-4]
    return url


def _get_repo_remote_url() -> str:
    """Raw `git remote get-url origin` value — preserves SSH or HTTPS as
    the operator configured it. This is the URL Argo CD writes to its
    Application sources.
    """
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, cwd=REPO_DIR,
    )
    if result.returncode != 0 or not result.stdout.strip():
        console.print("[red]Could not detect git remote origin.[/red]")
        raise typer.Exit(1)
    return result.stdout.strip()


def _is_ssh_url(url: str) -> bool:
    """True for git@host:path form. Argo CD authenticates SSH URLs with a
    Repository Secret holding an ed25519 deploy key; HTTPS URLs to public
    repos need no secret at all."""
    return url.startswith("git@")


def _instance_repo_name(url: str) -> str:
    """Last path segment of a git URL, .git stripped. Used to derive a
    short, filesystem-safe identifier for the instance repo's deploy key.

    git@github.com:user/foo.git              -> foo
    https://github.com/user/foo              -> foo
    https://github.com/user/foo.git          -> foo
    """
    m = re.search(r"[:/]([^/:]+?)(?:\.git)?/?$", url)
    if not m:
        raise ValueError(f"can't derive repo name from {url!r}")
    return m.group(1)


def _image_repo(url: str) -> str:
    """OWNER/REPO_NAME from a GitHub-style git URL — exactly what the
    GitHub Actions context `${{ github.repository }}` evaluates to.
    Used to fill the IMAGE_REPO placeholder so deployment manifests pull
    fork-built images from the same path the build workflow pushes them
    to. Without this, a fork named anything other than the upstream's
    repo name would push to `ghcr.io/<owner>/<fork-name>/<image>` but
    the manifests would still hardcode the upstream repo name.

    git@github.com:user/foo.git              -> user/foo
    https://github.com/user/foo              -> user/foo
    https://github.com/user/foo.git          -> user/foo
    """
    m = re.search(r"[:/]([^/:]+)/([^/:]+?)(?:\.git)?/?$", url)
    if not m:
        raise ValueError(f"can't derive image repo from {url!r}")
    return f"{m.group(1)}/{m.group(2)}"


def _build_template_substitutions(
    repo_url: str,
    apps_domain: str,
    nfs_server: str | None,
    prior_url: str | None = None,
) -> dict[str, str]:
    """Build the placeholder→value map applied to cluster manifests at
    init-fork and sync-upstream time. Centralized so adding a placeholder
    means changing one function instead of two call sites.

    `prior_url` lets init-fork rewrite repoURL across a URL change (e.g.,
    flipping HTTPS→SSH for going private). For sync-upstream, pass None —
    a brand-new Application that arrives via merge with `repoURL: REPO_URL`
    will get the placeholder substituted; manifests already pointing at
    the fork's URL are no-ops.
    """
    if prior_url is None or prior_url == "REPO_URL":
        repo_url_replacements = {"repoURL: REPO_URL": f"repoURL: {repo_url}"}
    elif prior_url == repo_url:
        repo_url_replacements = {}
    else:
        repo_url_replacements = {f"repoURL: {prior_url}": f"repoURL: {repo_url}"}

    replacements = {
        **repo_url_replacements,
        "APPS_DOMAIN": apps_domain,
        "IMAGE_REPO": _image_repo(repo_url),
    }
    if nfs_server and nfs_server != "none":
        replacements["NFS_SERVER"] = nfs_server
    return replacements


def _apply_template_substitutions(replacements: dict[str, str]) -> int:
    """Apply the substitution map to every YAML under CLUSTERS_DIR.
    Returns the count of files modified, prints a checkmark per touch.
    """
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
    return touched


def _get_ansible_user() -> str:
    """SSH user the Ansible playbooks connect as, read from the
    `ansible_user=` line in inventory.ini's `[all:vars]`. Falls back to
    `ubuntu` (the template default) if inventory.ini doesn't exist yet
    — matters for `remove-node`, which may run before/around bootstrap
    where the inventory is in transitional states.
    """
    inv = ANSIBLE_DIR / "inventory.ini"
    if not inv.exists():
        return "ubuntu"
    for line in inv.read_text().splitlines():
        s = line.strip()
        if s.startswith("ansible_user="):
            return s.split("=", 1)[1].strip()
    return "ubuntu"


def _instance_repo_key_path(url: str) -> Path:
    """Conventional path for the instance repo's Argo deploy key.
    Lives under ~/.ssh so it never gets committed; the public key gets
    pasted into GitHub's deploy keys UI; the private key gets baked
    into the Argo Repository Secret.
    """
    return Path.home() / ".ssh" / f"argocd-instance-{_instance_repo_name(url)}.key"


def _detect_current_repo_url() -> str | None:
    """Read the repoURL the manifests currently target. Returns None if
    init-fork hasn't run yet (REPO_URL placeholder still present).
    """
    root_path = CLUSTERS_DIR / "default" / "applications" / "root.yaml"
    if not root_path.exists():
        return None
    m = re.search(r"^\s*repoURL:\s+(\S.+?)\s*$", root_path.read_text(), re.MULTILINE)
    if not m:
        return None
    url = m.group(1)
    return None if url == "REPO_URL" else url


def _ensure_instance_deploy_key_and_prompt(repo_url: str) -> Path:
    """Generate the ed25519 deploy keypair for the instance repo (if it
    doesn't exist already) and prompt the operator to add the public key
    as a deploy key on the GitHub repo. Returns the private key path.

    Idempotent: skips key generation if the file is already there. The
    prompt for adding to GitHub still fires so re-runs after a manual
    repo move surface the public key again.
    """
    key_path = _instance_repo_key_path(repo_url)
    pub_path = key_path.with_suffix(key_path.suffix + ".pub")

    if not key_path.exists():
        key_path.parent.mkdir(mode=0o700, exist_ok=True)
        console.print(f"\nGenerating SSH deploy key at [cyan]{key_path}[/cyan]")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "",
             "-C", f"argocd-instance-{_instance_repo_name(repo_url)}",
             "-f", str(key_path)],
            check=True,
        )
    else:
        console.print(f"\n  [dim]Reusing existing SSH deploy key at {key_path}[/dim]")

    pub_key = pub_path.read_text().strip()
    console.print()
    console.print("[bold]Add this public key as a deploy key (read-only) on your instance repo:[/bold]")
    console.print()
    console.print(f"  [cyan]{pub_key}[/cyan]")
    console.print()
    if repo_url.startswith("git@github.com:"):
        # git@github.com:user/repo.git -> https://github.com/user/repo/settings/keys/new
        path = repo_url.split(":", 1)[1]
        if path.endswith(".git"):
            path = path[:-4]
        console.print(f"  GitHub UI: https://github.com/{path}/settings/keys/new")
    console.print()
    console.print(
        "[dim]After adding the deploy key, run this once the cluster is bootstrapped:\n"
        "    cluster_manager.py setup-instance-repo\n"
        "to apply the matching Argo Repository Secret.[/dim]"
    )
    return key_path


def _apply_instance_repo_secret(control: str, repo_url: str, key_path: Path) -> None:
    """Apply the Argo Repository Secret for the instance repo. Idempotent.
    Used both at first private-instance bootstrap and after a public→private
    conversion.
    """
    private_key = key_path.read_text()
    indented_key = "\n".join("    " + line for line in private_key.splitlines())
    url_yaml = json.dumps(repo_url)
    secret_yaml = f"""\
apiVersion: v1
kind: Secret
metadata:
  name: instance-repo
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
    cluster-manager/instance-repo: "true"
stringData:
  type: git
  url: {url_yaml}
  sshPrivateKey: |
{indented_key}
"""
    console.print(f"Applying Repository Secret [cyan]instance-repo[/cyan] for {repo_url}")
    _apply_yaml(control, secret_yaml)
    _assert_stored_secret_field(
        control, namespace="argocd", name="instance-repo",
        field="url", expected=repo_url, context="instance Repository Secret",
    )


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
        None, help="Explicit repo URL (HTTPS or SSH). Defaults to `git remote get-url origin`.",
    ),
    apps_domain: str = typer.Option(
        DEFAULT_APPS_DOMAIN,
        "--apps-domain",
        help="Wildcard DNS domain for app Ingress hostnames. Must match the "
             "*.<domain> A record you created on your router.",
    ),
) -> None:
    """Initialize / re-initialize repoURL + APPS_DOMAIN substitution.

    First run on a fresh fork replaces the `REPO_URL` placeholder. Re-run
    after changing your repo's location (e.g. flipping public→private with
    a new SSH URL) to rewrite every `repoURL:` line to the new value —
    detection is automatic via the existing repoURL value in root.yaml.

    If the URL is SSH (`git@host:user/repo.git`), also generates an
    ed25519 deploy keypair at ~/.ssh/argocd-instance-<repo>.key and
    prompts you to add the public key as a deploy key on the repo.
    The Argo Repository Secret pairing that key with the URL is applied
    in a separate step — `setup-instance-repo` — that needs the cluster
    to exist.
    """
    if repo_url is None:
        repo_url = _get_repo_remote_url()

    if apps_domain == DEFAULT_APPS_DOMAIN:
        apps_domain = typer.prompt(
            "Wildcard DNS domain for app Ingress hostnames (*.X on your router)",
            default=DEFAULT_APPS_DOMAIN,
        )

    nfs_server = typer.prompt(
        "Storage node hostname (NFS server, or 'none' to skip)",
        default="none",
    )

    image_repo = _image_repo(repo_url)

    console.print(f"Setting repoURL to:     [cyan]{repo_url}[/cyan]")
    console.print(f"Setting apps domain to: [cyan]{apps_domain}[/cyan]")
    console.print(f"Setting image registry: [cyan]ghcr.io/{image_repo}/...[/cyan]")
    if nfs_server != "none":
        console.print(f"Setting NFS server to:  [cyan]{nfs_server}[/cyan]")

    # Detect prior repoURL (if init-fork was already run). Driven from the
    # root Argo Application — its repoURL is what Argo actually uses.
    # If it's still the placeholder, this is a first init; otherwise we're
    # converting from one URL to another (e.g., HTTPS -> SSH for going
    # private). Sibling repos referenced in the manifests (e.g. a
    # deal-signal chart Application pointing at a different repo) are
    # NOT touched because the replacement is anchored at `repoURL: <prior>`.
    prior_url = _detect_current_repo_url()
    if prior_url == repo_url:
        console.print(f"  [dim](repoURL is already {repo_url})[/dim]")
    elif prior_url is not None:
        console.print(f"  Converting from prior repoURL: [yellow]{prior_url}[/yellow]")

    replacements = _build_template_substitutions(
        repo_url=repo_url,
        apps_domain=apps_domain,
        nfs_server=nfs_server,
        prior_url=prior_url,
    )
    touched = _apply_template_substitutions(replacements)

    if _is_ssh_url(repo_url):
        _ensure_instance_deploy_key_and_prompt(repo_url)

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

    # Use the raw remote URL (preserves SSH vs HTTPS as configured) so
    # the substitution matches whatever init-fork wrote originally. The
    # HTTPS-flattened _get_repo_url() form is for human-readable refs
    # like deploy-key labels, NOT for Argo source URLs (which need to
    # match the credential form Argo authenticates with).
    repo_url = _get_repo_remote_url()
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
    replacements = _build_template_substitutions(
        repo_url=repo_url,
        apps_domain=apps_domain,
        nfs_server=nfs_server,
    )
    touched = _apply_template_substitutions(replacements)

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
        ansible_user = _get_ansible_user()
        console.print(f"Uninstalling k3s on {hostname} ({node_host})...")
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", f"{ansible_user}@{node_host}",
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


@app.command("setup-instance-repo")
def setup_instance_repo(
    control: str = typer.Option(
        None, "--control", "-c",
        help="Control node host. Auto-detected from inventory if not provided.",
    ),
) -> None:
    """Wire Argo CD up to a private instance repo via SSH deploy key.

    Reads the repoURL from clusters/{cluster}/applications/root.yaml,
    confirms it's an SSH URL, and:
      1. Applies an Argo Repository Secret pairing that URL with the
         private key from ~/.ssh/argocd-instance-<repo>.key (generated
         by `init-fork` when the URL is SSH).
      2. Patches the live `root` Argo Application to use the URL — so
         conversion from public to private takes effect immediately,
         not only after the next sync of root.yaml itself.

    Idempotent: re-run any time, e.g. after rotating the deploy key
    or moving the instance repo to a new URL.

    No-op (with a friendly message) when the repoURL is HTTPS — public
    repos don't need a Repository Secret.
    """
    if control is None:
        control = _get_control_host()

    repo_url = _detect_current_repo_url()
    if repo_url is None:
        console.print(
            "[red]No initialized repoURL found in clusters/.[/red] Run "
            "[cyan]init-fork[/cyan] first."
        )
        raise typer.Exit(1)

    if not _is_ssh_url(repo_url):
        console.print(
            f"[dim]repoURL [cyan]{repo_url}[/cyan] is HTTPS — no Repository "
            f"Secret needed. (To switch to SSH/private, change your git "
            f"remote, run init-fork, then re-run this.)[/dim]"
        )
        return

    key_path = _instance_repo_key_path(repo_url)
    if not key_path.exists():
        console.print(
            f"[red]No deploy key at {key_path}.[/red] Run "
            f"[cyan]init-fork[/cyan] to generate it (it's safe to re-run)."
        )
        raise typer.Exit(1)

    _apply_instance_repo_secret(control, repo_url, key_path)

    # Patch the live root Application's repoURL so a conversion takes
    # effect immediately. After git push of the rewritten root.yaml,
    # Argo would pick this up anyway — but doing it here means the
    # Secret + URL pair becomes consistent in one CLI invocation.
    patch = {"spec": {"source": {"repoURL": repo_url}}}
    _kubectl(
        control, "-n", "argocd", "patch", "application", "root",
        "--type", "merge", "-p", json.dumps(patch),
        check=False,
    )
    console.print(
        f"[green]Done.[/green] Argo's root Application now points at "
        f"[cyan]{repo_url}[/cyan]. Push your manifest changes when ready, "
        "and the rest of the apps follow."
    )


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

    # Note: the OpenClaw active-model ConfigMap used to be created here.
    # It's been moved to `cluster_manager.py llama setup` — that command
    # now writes both the llama-cpp-model ConfigMap (chat server's
    # env) AND openclaw-model (OpenClaw's reference to it), atomically,
    # which is cleaner than having two commands write to the same place.
    # `setup-secrets` is secrets-only now.

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


LLAMA_NS = "llama-cpp"
# Two ConfigMaps back the llama.cpp stack, split by management lifecycle:
#   llama-cpp-defaults — Argo-managed, in git. Holds embedding model
#     config + any other keys that shouldn't vary per deployment.
#   llama-cpp-model    — imperatively managed by `llama setup` /
#     `llama set-chat`. Holds the chat model the operator picked for
#     THIS deployment. Not in git; not reconciled by Argo; edits stick.
# The llama-chat pod's `envFrom` points at BOTH (model as optional, so
# the pod boots on a fresh cluster and crashloops on missing
# CHAT_MODEL_FILE — the intended signal that `llama setup` is needed).
LLAMA_DEFAULTS_CONFIGMAP = "llama-cpp-defaults"
LLAMA_MODEL_CONFIGMAP = "llama-cpp-model"

# Full set of chat knobs in llama-cpp-model. Every key has a default
# in llama-cpp-defaults (git); what lives here is the operator's
# overrides for THIS deployment. `llama setup` writes all of them on
# first run; `llama set-chat` + the per-field setters patch subsets.
#
# Order in this list controls `llama setup` prompt order AND `llama list`
# display order, so keep identity-ish keys (repo/file/alias) up top and
# tunables below.
_LLAMA_CHAT_KEYS = (
    "CHAT_MODEL_REPO",
    "CHAT_MODEL_FILE",
    "CHAT_SERVED_MODEL",
    "CHAT_CTX_SIZE",
    "CHAT_GPU_LAYERS",
    "CHAT_PARALLEL_SLOTS",
    "CHAT_KV_TYPE",
    "CHAT_FLASH_ATTN",
    "CHAT_CPU_MOE",
    "CHAT_N_CPU_MOE",
    "CHAT_OVERRIDE_TENSOR",
    "CHAT_EXTRA_FLAGS",
)
# Keys in llama-cpp-defaults for the embed model. `llama set-embed`
# edits llama-cpp-defaults in the live cluster — Argo will revert that
# on the next sync, so for a durable change, edit the git file too.
_LLAMA_EMBED_KEYS = ("EMBED_MODEL_REPO", "EMBED_MODEL_FILE", "EMBED_SERVED_MODEL")

# Prompt labels for `llama setup`; same key order as _LLAMA_CHAT_KEYS.
_LLAMA_CHAT_PROMPTS = {
    "CHAT_MODEL_REPO":     "Chat model HuggingFace repo",
    "CHAT_MODEL_FILE":     "Chat GGUF filename",
    "CHAT_SERVED_MODEL":   "Served-as model id (advertised via /v1/models)",
    "CHAT_CTX_SIZE":       "Context size (tokens)",
    "CHAT_GPU_LAYERS":     "GPU layer offload (999 = all, lower = partial CPU offload)",
    "CHAT_PARALLEL_SLOTS": "Parallel request slots",
    "CHAT_KV_TYPE":        "KV cache quantization (q8_0 / q4_0 / q5_0 / f16)",
    "CHAT_FLASH_ATTN":     "Flash attention (on / off / auto)",
    "CHAT_CPU_MOE":        "Offload MoE expert tensors to CPU/RAM (on / off)",
    "CHAT_N_CPU_MOE":      "Partial MoE offload: first N expert layers to CPU (0 = off)",
    "CHAT_OVERRIDE_TENSOR": "Tensor placement regex (empty = none, advanced)",
    "CHAT_EXTRA_FLAGS":    "Extra llama-server flags (optional)",
}

# Validation enums for knobs that take a fixed vocabulary. Reject early
# with a specific error rather than letting llama-server crashloop on a
# typo.
_VALID_KV_TYPES = ("q8_0", "q4_0", "q4_1", "q5_0", "q5_1", "f16", "f32")
_VALID_FLASH_ATTN = ("on", "off", "auto")
# CHAT_CPU_MOE is parsed by the deployment shell as on/true/1 = enable,
# anything else = off. Accept the same vocabulary here for consistency.
_VALID_CPU_MOE = ("on", "off", "true", "false", "1", "0")


def _validate_chat_field(key: str, value: str) -> None:
    """Raise typer.BadParameter on invalid values. Called by every
    setter + by `llama setup` after each prompt."""
    if key == "CHAT_KV_TYPE" and value not in _VALID_KV_TYPES:
        raise typer.BadParameter(
            f"CHAT_KV_TYPE must be one of {list(_VALID_KV_TYPES)}, got {value!r}"
        )
    if key == "CHAT_FLASH_ATTN" and value not in _VALID_FLASH_ATTN:
        raise typer.BadParameter(
            f"CHAT_FLASH_ATTN must be one of {list(_VALID_FLASH_ATTN)}, got {value!r}"
        )
    if key == "CHAT_CPU_MOE" and value not in _VALID_CPU_MOE:
        raise typer.BadParameter(
            f"CHAT_CPU_MOE must be one of {list(_VALID_CPU_MOE)}, got {value!r}"
        )
    if key in ("CHAT_CTX_SIZE", "CHAT_GPU_LAYERS", "CHAT_PARALLEL_SLOTS", "CHAT_N_CPU_MOE"):
        try:
            n = int(value)
        except ValueError as e:
            raise typer.BadParameter(f"{key} must be an integer, got {value!r}") from e
        if n < 0:
            raise typer.BadParameter(f"{key} must be non-negative, got {n}")


llama_app = typer.Typer(
    name="llama",
    help="Manage llama.cpp chat + embed model selection.",
    no_args_is_help=True,
)
app.add_typer(llama_app)


def _llama_read_config(control: str) -> dict[str, str]:
    """Read both ConfigMaps, merged. Values in llama-cpp-model win over
    llama-cpp-defaults on key collision, same as the pod's envFrom order.
    """
    out: dict[str, str] = {}
    for cm in (LLAMA_DEFAULTS_CONFIGMAP, LLAMA_MODEL_CONFIGMAP):
        result = _kubectl(
            control, "-n", LLAMA_NS, "get", "configmap", cm,
            "--ignore-not-found", "-o", "json",
            capture=True, check=False,
        )
        if (result.stdout or "").strip():
            out.update(json.loads(result.stdout).get("data", {}) or {})
    return out


def _llama_patch_model_config(control: str, updates: dict[str, str]) -> None:
    """Apply a ConfigMap merge patch to llama-cpp-model, creating the
    ConfigMap if it doesn't exist yet.

    llama-cpp-model is imperative: NOT in git, NOT Argo-managed. Edits
    here persist across reconciles.
    """
    # Ensure the ConfigMap exists (empty-body create is fine if it doesn't).
    if not _kubectl_exists(control, LLAMA_NS, "configmap", LLAMA_MODEL_CONFIGMAP):
        _ssh(control,
            f"sudo k3s kubectl -n {LLAMA_NS} create configmap"
            f" {LLAMA_MODEL_CONFIGMAP} --dry-run=client -o yaml"
            f" | sudo k3s kubectl apply -f -",
            capture=True,
        )
    patch = {"data": updates}
    # Go through _kubectl so the JSON payload gets shlex.quote'd — ssh
    # joins argv with spaces for the remote shell, and without quoting
    # the braces/quotes in the JSON get re-tokenized and kubectl sees
    # `data:` as a separate resource arg ("no need to specify a resource
    # type…" error).
    _kubectl(
        control, "-n", LLAMA_NS, "patch", "configmap", LLAMA_MODEL_CONFIGMAP,
        "--type", "merge", "-p", json.dumps(patch),
        check=True,
    )


def _llama_pvc_for(role: str) -> str:
    return "llama-chat-models" if role == "chat" else "llama-embed-models"


def _llama_deployment_for(role: str) -> str:
    return "llama-chat" if role == "chat" else "llama-embed"


@llama_app.command("list")
def llama_list(
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Show the active chat + embed models and files on the PVCs."""
    if control is None:
        control = _get_control_host()

    data = _llama_read_config(control)
    if not data:
        console.print(
            f"[red]No llama.cpp ConfigMaps found in {LLAMA_NS}.[/red]\n"
            f"Expected {LLAMA_DEFAULTS_CONFIGMAP} (from Argo) + "
            f"{LLAMA_MODEL_CONFIGMAP} (from `llama setup`)."
        )
        raise typer.Exit(1)
    if not data.get("CHAT_MODEL_FILE"):
        console.print(
            "[yellow]No chat model configured yet.[/yellow] "
            f"Run [cyan]cluster_manager.py llama setup[/cyan] to populate "
            f"{LLAMA_MODEL_CONFIGMAP}."
        )

    # Distinguish imperative (llama-cpp-model) from defaulted (llama-cpp-
    # defaults) values — gives the operator a quick sense of which knobs
    # they've explicitly set vs which are riding on template defaults.
    model_cm = _kubectl(
        control, "-n", LLAMA_NS, "get", "configmap", LLAMA_MODEL_CONFIGMAP,
        "--ignore-not-found", "-o", "json", capture=True, check=False,
    )
    model_keys: set[str] = set()
    if (model_cm.stdout or "").strip():
        model_keys = set(json.loads(model_cm.stdout).get("data", {}).keys())

    def _mark(key: str) -> str:
        return "[green]set[/green]  " if key in model_keys else "[dim]default[/dim]  "

    console.print(
        f"[bold]Active chat model[/bold] "
        f"[cyan]{data.get('CHAT_MODEL_REPO','?')}/{data.get('CHAT_MODEL_FILE','?')}[/cyan] "
        f"served as [green]{data.get('CHAT_SERVED_MODEL','?')}[/green]"
    )
    console.print("[bold]Chat tunables:[/bold]")
    for key in _LLAMA_CHAT_KEYS:
        if key in ("CHAT_MODEL_REPO", "CHAT_MODEL_FILE", "CHAT_SERVED_MODEL"):
            continue
        val = data.get(key, "?")
        display = val if val else "[dim](empty)[/dim]"
        console.print(f"  {_mark(key)}{key} = {display}")

    console.print(
        f"\n[bold]Active embed model[/bold] "
        f"[cyan]{data.get('EMBED_MODEL_REPO','?')}/{data.get('EMBED_MODEL_FILE','?')}[/cyan] "
        f"served as [green]{data.get('EMBED_SERVED_MODEL','?')}[/green] "
        f"[dim](edit via git — change requires vault reindex)[/dim]"
    )

    for role in ("chat", "embed"):
        deploy = _llama_deployment_for(role)
        console.print(f"\n[bold]{deploy} PVC contents:[/bold]")
        # List /models inside a running pod; if the deployment is down
        # (e.g. mid-rollout), we skip and say so — that's more useful
        # than a kubectl error.
        result = _kubectl(
            control, "-n", LLAMA_NS, "exec", f"deploy/{deploy}", "--",
            "sh", "-c", "ls -lh /models 2>/dev/null | tail -n +2",
            capture=True, check=False,
        )
        out = (result.stdout or "").strip()
        console.print(out if out else "  [dim](deployment not running — skipping disk list)[/dim]")


def _llama_set(control: str, role: str, repo: str, filename: str, served_as: str | None) -> None:
    keys = _LLAMA_CHAT_KEYS if role == "chat" else _LLAMA_EMBED_KEYS
    updates = {
        keys[0]: repo,
        keys[1]: filename,
    }
    if served_as:
        updates[keys[2]] = served_as

    if role == "chat":
        # Chat config lives in llama-cpp-model (imperative). Edits persist.
        # Also keep OpenClaw's active-model ConfigMap in lockstep with the
        # served-as alias — openclaw.json interpolates ${ACTIVE_MODEL}
        # from it, and a mismatch 404s every inference call.
        console.print(f"Patching [cyan]{LLAMA_MODEL_CONFIGMAP}[/cyan]: {updates}")
        _llama_patch_model_config(control, updates)
        if served_as:
            _apply_openclaw_active_model(control, served_as)
    else:
        # Embed config lives in llama-cpp-defaults (Argo-managed). Live
        # patch works for a quick experiment but Argo will revert it on
        # the next sync — warn so the operator knows to also edit git
        # for a durable change. Embed swaps also invalidate the Chroma
        # collection (different vector space) so this is rare anyway.
        console.print(
            f"[yellow]Note:[/yellow] patching [cyan]{LLAMA_DEFAULTS_CONFIGMAP}[/cyan]: {updates}\n"
            f"  Argo will revert this on next sync — for a durable change, "
            f"also edit clusters/default/apps/llama-cpp/configmap.yaml in git.\n"
            f"  Embed dimension change? Remember to `restart --wipe-rag` "
            f"to reindex the vault."
        )
        _kubectl(
            control, "-n", LLAMA_NS, "patch", "configmap", LLAMA_DEFAULTS_CONFIGMAP,
            "--type", "merge", "-p", json.dumps({"data": updates}),
            check=True,
        )

    # Bouncing the deployment triggers the init container, which curls
    # the GGUF from HF to the PVC if absent (first-use for new model) or
    # reuses the cached file (subsequent switches). First pull of a ~10 GB
    # chat model takes minutes over a home connection — tail `kubectl
    # logs -f deploy/llama-chat -c pull-model` to watch progress.
    deploy = _llama_deployment_for(role)
    console.print(f"Restarting [cyan]{deploy}[/cyan]")
    _restart_deployment(control, LLAMA_NS, deploy)
    if role == "chat":
        # OpenClaw reads active-model at init-container time; restart to
        # pick up a name change on its side too.
        _restart_deployment(control, "openclaw", "openclaw")
    console.print(
        f"[green]Done.[/green] Pod will download the GGUF (if not cached) and reload."
    )


def _apply_openclaw_active_model(control: str, served_as: str) -> None:
    """Write the openclaw-model ConfigMap so openclaw.json's
    ${ACTIVE_MODEL} interpolation matches the chat server's --alias.
    """
    _ssh(control,
        f"sudo k3s kubectl -n openclaw create configmap openclaw-model"
        f" --from-literal=active-model={_q(served_as)}"
        f" --dry-run=client -o yaml"
        f" | sudo k3s kubectl apply -f -",
        capture=True,
    )


# Defaults offered by `llama setup` on a fresh deployment. Sensible
# for a 16 GB-class consumer GPU (5080, 4080, 3080/3090) running a 14B
# chat model all on the card. Values not in llama-cpp-defaults (repo/
# file/alias) are prompted without a baseline suggestion; tunables
# (ctx/ngl/etc.) fall back to the git defaults if the operator just
# hits Enter.
_LLAMA_SETUP_DEFAULTS = {
    "CHAT_MODEL_REPO":     "bartowski/Qwen_Qwen3-14B-GGUF",
    "CHAT_MODEL_FILE":     "Qwen_Qwen3-14B-Q5_K_M.gguf",
    "CHAT_SERVED_MODEL":   "qwen3-14b",
    "CHAT_CTX_SIZE":       "32768",
    "CHAT_GPU_LAYERS":     "999",
    "CHAT_PARALLEL_SLOTS": "1",
    "CHAT_KV_TYPE":        "q8_0",
    "CHAT_FLASH_ATTN":     "on",
    "CHAT_CPU_MOE":        "off",
    "CHAT_N_CPU_MOE":      "0",
    "CHAT_OVERRIDE_TENSOR": "",
    "CHAT_EXTRA_FLAGS":    "",
}


@llama_app.command("setup")
def llama_setup(
    repo: str = typer.Option(None, "--repo"),
    filename: str = typer.Option(None, "--file"),
    served_as: str = typer.Option(None, "--served-as"),
    ctx: str = typer.Option(None, "--ctx", help="Context size in tokens."),
    ngl: str = typer.Option(None, "--ngl", help="GPU layer offload count (999 = all)."),
    parallel: str = typer.Option(None, "--parallel", help="Concurrent request slots."),
    kv_type: str = typer.Option(None, "--kv-type", help=f"One of {list(_VALID_KV_TYPES)}."),
    flash_attn: str = typer.Option(None, "--flash-attn", help=f"One of {list(_VALID_FLASH_ATTN)}."),
    cpu_moe: str = typer.Option(None, "--cpu-moe", help=f"Offload MoE experts to CPU/RAM. One of {list(_VALID_CPU_MOE)}."),
    n_cpu_moe: str = typer.Option(None, "--n-cpu-moe", help="Partial MoE offload: first N expert layers to CPU (0 = off)."),
    override_tensor: str = typer.Option(None, "--override-tensor", help="Tensor placement regex (advanced)."),
    extra_flags: str = typer.Option(None, "--flags", help="Extra llama-server flags."),
    restart: bool = typer.Option(True, "--restart/--no-restart"),
    force: bool = typer.Option(False, "--force", help="Re-prompt for every key, even those already set."),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """First-time + reconfigure of the llama-chat model.

    Writes two ConfigMaps, both imperatively managed (not in git, not
    reconciled by Argo):
      - llama-cpp/llama-cpp-model: CHAT_* env keys the llama-chat pod
        reads via envFrom. Model repo/file/alias plus every tunable
        (ctx, ngl, parallel, kv-type, flash-attn, extra flags).
      - openclaw/openclaw-model: active-model key interpolated into
        openclaw.json so OpenClaw picks the same id the chat server
        advertises under /v1/models.

    Prompts for every chat knob in order; Enter accepts the current
    value (or default if unset). Pass CLI flags for any subset to skip
    those prompts; pass --force to re-prompt even for keys already set.
    For a single-knob tweak that doesn't need the full walk, use
    `llama set-ctx`, `llama set-ngl`, `llama set-kv-type`, etc.
    """
    if control is None:
        control = _get_control_host()

    existing = _llama_read_config(control)
    existing_active = _kubectl(
        control, "-n", "openclaw", "get", "configmap", "openclaw-model",
        "--ignore-not-found", "-o", "jsonpath={.data.active-model}",
        capture=True, check=False,
    ).stdout.strip()

    cli_overrides = {
        "CHAT_MODEL_REPO":     repo,
        "CHAT_MODEL_FILE":     filename,
        "CHAT_SERVED_MODEL":   served_as,
        "CHAT_CTX_SIZE":       ctx,
        "CHAT_GPU_LAYERS":     ngl,
        "CHAT_PARALLEL_SLOTS": parallel,
        "CHAT_KV_TYPE":        kv_type,
        "CHAT_FLASH_ATTN":     flash_attn,
        "CHAT_CPU_MOE":        cpu_moe,
        "CHAT_N_CPU_MOE":      n_cpu_moe,
        "CHAT_OVERRIDE_TENSOR": override_tensor,
        "CHAT_EXTRA_FLAGS":    extra_flags,
    }

    def _resolve(key: str) -> str:
        override = cli_overrides.get(key)
        if override is not None:
            _validate_chat_field(key, override)
            return override
        current = existing.get(key, "")
        # Already-set keys skipped unless --force; makes re-running with
        # one CLI override fast (only missing keys prompt).
        if current and not force:
            return current
        default = current or _LLAMA_SETUP_DEFAULTS.get(key, "")
        answer = typer.prompt(_LLAMA_CHAT_PROMPTS[key], default=default)
        _validate_chat_field(key, answer)
        return answer

    # Iterate in the canonical order so the prompts feel coherent.
    values = {k: _resolve(k) for k in _LLAMA_CHAT_KEYS}

    console.print(f"\nWriting [cyan]{LLAMA_NS}/{LLAMA_MODEL_CONFIGMAP}[/cyan]:")
    for k in _LLAMA_CHAT_KEYS:
        console.print(f"  {k}={values[k]}")
    _llama_patch_model_config(control, values)

    if existing_active != values["CHAT_SERVED_MODEL"]:
        console.print(
            f"Writing [cyan]openclaw/openclaw-model[/cyan]: "
            f"active-model={values['CHAT_SERVED_MODEL']}"
        )
        _apply_openclaw_active_model(control, values["CHAT_SERVED_MODEL"])

    if restart:
        console.print("Restarting [cyan]llama-chat[/cyan] and [cyan]openclaw[/cyan]")
        _restart_deployment(control, LLAMA_NS, "llama-chat")
        _restart_deployment(control, "openclaw", "openclaw")

    console.print("[green]Done.[/green]")


def _suggest_served_as(filename: str) -> str:
    """Best-effort derive a served-as model id from a GGUF filename.

    Strips the GGUF extension, a leading "Org_" packaging prefix, and
    any trailing quant suffix (-Q5_K_M, -IQ4_XS, .Q8_0, etc.). Lowercases.
    Returns '' if nothing usable remains.

    Examples:
      Qwen3.6-27B-IQ4_XS.gguf            -> qwen3.6-27b
      Qwen_Qwen3-14B-Q5_K_M.gguf         -> qwen3-14b
      DeepSeek-R1-Distill-Qwen-14B-Q5_K_M.gguf -> deepseek-r1-distill-qwen-14b
    """
    name = filename.rsplit("/", 1)[-1]
    if name.endswith(".gguf"):
        name = name[:-5]
    # Strip "Org_" repackaging prefix (e.g., "Qwen_Qwen3-14B" -> "Qwen3-14B").
    # Only if the prefix is a single capitalized word — avoids stripping
    # legit hyphenated family names.
    if "_" in name:
        first, rest = name.split("_", 1)
        if first.isalpha() and first[0].isupper() and rest[:1].isalpha():
            name = rest
    # Drop trailing quant suffix (matches both -Q4_K_M and .Q8_0 etc.).
    name = re.sub(
        r"[-\._](IQ|Q)\d[_.]?[A-Z0-9_]*$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    return name.lower()


@llama_app.command("set-chat")
def llama_set_chat(
    repo: str = typer.Argument(..., help="HuggingFace repo, e.g. bartowski/Qwen_Qwen3-14B-GGUF"),
    filename: str = typer.Argument(..., help="GGUF filename inside the repo."),
    served_as: str = typer.Option(
        None, "--served-as",
        help="Id exposed via /v1/models (OpenClaw picks models by this id). "
             "If omitted AND the model is changing, you'll be prompted.",
    ),
    ctx: str = typer.Option(None, "--ctx", help="Context size in tokens."),
    ngl: str = typer.Option(None, "--ngl", help="GPU layer offload count (999 = all)."),
    parallel: str = typer.Option(None, "--parallel", help="Concurrent request slots."),
    kv_type: str = typer.Option(None, "--kv-type", help=f"One of {list(_VALID_KV_TYPES)}."),
    flash_attn: str = typer.Option(None, "--flash-attn", help=f"One of {list(_VALID_FLASH_ATTN)}."),
    cpu_moe: str = typer.Option(None, "--cpu-moe", help=f"Offload MoE experts to CPU/RAM. One of {list(_VALID_CPU_MOE)}."),
    n_cpu_moe: str = typer.Option(None, "--n-cpu-moe", help="Partial MoE offload: first N expert layers to CPU (0 = off)."),
    override_tensor: str = typer.Option(None, "--override-tensor", help="Tensor placement regex (advanced)."),
    flags: str = typer.Option(None, "--flags", help="Replace CHAT_EXTRA_FLAGS (escape hatch)."),
    keep_alias: bool = typer.Option(
        False, "--keep-alias",
        help="Explicitly keep the current --served-as alias across a model "
             "change. Suppresses the served-as mismatch prompt. Use when "
             "you're swapping variants within a family (e.g. bartowski's "
             "Q5 -> unsloth's Q5 of the same base model) and want to keep "
             "the existing alias for API-client stability.",
    ),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Swap chat model and optionally tune knobs in one shot.

    Required positional args — repo + filename — are what defines the
    switch. Every `--<knob>` option is optional; anything omitted keeps
    the current ConfigMap value. One write, one restart.

    If you change the model but don't pass --served-as, you'll get a
    warning + prompt to update the alias — OpenClaw otherwise keeps
    reporting the old model id even though the weights have changed.
    Pass --keep-alias to suppress that prompt for intentional
    within-family variant swaps.

    Examples:
      # Pure model swap + alias update (prompted)
      llama set-chat bartowski/Qwen3.6-27B-GGUF Qwen3.6-27B-IQ4_XS.gguf

      # Swap and retune for partial offload at longer context
      llama set-chat <repo> <file> --ctx 16384 --ngl 52 --kv-type q4_0
    """
    if control is None:
        control = _get_control_host()

    # Build the patch from required + any --<knob>s that were given.
    updates: dict[str, str] = {"CHAT_MODEL_REPO": repo, "CHAT_MODEL_FILE": filename}
    opt_map = {
        "CHAT_SERVED_MODEL":   served_as,
        "CHAT_CTX_SIZE":       ctx,
        "CHAT_GPU_LAYERS":     ngl,
        "CHAT_PARALLEL_SLOTS": parallel,
        "CHAT_KV_TYPE":        kv_type,
        "CHAT_FLASH_ATTN":     flash_attn,
        "CHAT_CPU_MOE":        cpu_moe,
        "CHAT_N_CPU_MOE":      n_cpu_moe,
        "CHAT_OVERRIDE_TENSOR": override_tensor,
        "CHAT_EXTRA_FLAGS":    flags,
    }
    for key, val in opt_map.items():
        if val is not None:
            _validate_chat_field(key, val)
            updates[key] = val

    # Model changed + alias not explicitly updated = likely mistake.
    # OpenClaw keeps reporting the old id under /v1/models, and chat
    # requests silently route to the new weights — functional but
    # misleading. Warn + prompt for an updated alias.
    if served_as is None and not keep_alias:
        existing = _llama_read_config(control)
        current_repo = existing.get("CHAT_MODEL_REPO", "")
        current_file = existing.get("CHAT_MODEL_FILE", "")
        current_served = existing.get("CHAT_SERVED_MODEL", "")
        if (repo != current_repo or filename != current_file) and current_served:
            suggestion = _suggest_served_as(filename) or current_served
            console.print(
                f"[yellow]⚠ Model is changing but --served-as was not set.[/yellow]\n"
                f"  Current alias: [cyan]{current_served}[/cyan]\n"
                f"  New model:     [cyan]{filename}[/cyan]\n"
                f"  Without an alias update, OpenClaw + /v1/models will keep "
                f"reporting [cyan]{current_served}[/cyan] even though the "
                f"weights have switched."
            )
            if typer.confirm("Update alias?", default=True):
                new_served = typer.prompt("New served-as", default=suggestion)
                _validate_chat_field("CHAT_SERVED_MODEL", new_served)
                updates["CHAT_SERVED_MODEL"] = new_served

    console.print(f"Patching [cyan]{LLAMA_MODEL_CONFIGMAP}[/cyan]:")
    for k, v in updates.items():
        console.print(f"  {k}={v}")
    _llama_patch_model_config(control, updates)
    if "CHAT_SERVED_MODEL" in updates:
        _apply_openclaw_active_model(control, updates["CHAT_SERVED_MODEL"])

    console.print("Restarting [cyan]llama-chat[/cyan]")
    _restart_deployment(control, LLAMA_NS, "llama-chat")
    if "CHAT_SERVED_MODEL" in updates:
        _restart_deployment(control, "openclaw", "openclaw")
    console.print(
        "[green]Done.[/green] Pod will download the GGUF (if not cached) and reload."
    )


def _llama_set_single(control: str, key: str, value: str) -> None:
    """Shared path for the per-knob setters. Validate, patch, restart."""
    _validate_chat_field(key, value)
    console.print(f"Patching [cyan]{LLAMA_MODEL_CONFIGMAP}[/cyan]: {key}={value}")
    _llama_patch_model_config(control, {key: value})
    _restart_deployment(control, LLAMA_NS, "llama-chat")
    console.print("[green]Done.[/green] llama-chat restarting.")


@llama_app.command("set-ctx")
def llama_set_ctx(
    tokens: int = typer.Argument(..., help="Context size in tokens (e.g. 32768)."),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Change CHAT_CTX_SIZE without touching the model or other knobs."""
    if control is None:
        control = _get_control_host()
    _llama_set_single(control, "CHAT_CTX_SIZE", str(tokens))


@llama_app.command("set-ngl")
def llama_set_ngl(
    layers: int = typer.Argument(..., help="GPU layer offload count (999 = all)."),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Change CHAT_GPU_LAYERS — the main knob for fitting a bigger model
    in less VRAM by offloading N layers to CPU.
    """
    if control is None:
        control = _get_control_host()
    _llama_set_single(control, "CHAT_GPU_LAYERS", str(layers))


@llama_app.command("set-parallel")
def llama_set_parallel(
    slots: int = typer.Argument(..., help="Concurrent request slots."),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Change CHAT_PARALLEL_SLOTS — how many requests can decode at once.
    llama.cpp DIVIDES the total ctx across slots (per-request ctx =
    CHAT_CTX_SIZE / CHAT_PARALLEL_SLOTS), so bumping this shrinks the
    effective per-request context. Default is 1 (full ctx per request);
    raise only if you need to serve concurrent clients AND your prompts
    fit in the smaller per-slot window.
    """
    if control is None:
        control = _get_control_host()
    _llama_set_single(control, "CHAT_PARALLEL_SLOTS", str(slots))


@llama_app.command("set-kv-type")
def llama_set_kv_type(
    kv_type: str = typer.Argument(..., help=f"One of {list(_VALID_KV_TYPES)}."),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Change CHAT_KV_TYPE — KV cache dtype. Smaller types buy context
    size at a small quality cost. q8_0 is the standard-issue pick.
    """
    if control is None:
        control = _get_control_host()
    _llama_set_single(control, "CHAT_KV_TYPE", kv_type)


@llama_app.command("set-flash-attn")
def llama_set_flash_attn(
    mode: str = typer.Argument(..., help=f"One of {list(_VALID_FLASH_ATTN)}."),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Change CHAT_FLASH_ATTN — usually 'on' on Blackwell/Ada,
    'off' or 'auto' on older cards with partial FA support.
    """
    if control is None:
        control = _get_control_host()
    _llama_set_single(control, "CHAT_FLASH_ATTN", mode)


@llama_app.command("set-cpu-moe")
def llama_set_cpu_moe(
    mode: str = typer.Argument(..., help=f"One of {list(_VALID_CPU_MOE)}."),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Toggle --cpu-moe — pin MoE expert tensors to host RAM instead of VRAM.
    `on` for MoE models on a GPU that can't hold all experts (e.g. Qwen3-A3B
    on a 16 GB card); `off` for dense models that fit on the GPU.
    """
    if control is None:
        control = _get_control_host()
    _llama_set_single(control, "CHAT_CPU_MOE", mode)


@llama_app.command("set-n-cpu-moe")
def llama_set_n_cpu_moe(
    n: int = typer.Argument(..., help="Number of MoE expert layers to pin to CPU (0 = off)."),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Set --n-cpu-moe — partial MoE offload, first N expert layers to CPU.
    Use to split expert weights between GPU and CPU when full --cpu-moe
    leaves VRAM headroom on the table. 0 disables. Mutually exclusive with
    set-cpu-moe on (the latter wins if both are set).
    """
    if control is None:
        control = _get_control_host()
    _llama_set_single(control, "CHAT_N_CPU_MOE", str(n))


@llama_app.command("set-override-tensor")
def llama_set_override_tensor(
    regex: str = typer.Argument(..., help="Tensor placement regex; empty string clears."),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Set --override-tensor — fine-grained control over which tensors live
    on which device. Reach for this when --cpu-moe's blanket pattern is too
    coarse (e.g. keep first N expert layers on GPU, rest on CPU). Pass an
    empty string to clear.
    """
    if control is None:
        control = _get_control_host()
    _llama_set_single(control, "CHAT_OVERRIDE_TENSOR", regex)


@llama_app.command("set-flags")
def llama_set_flags(
    flags: str = typer.Argument(..., help="Full replacement for CHAT_EXTRA_FLAGS."),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Replace CHAT_EXTRA_FLAGS — the escape hatch for any llama-server
    flag not yet promoted to a named field (--rope-*, sampling defaults,
    etc.).
    """
    if control is None:
        control = _get_control_host()
    _llama_set_single(control, "CHAT_EXTRA_FLAGS", flags)


@llama_app.command("set-embed")
def llama_set_embed(
    repo: str = typer.Argument(..., help="HuggingFace repo, e.g. nomic-ai/nomic-embed-text-v1.5-GGUF"),
    filename: str = typer.Argument(..., help="GGUF filename, e.g. nomic-embed-text-v1.5.Q8_0.gguf"),
    served_as: str = typer.Option(
        None, "--served-as",
        help="Name to expose via /v1/models. Must match EMBED_MODEL env in "
             "the rag-indexer/rag-mcp deployments, or embeddings calls 404.",
    ),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Switch the llama-embed model. Restarts llama-embed deployment.

    Changing embed models means the vector space changes — existing
    Chroma vectors become meaningless. If you're swapping to a model
    with a different output dimension (e.g. 768 → 1024), also re-index
    the vault: `cluster-manager restart --wipe-rag`.
    """
    if control is None:
        control = _get_control_host()
    _llama_set(control, "embed", repo, filename, served_as)


@llama_app.command("logs")
def llama_logs(
    role: str = typer.Argument(..., help="chat or embed"),
    container: str = typer.Option(
        "llama-server", "--container",
        help="'llama-server' (default) or 'pull-model' for the init container.",
    ),
    follow: bool = typer.Option(False, "-f", "--follow"),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Tail logs of the llama-chat or llama-embed pod."""
    if role not in ("chat", "embed"):
        console.print("[red]role must be 'chat' or 'embed'[/red]")
        raise typer.Exit(2)
    if control is None:
        control = _get_control_host()
    deploy = _llama_deployment_for(role)
    args = ["-n", LLAMA_NS, "logs", f"deploy/{deploy}", "-c", container]
    if follow:
        args.append("-f")
    subprocess.run(
        ["ssh", control, "sudo", "k3s", "kubectl"] + args,
        check=False,
    )


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

    git@github.com:you/my-private-apps.git  →  my-private-apps
    ssh://git@host/org/repo.git             →  repo
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
        # Read the Application as JSON and parse in Python. Using `-o jsonpath`
        # with a `|` separator doesn't survive: `_kubectl` passes args to
        # `ssh host arg1 arg2...`, SSH joins them with spaces into a single
        # remote-shell command, and the shell interprets `|` as a pipe
        # operator — swallowing kubectl's output.
        app_result = _kubectl(
            control, "-n", "argocd", "get", "application", f"{name}-root",
            "--ignore-not-found", "-o", "json",
            capture=True,
        )
        stdout = (app_result.stdout or "").strip()
        if not stdout:
            entries.append({
                "project": name, "repo_url": "?", "sync": "Unknown", "health": "Unknown",
            })
            continue
        data = json.loads(stdout)
        spec_source = data.get("spec", {}).get("source", {}) or {}
        status = data.get("status", {}) or {}
        entries.append({
            "project": name,
            "repo_url": spec_source.get("repoURL") or "?",
            "sync": (status.get("sync") or {}).get("status") or "Unknown",
            "health": (status.get("health") or {}).get("status") or "Unknown",
        })
    return entries


def _assert_stored_secret_field(
    control: str, *, namespace: str, name: str, field: str, expected: str, context: str,
) -> None:
    """Read a stringData field back out of a just-applied Secret and verify the
    stored value matches the submitted value. Secrets store stringData as
    base64 in .data — decode before compare.

    Fails the whole command with a loud diagnostic if they differ. This
    catches the class of bugs where something between our apply and the
    stored bytes (admission webhook, YAML parser quirk, silent rewrite)
    mangles the value.
    """
    result = _kubectl(
        control, "-n", namespace, "get", "secret", name,
        "-o", f"jsonpath={{.data.{field}}}",
        capture=True, check=True,
    )
    raw = (result.stdout or "").strip()
    if not raw:
        console.print(f"[red]Could not read back {context}/{name} field {field!r}[/red]")
        raise typer.Exit(2)
    stored = base64.b64decode(raw).decode()
    if stored != expected:
        console.print(f"[red]Invariant violation: {context}/{name} stored {field} ≠ submitted[/red]")
        console.print(f"  submitted: {expected!r}")
        console.print(f"  stored:    {stored!r}")
        raise typer.Exit(2)


def _assert_stored_app_field(
    control: str, *, namespace: str, name: str, jsonpath: str, expected: str, context: str,
) -> None:
    """Same invariant check for non-Secret resources (no base64 decode)."""
    result = _kubectl(
        control, "-n", namespace, "get", "application", name,
        "-o", f"jsonpath={jsonpath}",
        capture=True, check=True,
    )
    stored = (result.stdout or "").strip()
    if stored != expected:
        console.print(f"[red]Invariant violation: {context}/{name} stored {jsonpath} ≠ submitted[/red]")
        console.print(f"  submitted: {expected!r}")
        console.print(f"  stored:    {stored!r}")
        raise typer.Exit(2)


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
    # Shape invariant: the scp-like form must have a colon after the host
    # (git@HOST:PATH), or the URL must be an explicit ssh:// URL. This catches
    # args like "git@github.com/owner/repo" where the colon was already lost
    # by some upstream step.
    if not re.match(r"^(git@[\w.\-]+:.+|ssh://.+)$", repo_url):
        console.print(f"[red]--repo-url must be 'git@HOST:PATH' or 'ssh://...'. Got: {repo_url!r}[/red]")
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
    _assert_stored_secret_field(
        control, namespace="argocd", name=secret_name,
        field="url", expected=repo_url, context="Secret",
    )

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
    _assert_stored_app_field(
        control, namespace="argocd", name=f"{project_name}-root",
        jsonpath="{.spec.source.repoURL}", expected=repo_url,
        context="Application",
    )

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


# ---------------------------------------------------------------------------
# Generic repo-secret registration (for child Argo Applications that reference
# additional private repos beyond the root private-apps repo).
# ---------------------------------------------------------------------------

def _ensure_ssh_deploy_key(ssh_key_path: Path | None, name: str) -> tuple[Path, str, str]:
    """Generate (or reuse) an ed25519 deploy keypair for Argo.

    Returns (path, pub_key, private_key). Keypair lives at
    ~/.ssh/argocd-<name>.{,.pub} unless an explicit path is supplied.
    """
    if ssh_key_path is None:
        ssh_key_path = Path.home() / ".ssh" / f"argocd-{name}.key"
    pub_key_path = ssh_key_path.with_suffix(ssh_key_path.suffix + ".pub")

    if not ssh_key_path.exists():
        ssh_key_path.parent.mkdir(mode=0o700, exist_ok=True)
        console.print(f"Generating SSH deploy key at [cyan]{ssh_key_path}[/cyan]")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "",
             "-C", f"argocd-{name}@{_get_repo_url().rsplit('/', 1)[-1]}",
             "-f", str(ssh_key_path)],
            check=True,
        )
    else:
        console.print(f"  [dim]Reusing existing key at {ssh_key_path}[/dim]")

    return ssh_key_path, pub_key_path.read_text().strip(), ssh_key_path.read_text()


def _prompt_for_deploy_key(pub_key: str, repo_url: str) -> None:
    """Print the public key + where to add it, then block on Enter."""
    console.print()
    console.print("[bold]Add this as a deploy key (read-only) on your repo:[/bold]")
    console.print()
    console.print(f"  [cyan]{pub_key}[/cyan]")
    console.print()
    gh_url = repo_url.replace("git@github.com:", "https://github.com/").replace(".git", "")
    console.print(f"  GitHub UI: {gh_url}/settings/keys/new")
    console.print()
    typer.prompt("Press Enter once the deploy key is added", default="", show_default=False)


_REPO_URL_RE = re.compile(r"^(git@[\w.\-]+:.+|ssh://.+)$")


def _find_repo_secret_by_url(control: str, repo_url: str) -> str | None:
    """Return the name of a Repository Secret whose stored `url` matches, or None.

    Only looks at Secrets labeled `cluster-manager/repo-secret=true` — i.e., those
    created by this CLI. Prevents false positives against hand-rolled repo creds.
    """
    r = _kubectl(
        control, "-n", "argocd", "get", "secrets",
        "-l", "cluster-manager/repo-secret=true",
        "-o", "json", capture=True, check=True,
    )
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    for item in data.get("items", []):
        stored_url_b64 = item.get("data", {}).get("url")
        if not stored_url_b64:
            continue
        try:
            stored_url = base64.b64decode(stored_url_b64).decode().strip()
        except (ValueError, UnicodeDecodeError):
            continue
        if stored_url == repo_url:
            return item.get("metadata", {}).get("name")
    return None


def _register_repo(
    control: str,
    repo_url: str,
    *,
    name: str | None = None,
    project: str = "*",
    ssh_key_path: Path | None = None,
) -> bool:
    """Register a repo with Argo CD. Returns True if newly registered, False if already present.

    Idempotent short-circuit: if a labeled Repository Secret already stores this
    exact URL, return False without generating a key or prompting the user.
    """
    if not _REPO_URL_RE.match(repo_url):
        raise typer.BadParameter(f"repo-url must be 'git@HOST:PATH' or 'ssh://...'. Got: {repo_url!r}")

    existing = _find_repo_secret_by_url(control, repo_url)
    if existing:
        console.print(f"  [dim]Repo [cyan]{repo_url}[/cyan] already registered as Secret [cyan]{existing}[/cyan] — skipping.[/dim]")
        return False

    if name is None:
        name = f"{_derive_project_name(repo_url)}-repo"
        console.print(f"  [dim]Derived Secret name: [cyan]{name}[/cyan][/dim]")

    _, pub_key, private_key = _ensure_ssh_deploy_key(ssh_key_path, name.removesuffix("-repo"))
    _prompt_for_deploy_key(pub_key, repo_url)

    indented_key = "\n".join("    " + line for line in private_key.splitlines())
    url_yaml = json.dumps(repo_url)
    project_yaml = json.dumps(project)
    repo_secret = f"""apiVersion: v1
kind: Secret
metadata:
  name: {name}
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
    cluster-manager/repo-secret: "true"
stringData:
  type: git
  url: {url_yaml}
  project: {project_yaml}
  sshPrivateKey: |
{indented_key}
"""
    console.print(f"Applying Repository Secret [cyan]{name}[/cyan]")
    _apply_yaml(control, repo_secret)
    _assert_stored_secret_field(
        control, namespace="argocd", name=name,
        field="url", expected=repo_url, context="Repository Secret",
    )
    console.print(
        f"[green]✓[/green] Registered repo [cyan]{repo_url}[/cyan] as "
        f"Secret [cyan]{name}[/cyan] (project [cyan]{project}[/cyan])."
    )
    return True


@app.command("add-repo-secret")
def add_repo_secret(
    repo_url: str = typer.Option(..., "--repo-url", help="SSH URL of the repo to register."),
    ssh_key_path: Path = typer.Option(
        None, "--ssh-key-path",
        help="Path for the SSH deploy key. Default: ~/.ssh/argocd-<name>.key",
    ),
    name: str = typer.Option(
        None, "--name",
        help="Repository Secret name. Default: <repo-slug>-repo.",
    ),
    project: str = typer.Option(
        "*", "--project",
        help="Argo AppProject that may use this repo. Default: * (any).",
    ),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Register a git repository with Argo CD as a Repository Secret.

    Use this when a child Argo Application needs to clone a private repo
    besides the root private-apps repo — e.g., a private application repo
    whose Helm chart is deployed from a separate location.

    Typically invoked indirectly via `app-provision`. Use this standalone form
    for repos that don't have a per-app provisioning manifest.
    """
    if control is None:
        control = _get_control_host()
    _register_repo(control, repo_url, name=name, project=project, ssh_key_path=ssh_key_path)


# ---------------------------------------------------------------------------
# Garage S3 provisioning — buckets + access key + k8s Secret for an app.
# ---------------------------------------------------------------------------

_KEY_ID_RE = re.compile(r"Key ID:\s*(\S+)")
_SECRET_RE = re.compile(r"Secret key:\s*(\S+)")
_KEY_LIST_LINE_RE = re.compile(r"^\s*(GK\S+)\s+(\S+)\s*$")


def _parse_garage_key_info(stdout: str) -> tuple[str, str]:
    key_match = _KEY_ID_RE.search(stdout)
    sec_match = _SECRET_RE.search(stdout)
    if not key_match or not sec_match:
        raise RuntimeError(
            f"Could not parse key ID / secret from `garage key info --show-secret` output:\n{stdout}"
        )
    return key_match.group(1), sec_match.group(1)


def _garage_list_keys(control: str) -> list[tuple[str, str]]:
    """Return [(access_key_id, name), ...] parsed from `garage key list`.

    Garage allows duplicate key names, so we always use the access-key ID for
    disambiguation once we've picked which key to work with.
    """
    r = _kubectl(
        control, "-n", "garage", "exec", "garage-0", "--",
        "/garage", "key", "list", capture=True, check=True,
    )
    keys: list[tuple[str, str]] = []
    for line in (r.stdout or "").splitlines():
        m = _KEY_LIST_LINE_RE.match(line)
        if m:
            keys.append((m.group(1), m.group(2)))
    return keys


def _provision_s3_app_creds(
    control: str,
    *,
    key_name: str,
    namespace: str,
    secret_name: str,
    buckets: list[str],
) -> None:
    """Provision Garage buckets + access key + k8s Secret. Idempotent.

    Buckets that exist are reused. A key named `key_name` is looked up by name
    first (Garage allows duplicate names, so we list + filter rather than
    trusting `key create` to be a no-op). If exactly one exists, we reuse its
    access-key ID; if zero, we create one; if >1, we fail loudly with cleanup
    commands rather than pick an arbitrary duplicate. After that point every
    `--key` reference uses the ID, not the name, so ambiguity can't resurface.
    """
    _ensure_namespace(control, namespace)

    for bucket in buckets:
        console.print(f"Ensuring bucket [cyan]{bucket}[/cyan]")
        r = _kubectl(
            control, "-n", "garage", "exec", "garage-0", "--",
            "/garage", "bucket", "create", bucket, capture=True,
        )
        combined = (r.stderr or "") + (r.stdout or "")
        if r.returncode != 0 and "already exists" not in combined.lower():
            console.print(f"[red]bucket create failed: {combined.strip()}[/red]")
            raise typer.Exit(1)

    matching = [(kid, kname) for (kid, kname) in _garage_list_keys(control) if kname == key_name]
    if len(matching) > 1:
        console.print(
            f"[red]Garage has {len(matching)} keys named [cyan]{key_name}[/cyan]. "
            "Delete the extras before re-running (they can't all be the one the Secret points to):[/red]"
        )
        for kid, _ in matching:
            console.print(
                f"  sudo k3s kubectl -n garage exec garage-0 -- /garage key delete --yes {kid}"
            )
        console.print(
            "[dim]If none of those IDs are attached to buckets yet, it's safe to delete all of them — "
            "the next app-provision run will create a fresh single key.[/dim]"
        )
        raise typer.Exit(1)

    if not matching:
        console.print(f"Creating access key [cyan]{key_name}[/cyan]")
        _kubectl(
            control, "-n", "garage", "exec", "garage-0", "--",
            "/garage", "key", "create", key_name, check=True,
        )
        matching = [(kid, kname) for (kid, kname) in _garage_list_keys(control) if kname == key_name]
        if len(matching) != 1:
            console.print(f"[red]Expected exactly one key named {key_name} after create, got {len(matching)}[/red]")
            raise typer.Exit(1)
    else:
        console.print(f"  [dim]Reusing existing key [cyan]{matching[0][0]}[/cyan] named [cyan]{key_name}[/cyan][/dim]")

    key_id = matching[0][0]

    for bucket in buckets:
        _kubectl(
            control, "-n", "garage", "exec", "garage-0", "--",
            "/garage", "bucket", "allow",
            "--read", "--write", "--owner",
            "--key", key_id, bucket, check=True,
        )

    r = _kubectl(
        control, "-n", "garage", "exec", "garage-0", "--",
        "/garage", "key", "info", key_id, "--show-secret",
        capture=True, check=True,
    )
    access_key_id, secret_access_key = _parse_garage_key_info(r.stdout or "")

    console.print(f"Writing Secret [cyan]{namespace}/{secret_name}[/cyan]")
    _ssh(
        control,
        f"sudo k3s kubectl -n {_q(namespace)} create secret generic {_q(secret_name)}"
        f" --from-literal=accessKeyId={_q(access_key_id)}"
        f" --from-literal=secretAccessKey={_q(secret_access_key)}"
        f" --dry-run=client -o yaml"
        f" | sudo k3s kubectl apply -f -",
        check=True,
    )

    console.print(f"[green]✓[/green] Secret [cyan]{namespace}/{secret_name}[/cyan] with buckets [cyan]{', '.join(buckets)}[/cyan]")


@app.command("provision-s3-app")
def provision_s3_app(
    app_name: str = typer.Option(..., "--app", help="App name. Used for key name and default Secret name."),
    namespace: str = typer.Option(..., "--namespace", "-n", help="Target namespace for the k8s Secret."),
    buckets: str = typer.Option(..., "--buckets", help="Comma-separated bucket names."),
    secret_name: str = typer.Option(
        None, "--secret-name",
        help="k8s Secret name for the generated credentials. Default: <app>-s3",
    ),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Provision Garage buckets + access key + k8s Secret for one app.

    Typically invoked indirectly via `app-provision`. Use this standalone form
    for one-off provisioning outside a per-app manifest.
    """
    if control is None:
        control = _get_control_host()

    if secret_name is None:
        secret_name = f"{app_name}-s3"

    bucket_list = [b.strip() for b in buckets.split(",") if b.strip()]
    if not bucket_list:
        console.print("[red]--buckets is empty[/red]")
        raise typer.Exit(1)

    _provision_s3_app_creds(
        control,
        key_name=f"{app_name}-key",
        namespace=namespace,
        secret_name=secret_name,
        buckets=bucket_list,
    )


# ---------------------------------------------------------------------------
# Image pull secrets — dockerconfigjson + default SA patch.
# ---------------------------------------------------------------------------

def _resolve_registry_token(registry: str, username: str) -> str:
    """Resolve a registry token without ever writing it to disk.

    Order: $<REGISTRY>_TOKEN env var, then (for ghcr.io only) `gh auth token`,
    then interactive prompt with hidden input.
    """
    env_var = f"{registry.split('.')[0].upper()}_TOKEN"
    token = os.environ.get(env_var)
    if token and token.strip():
        console.print(f"  [dim]Using ${env_var}[/dim]")
        return token.strip()

    if registry == "ghcr.io":
        try:
            r = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, check=True,
            )
            if r.stdout.strip():
                console.print("  [dim]Using `gh auth token`[/dim]")
                return r.stdout.strip()
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

    return typer.prompt(
        f"Personal access token for {username}@{registry} (needs read:packages)",
        hide_input=True,
    ).strip()


def _attach_pull_secret_to_default_sa(control: str, namespace: str, secret_name: str) -> None:
    """Idempotently append secret_name to the default SA's imagePullSecrets.

    Read-modify-write so we don't clobber other pull secrets attached to the SA.
    """
    r = _kubectl(
        control, "-n", namespace, "get", "sa", "default", "-o", "json",
        capture=True, check=True,
    )
    sa = json.loads(r.stdout)
    existing = sa.get("imagePullSecrets") or []
    if any(item.get("name") == secret_name for item in existing):
        console.print(f"  [dim]default SA already references [cyan]{secret_name}[/cyan][/dim]")
        return

    patch = json.dumps({"imagePullSecrets": existing + [{"name": secret_name}]})
    _kubectl(control, "-n", namespace, "patch", "sa", "default", "-p", patch, check=True)
    console.print(f"  [dim]Attached [cyan]{secret_name}[/cyan] to default SA[/dim]")


def _provision_image_pull_secret(
    control: str,
    *,
    namespace: str,
    secret_name: str,
    registry: str,
    username: str,
    token: str,
    patch_default_sa: bool = True,
) -> None:
    """Create a dockerconfigjson Secret and optionally wire it to the default SA.

    Patching the default SA means every Pod in the namespace automatically gets
    the pull secret without its Deployment/CronJob spec having to mention it —
    which keeps the chart backend-agnostic (it doesn't know whether the registry
    is public or private).
    """
    _ensure_namespace(control, namespace)

    auth_b64 = base64.b64encode(f"{username}:{token}".encode()).decode()
    docker_config = {"auths": {registry: {"username": username, "auth": auth_b64}}}
    config_b64 = base64.b64encode(json.dumps(docker_config).encode()).decode()

    secret_yaml = f"""apiVersion: v1
kind: Secret
metadata:
  name: {secret_name}
  namespace: {namespace}
  labels:
    cluster-manager/image-pull-secret: "true"
type: kubernetes.io/dockerconfigjson
data:
  .dockerconfigjson: {config_b64}
"""
    console.print(f"Writing dockerconfigjson Secret [cyan]{namespace}/{secret_name}[/cyan] for [cyan]{registry}[/cyan]")
    _apply_yaml(control, secret_yaml)

    # Invariant: what we applied came back intact — the registry host round-trips
    # through base64 decoding of .dockerconfigjson. Use -o json + Python parse
    # (not jsonpath) because the key `.dockerconfigjson` starts with a dot and
    # the backslash-escape needed in jsonpath gets eaten by the remote shell
    # when we go through SSH.
    r = _kubectl(
        control, "-n", namespace, "get", "secret", secret_name,
        "-o", "json", capture=True, check=True,
    )
    try:
        secret_obj = json.loads(r.stdout or "{}")
        stored_b64 = (secret_obj.get("data") or {}).get(".dockerconfigjson", "")
        stored = base64.b64decode(stored_b64).decode() if stored_b64 else ""
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        stored = ""
    if registry not in stored:
        console.print(
            f"[red]invariant: stored dockerconfigjson for {namespace}/{secret_name} "
            f"does not contain registry {registry!r}[/red]"
        )
        console.print(f"[red]    stored: {stored!r}[/red]")
        raise typer.Exit(1)

    if patch_default_sa:
        _attach_pull_secret_to_default_sa(control, namespace, secret_name)

    console.print(f"[green]✓[/green] Image pull secret [cyan]{namespace}/{secret_name}[/cyan]")


@app.command("add-image-pull-secret")
def add_image_pull_secret(
    namespace: str = typer.Option(..., "--namespace", "-n"),
    username: str = typer.Option(..., "--username", "-u"),
    registry: str = typer.Option("ghcr.io", "--registry"),
    secret_name: str = typer.Option(
        None, "--secret-name",
        help="Secret name. Default: <registry-short>-pull (e.g. ghcr-pull).",
    ),
    no_patch_default_sa: bool = typer.Option(
        False, "--no-patch-default-sa",
        help="Don't wire the secret into the namespace's default ServiceAccount.",
    ),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Provision an image pull Secret for a private registry.

    Typically invoked indirectly via `app-provision`. Use standalone for
    one-off namespaces outside a per-app manifest.

    Token resolution (no token ever written to disk or argv):
      1. $<REGISTRY>_TOKEN env var (e.g. $GHCR_TOKEN)
      2. `gh auth token` (ghcr.io only, if the gh CLI is authed)
      3. Hidden interactive prompt
    """
    if control is None:
        control = _get_control_host()
    if secret_name is None:
        secret_name = f"{registry.split('.')[0]}-pull"

    token = _resolve_registry_token(registry, username)
    _provision_image_pull_secret(
        control,
        namespace=namespace,
        secret_name=secret_name,
        registry=registry,
        username=username,
        token=token,
        patch_default_sa=not no_patch_default_sa,
    )


# ---------------------------------------------------------------------------
# app-provision — single entry point driven by a per-app manifest.
# ---------------------------------------------------------------------------

_PROVISION_SPEC_KEYS = {"namespace", "repos", "s3", "imagePullSecrets"}


def _resolve_manifest_path(path: Path) -> Path:
    """Accept either a file or a directory containing `provision.yaml`."""
    if path.is_dir():
        candidate = path / "provision.yaml"
        if not candidate.exists():
            console.print(f"[red]{path} is a directory but has no provision.yaml[/red]")
            raise typer.Exit(1)
        return candidate
    if not path.exists():
        console.print(f"[red]Manifest not found: {path}[/red]")
        raise typer.Exit(1)
    return path


def _load_provision_spec(path: Path) -> dict:
    try:
        spec = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        console.print(f"[red]YAML parse error in {path}: {e}[/red]")
        raise typer.Exit(1)
    if not isinstance(spec, dict):
        console.print(f"[red]{path}: top-level document must be a mapping[/red]")
        raise typer.Exit(1)
    if "namespace" not in spec or not isinstance(spec["namespace"], str):
        console.print(f"[red]{path}: missing or non-string 'namespace'[/red]")
        raise typer.Exit(1)
    unknown = set(spec) - _PROVISION_SPEC_KEYS
    if unknown:
        console.print(f"[red]{path}: unknown top-level fields: {sorted(unknown)}[/red]")
        raise typer.Exit(1)
    for i, repo in enumerate(spec.get("repos") or []):
        if not isinstance(repo, dict) or not isinstance(repo.get("url"), str):
            console.print(f"[red]{path}: repos[{i}] must be a mapping with string 'url'[/red]")
            raise typer.Exit(1)
    for i, s3 in enumerate(spec.get("s3") or []):
        if not isinstance(s3, dict):
            console.print(f"[red]{path}: s3[{i}] must be a mapping[/red]")
            raise typer.Exit(1)
        if not isinstance(s3.get("secret"), str):
            console.print(f"[red]{path}: s3[{i}].secret must be a string[/red]")
            raise typer.Exit(1)
        if not isinstance(s3.get("buckets"), list) or not all(isinstance(b, str) for b in s3["buckets"]):
            console.print(f"[red]{path}: s3[{i}].buckets must be a list of strings[/red]")
            raise typer.Exit(1)
    for i, ip in enumerate(spec.get("imagePullSecrets") or []):
        if not isinstance(ip, dict):
            console.print(f"[red]{path}: imagePullSecrets[{i}] must be a mapping[/red]")
            raise typer.Exit(1)
        for field in ("registry", "username"):
            if not isinstance(ip.get(field), str):
                console.print(f"[red]{path}: imagePullSecrets[{i}].{field} must be a string[/red]")
                raise typer.Exit(1)
        if "secret" in ip and not isinstance(ip["secret"], str):
            console.print(f"[red]{path}: imagePullSecrets[{i}].secret must be a string[/red]")
            raise typer.Exit(1)
        if "patchDefaultServiceAccount" in ip and not isinstance(ip["patchDefaultServiceAccount"], bool):
            console.print(f"[red]{path}: imagePullSecrets[{i}].patchDefaultServiceAccount must be a bool[/red]")
            raise typer.Exit(1)
    return spec


@app.command("app-provision")
def app_provision(
    manifest: Path = typer.Argument(..., help="Path to a provisioning YAML, or a directory containing provision.yaml."),
    control: str = typer.Option(None, "--control", "-c"),
) -> None:
    """Run a per-app provisioning manifest — one idempotent command per app.

    Reads a spec that declares everything an app needs *before* Argo CD syncs
    it: its namespace, any private repos Argo must be able to pull, and any
    Garage S3 bucket+credential bundles. Replaces chaining `add-repo-secret`
    and multiple `provision-s3-app` calls by hand.

    Spec shape:

        namespace: <ns>                          # required
        repos:                                   # optional
          - url: git@host:org/repo.git
            project: <argo-project>              # optional, default "*"
        s3:                                      # optional; one entry per Secret
          - secret: <secret-name>
            buckets: [<name>, ...]
            keyName: <name>                      # optional, default <secret>-key
        imagePullSecrets:                        # optional
          - registry: ghcr.io
            username: <github-user>
            secret: <secret-name>                # optional, default <registry-short>-pull
            patchDefaultServiceAccount: true     # optional, default true

    Idempotent end-to-end: already-registered repos are detected by stored URL
    and skipped without the GitHub deploy-key prompt; existing Garage buckets
    and keys are reused; the default SA's imagePullSecrets list is updated in
    place without clobbering other entries.
    """
    if control is None:
        control = _get_control_host()

    path = _resolve_manifest_path(manifest)
    spec = _load_provision_spec(path)

    namespace = spec["namespace"]
    console.print(f"[bold]Provisioning from[/bold] [cyan]{path}[/cyan]  →  namespace [cyan]{namespace}[/cyan]")
    console.print()

    _ensure_namespace(control, namespace)

    for repo in spec.get("repos") or []:
        _register_repo(
            control,
            repo["url"],
            project=repo.get("project", "*"),
        )

    for ip in spec.get("imagePullSecrets") or []:
        registry = ip["registry"]
        username = ip["username"]
        secret_name = ip.get("secret") or f"{registry.split('.')[0]}-pull"
        token = _resolve_registry_token(registry, username)
        _provision_image_pull_secret(
            control,
            namespace=namespace,
            secret_name=secret_name,
            registry=registry,
            username=username,
            token=token,
            patch_default_sa=ip.get("patchDefaultServiceAccount", True),
        )

    for s3 in spec.get("s3") or []:
        secret_name = s3["secret"]
        bucket_list = list(s3["buckets"])
        key_name = s3.get("keyName") or f"{secret_name}-key"
        _provision_s3_app_creds(
            control,
            key_name=key_name,
            namespace=namespace,
            secret_name=secret_name,
            buckets=bucket_list,
        )

    console.print()
    console.print(f"[green]✓[/green] App provisioning complete for namespace [cyan]{namespace}[/cyan].")


if __name__ == "__main__":
    app()

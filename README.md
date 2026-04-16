# k8s Cluster Bootstrap

Ephemeral, reproducible-from-git k3s cluster for a small fleet of Ubuntu boxes — optionally with NVIDIA GPU nodes for AI workloads. Create an instance repo from this template, edit an inventory, run two commands: you end up with a k3s cluster running Ollama on the GPU, managed by Argo CD.

> **LAN-only by design.** Ollama's API has no authentication. Do NOT deploy this on a cloud VM, a box with a public IP, or any network you don't fully trust without adding your own auth layer.

## What you get

- **k3s** cluster: 1 server + N agents (no HA)
- **Argo CD** on the control node, reconciling from your instance repo via the app-of-apps pattern — at `https://argocd.apps`
- **Ollama** deployed to the GPU node with a persistent local-path PVC — at `https://ollama.apps`
- **Node Feature Discovery (NFD)** auto-labels nodes with hardware info (PCI devices, CPU features)
- **NVIDIA device plugin** installed via Helm so pods can request `nvidia.com/gpu: 1`
- **Traefik Ingress** (shipped with k3s) fronted by one wildcard DNS record — new apps never require touching the router
- A single Python CLI (`cluster_manager.py`) that drives the whole lifecycle

## How the two repos work

This is a **public template repo**. It contains the generic infrastructure code — Ansible roles, CLI, and default app manifests with `REPO_URL` and `APPS_DOMAIN` placeholders. You don't modify this repo to deploy your cluster.

Instead, you create your own **instance repo** from it. The `init-fork` command rewrites the placeholders with your repo's URL and your LAN's domain. Argo CD reconciles from your instance repo.

```
k8s-cluster-bootstrap (upstream)        my-cluster (instance)
├── ansible/                          ├── ansible/
├── scripts/cluster_manager.py        ├── scripts/cluster_manager.py
├── clusters/                         ├── clusters/
│   repoURL: REPO_URL                 │   repoURL: https://github.com/you/my-cluster
│   host: argocd.APPS_DOMAIN          │   host: argocd.apps
└── README.md                         ├── ansible/inventory.ini
                                      └── your custom apps...
```

To pull upstream improvements into your instance later:
```bash
./scripts/cluster_manager.py sync-upstream
```

## Topology

```
┌─────────────────────────┐  ┌─────────────────────────┐  ┌──────────────────────────┐
│  control node           │  │  worker node            │  │  gpu node                │
│  (k3s server)           │  │  (k3s agent)            │  │  (k3s agent)             │
│  stable, always-on      │  │  stable, always-on      │  │  NVIDIA GPU; may reboot  │
│                         │  │                         │  │  labels: nvidia.com/gpu  │
│                         │  │                         │  │  taints: nvidia.com/gpu  │
└────────────┬────────────┘  └────────────┬────────────┘  └────────────┬─────────────┘
             │                            │                            │
             └────────────────────────────┴────────────────────────────┘
                                    LAN (router DNS)

                   *.apps  →  control node IP  (wildcard A record)
                   AI workloads (Ollama) nodeSelector+tolerate nvidia.com/gpu=true
                   → only schedule on GPU node
```

- **One server, no HA.** If it dies, rebuild from git.
- **GPU node is tainted** so random workloads don't steal its resources.
- **Minimum useful cluster** is 1 control + 1 GPU; scale agents as you like.

## Requirements

### Workstation (where you run the CLI)
- macOS or Linux
- `ansible` (`brew install ansible` / `apt install ansible`)
- Python 3.10+ (CLI deps installed into a venv — see walkthrough below)
- SSH key already loaded in your agent, accepted as an authorized key on every node

### Nodes
- **x86_64 Ubuntu** — already installed and booted. Ubuntu 25.10+ recommended (newer kernels ship drivers for recent NICs like the Realtek RTL8126).
- **NOPASSWD sudo** for the SSH user on every node (PXE autoinstall sets this up; see `pi-pxe-server`)
- **Router DNS registration** — your router must register DHCP client hostnames into DNS so you can SSH to `<name>`. Ubiquiti does this by default. If yours doesn't, use raw IPs in `inventory.ini`.
- **One node with an NVIDIA GPU** (any card supported by `ubuntu-drivers --gpgpu`)

### Network (one-time wildcard DNS)

Add one wildcard DNS A record to your router so `*.apps` resolves to the control node's IP. After this, every future app gets a free hostname — no per-app DNS.

**UniFi / Ubiquiti:**
1. Network app → **Settings** → **Routing** → **DNS** → **DNS Entries**
2. **Create Entry**:
   - Record type: `A`
   - Hostname: `*.apps`
   - IP Address: the control node's IP (check with `ssh k3s-control hostname -I`)
3. Apply. Verify: `dig +short argocd.apps` should print the control node IP.

If you use a different router, make the equivalent wildcard A record. If you want a different domain, pass `--apps-domain <your.domain>` to `init-fork`.

## First-time setup

All commands run on your workstation.

### 1. Create your instance repo

```bash
# Create a new repo on GitHub (pick any name you like).
gh repo create <you>/my-cluster

# Clone this upstream template, then re-point origin at your instance repo.
git clone https://github.com/<upstream-owner>/k8s-cluster-bootstrap.git my-cluster
cd my-cluster
git remote set-url origin git@github.com:<you>/my-cluster.git
git remote add upstream https://github.com/<upstream-owner>/k8s-cluster-bootstrap.git
git push -u origin main
```

### 2. Install CLI dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

In future sessions: `source .venv/bin/activate` before running the CLI.

### 3. Initialize placeholders

This rewrites `REPO_URL` and `APPS_DOMAIN` in the cluster manifests to point at your instance repo and your LAN domain. It prompts for the domain (default: `apps`).

```bash
./scripts/cluster_manager.py init-fork
git commit -am "Initialize instance"
git push
```

### 4. Prep every node

Run once per node. Pass the node's IP — the command prompts for a hostname and role, adds the node to `ansible/inventory.ini` (creating it if needed), authorizes the SSH host key, and runs the Ansible prep playbook.

```bash
# Prompts for hostname and role interactively:
./scripts/cluster_manager.py prep-node 192.168.1.10
./scripts/cluster_manager.py prep-node 192.168.1.12

# Or pass everything on the command line:
./scripts/cluster_manager.py prep-node 192.168.1.10 --hostname k3s-control --role control
./scripts/cluster_manager.py prep-node 192.168.1.11 --hostname k3s-worker --role worker
./scripts/cluster_manager.py prep-node 192.168.1.12 --hostname k3s-gpu --role gpu
```

After each node is prepped, its hostname is set and registered in router DNS — you can SSH to it by name (e.g. `k3s-control`).

Commit the inventory so it's tracked:
```bash
git add ansible/inventory.ini
git commit -m "Add inventory"
git push
```

### 5. Bootstrap the cluster

```bash
./scripts/cluster_manager.py bootstrap
```

### 6. Create secrets

Generates secrets and runtime config that aren't stored in git. Run once after bootstrap.

```bash
./scripts/cluster_manager.py setup-secrets
```

This creates:
- Wildcard TLS certificate for `*.APPS_DOMAIN` (self-signed, 10-year)
- OpenClaw gateway token (save it — needed for the web UI)
- Initial model selection for OpenClaw (prompts you to choose)

### 7. Verify

```bash
./scripts/cluster_manager.py status
```

After `bootstrap` finishes, Argo CD reconciles Ollama, the NVIDIA device plugin, and its own Ingress — typically in under a minute. Watch:

```bash
ssh k3s-control sudo k3s kubectl -n argocd get applications
```

Then open **`https://argocd.apps`**. The initial admin password:

```bash
ssh k3s-control sudo k3s kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d
```

Change it immediately after first login.

## Day-to-day operations

### Manage models

```bash
# See what's pulled and which is active
./scripts/cluster_manager.py models list

# Pull a new model
./scripts/cluster_manager.py models pull llama3.3:70b

# Switch the active model (restarts OpenClaw)
./scripts/cluster_manager.py models set llama3.3:70b

# Remove a model you no longer need
./scripts/cluster_manager.py models remove llama3.2:3b
```

### Check cluster status

```bash
./scripts/cluster_manager.py status
```

### Connect Slack (optional)

```bash
./scripts/cluster_manager.py setup-slack
```

Prompts for your Slack Bot Token (`xoxb-...`) and App Token (`xapp-...`) from https://api.slack.com/apps. Stores them in the cluster Secret, restarts OpenClaw. Run again to rotate tokens.

### Sync upstream improvements

When the public template gets bug fixes or new features:

```bash
./scripts/cluster_manager.py sync-upstream
git push
```

This fetches from `upstream/main`, merges, and re-runs `init-fork` to replace any new placeholders that came with the merge. If there are merge conflicts, resolve them manually, then run `./scripts/cluster_manager.py init-fork && git commit`.

### Add a new app

Pure git workflow — no Ansible, no DNS:

1. Create `clusters/default/apps/<name>/` with raw Kubernetes manifests (include an Ingress for `<name>.apps` if you want a hostname).
2. For AI workloads, include `nodeSelector: nvidia.com/gpu: "true"` and the matching toleration.
3. Create `clusters/default/applications/children/<name>.yaml` — an Argo `Application` pointing at that path.
4. Commit + push. Argo picks it up automatically via `selfHeal: true`.

### Add a new node

1. Install Ubuntu on the new machine.
2. `./scripts/cluster_manager.py prep-node <ip>` — prompts for hostname and role, adds to inventory, preps the node.
3. `./scripts/cluster_manager.py bootstrap` — idempotent; only the new node actually changes.

## The CLI

`scripts/cluster_manager.py` is the single entrypoint:

| Command | Purpose |
|---|---|
| `init-fork [URL] [--apps-domain D]` | Rewrite `REPO_URL` + `APPS_DOMAIN` placeholders in cluster manifests. |
| `prep-node <ip> [--hostname H] [--role R]` | Add node to inventory, authorize SSH key, run prep playbook (apt upgrade, hostname, NVIDIA). |
| `bootstrap` | Run `ansible/cluster.yml` against the whole inventory (k3s + Argo CD). |
| `setup-secrets` | Generate TLS cert, OpenClaw token, and initial model selection. |
| `models list` | Show pulled Ollama models and which is active. |
| `models pull <tag>` | Pull a model into Ollama. |
| `models set <tag>` | Set the active model for OpenClaw (restarts pod). |
| `models remove <tag>` | Delete a model from Ollama. |
| `setup-slack` | Configure Slack bot + app tokens for OpenClaw. |
| `approve-pairing <channel> <code>` | Approve a user's pairing request (e.g. `slack HPP2WU9B`). |
| `status [--control H]` | `kubectl get nodes,pods -A` via SSH to the control node. |
| `sync-upstream [--remote R] [--branch B]` | Fetch + merge upstream, re-apply placeholders. |

Run `./scripts/cluster_manager.py --help` (or `<cmd> --help`) for full options.

### What `prep.yml` does
1. `base` on every targeted host — apt upgrade, utilities, unattended-upgrades, set hostname (then DHCP renew so the router registers `<name>`).
2. `nvidia` on hosts in `[gpu]` — install driver (autodetected via `ubuntu-drivers`) + NVIDIA Container Toolkit. Auto-reboots if a new driver was installed.

### What `cluster.yml` does
1. `k3s-server` on control — install k3s (pinned), capture join token.
2. `k3s-agent` on every agent — join the cluster. GPU nodes also get the `nvidia.com/gpu=true` label + `NoSchedule` taint and containerd NVIDIA runtime config.
3. `argocd` on control — install Argo CD (pinned), set `server.insecure=true` for HTTP Ingress, and apply the root Application.

## Repo layout

```
.
├── README.md
├── requirements.txt                    # Python deps for the CLI
├── scripts/
│   └── cluster_manager.py              # typer CLI
├── ansible/
│   ├── ansible.cfg
│   ├── inventory.ini.example           # committed template (public)
│   ├── inventory.ini                   # your real inventory (instance repo only)
│   ├── prep.yml                        # per-node: base + nvidia
│   ├── cluster.yml                     # cluster-wide: k3s + argocd
│   ├── group_vars/all.yml              # pinned versions, apps_domain
│   └── roles/
│       ├── base/                       # apt, hostname, unattended-upgrades
│       ├── nvidia/                     # GPU only; auto-reboots
│       ├── k3s-server/
│       ├── k3s-agent/                  # GPU variant adds label/taint/containerd
│       └── argocd/                     # installs Argo CD, applies root Application
└── clusters/
    └── default/
        ├── applications/
        │   ├── root.yaml               # app-of-apps, applied by Ansible
        │   └── children/               # reconciled by root
        │       ├── ollama.yaml
        │       ├── openclaw.yaml
        │       ├── nvidia-device-plugin.yaml
        │       ├── node-feature-discovery.yaml
        │       └── argocd-ingress.yaml
        └── apps/                       # raw k8s manifests, reconciled by Argo
            ├── argocd-ingress/
            ├── ollama/
            └── openclaw/
```

## Version pinning

All pinned in `ansible/group_vars/all.yml`:

| Component | Version |
|---|---|
| k3s | `v1.32.3+k3s1` |
| Argo CD | `v2.14.3` |
| Ollama | `0.20.7` |
| OpenClaw | `2026.4.14` |
| NVIDIA device plugin Helm chart | `0.17.0` |
| Node Feature Discovery Helm chart | `0.18.3` |

Bump deliberately; re-run `./scripts/cluster_manager.py bootstrap` to apply.

## Known sharp edges

- **Unattended-upgrades on the GPU node can break `nvidia-smi`.** A kernel upgrade without a DKMS rebuild silently breaks GPU access. Re-run `prep-node <gpu-host>` — the `nvidia` role will reinstall drivers.
- **local-path PVCs don't survive OS reinstall.** Reinstalling the GPU node's OS means re-pulling models. By design — treat the OS as ephemeral.
- **Self-signed TLS cert.** `setup-secrets` generates a wildcard cert for `*.APPS_DOMAIN`. Your browser will show a warning on first visit — accept it once per browser.
- **Ollama has no auth.** LAN only. See top-of-readme warning.

## Key decisions

| Decision | Choice | Reason |
|---|---|---|
| Repo model | Public template + instance repo | Generic upstream stays clean; instance holds your config |
| Cluster topology | 1 server + N agents, no HA | Simple; rebuild on failure |
| Control plane placement | Non-GPU node | Stable; GPU node can reboot freely |
| GPU scheduling | Label + taint + toleration on `nvidia.com/gpu` | Matches NVIDIA GPU Operator convention |
| Bootstrap driver | Ansible behind a typer CLI | Idempotent roles, one operator entrypoint |
| Node addressability | Router DNS (`<name>`) | No DHCP-reservation bookkeeping |
| App addressability | Wildcard DNS (`*.apps`) + Traefik Ingress | One-time DNS; new apps add no manual steps |
| GitOps tool | Argo CD | UI is useful; app-of-apps pattern |
| App delivery | Committed Application manifests + `init-fork` | Adding apps is pure git |
| Model storage | Persistent local-path PVC on GPU node | Ephemeral = OS; don't re-pull large models |
| External access | LAN only (HTTPS Ingress, self-signed) | No public exposure; secure context for web apps |
| Secrets | Kubernetes Secrets created by `setup-secrets` CLI | Not in git; migrate to Sealed Secrets or external store later |
| Model management | Runtime-only via API | No model names in repo |
| Version pinning | All in `group_vars/all.yml` | Reproducible re-runs |

## Non-goals

- HA control plane
- Public internet exposure
- CA-signed TLS (self-signed is sufficient for LAN)
- Model pre-pulling / init containers
- Backup of model weights

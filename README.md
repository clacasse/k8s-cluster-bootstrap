# k8s Cluster Homelab

Ephemeral, reproducible-from-git k3s cluster for a small fleet of Ubuntu boxes — optionally with one or more NVIDIA GPU nodes for AI workloads. Clone, edit an inventory, run two commands: you end up with a k3s cluster running Ollama on the GPU, managed by Argo CD.

> **LAN-only by design.** Ollama's API has no authentication. Do NOT deploy this on a cloud VM, a box with a public IP, or any network you don't fully trust without adding your own auth layer (reverse proxy with basic auth, Tailscale ACLs, etc.).

## What you get

- **k3s** cluster: 1 server + N agents (no HA)
- **Argo CD** on the control node, reconciling from your fork via the app-of-apps pattern
- **Ollama** deployed to the GPU node (via NodePort `31434`) with a persistent local-path PVC for model weights
- **NVIDIA device plugin** installed via Helm so pods can request `nvidia.com/gpu: 1`
- A single Python CLI (`cluster_manager.py`) that drives the whole lifecycle

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
                                    LAN (router DNS, .localdomain)

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
- Python 3 with `typer` and `rich` (`pip install typer rich`)
- SSH key already loaded in your agent, accepted as an authorized key on every node

### Nodes
- **x86_64 Ubuntu** — already installed and booted before you touch this repo. Ubuntu 25.10+ is recommended (newer kernels ship drivers for recent NICs like the Realtek RTL8126).
- **Same sudo password on every node** (the CLI prompts once with `--ask-become-pass`)
- **Router DNS registration.** Your router must register DHCP client hostnames into DNS so you can SSH to `<name>.localdomain`. Ubiquiti's built-in DNS does this by default. If yours doesn't, edit `inventory.ini` to use raw IPs instead.
- **One node with an NVIDIA GPU** if you want Ollama (any card supported by `ubuntu-drivers --gpgpu`)

## First-run walkthrough

All commands run on your workstation.

```bash
# 1. Fork this repo on GitHub, then clone your fork.
git clone https://github.com/<you>/k8s-cluster-homelab.git
cd k8s-cluster-homelab

# 2. One-time: rewrite the REPO_URL placeholder in Argo manifests to point at
#    your fork (auto-detected from `git remote`). Commit + push the result —
#    Argo CD reconciles from git, so your fork must have the real URL.
./scripts/cluster_manager.py init-fork
git commit -am "Point Argo at my fork"
git push

# 3. Configure your nodes. Fill in hostnames (or IPs) matching your network.
cp ansible/inventory.ini.example ansible/inventory.ini
$EDITOR ansible/inventory.ini

# 4. Pre-authorize each node's SSH host key (Ansible won't connect otherwise).
for host in $(awk '/^\[/{g=$0} g!~/vars|children/ && /\./ {print $1}' ansible/inventory.ini); do
    ssh-keyscan -H "$host" >> ~/.ssh/known_hosts
done

# 5. Prep every node (apt upgrade, hostname, NVIDIA driver on GPU nodes).
#    Run once per host. Idempotent — safe to re-run anytime.
./scripts/cluster_manager.py prep-node k3s-control.localdomain
./scripts/cluster_manager.py prep-node k3s-worker.localdomain
./scripts/cluster_manager.py prep-node k3s-gpu.localdomain

# 6. Bootstrap the cluster: k3s on every node + Argo CD on control.
./scripts/cluster_manager.py bootstrap

# 7. Verify.
./scripts/cluster_manager.py status
```

After `bootstrap` finishes, Argo CD reconciles the Ollama and NVIDIA-device-plugin Applications on its own. Watch progress:

```bash
ssh k3s-control.localdomain sudo k3s kubectl -n argocd get applications
```

## The CLI

`scripts/cluster_manager.py` is the single entrypoint:

| Command | Purpose |
|---|---|
| `init-fork [URL]` | Rewrite `REPO_URL` placeholder in `clusters/**/*.yaml`. Defaults to `git remote get-url origin`. |
| `prep-node <host>` | Run `ansible/prep.yml` against one host. Apt upgrade, hostname, NVIDIA if in `[gpu]` group. |
| `bootstrap` | Run `ansible/cluster.yml` against the whole inventory. Installs k3s + Argo CD. |
| `pull-model <tag>` | Pull a model into the running Ollama server. `--host` to target a specific node:31434. |
| `status` | `kubectl get nodes,pods -A` via SSH to the control node. |

Run `./scripts/cluster_manager.py --help` (or `<cmd> --help`) for full options.

### What `prep.yml` does
1. `base` on every targeted host — apt upgrade, utilities, unattended-upgrades, set hostname (then DHCP renew so the router registers `<name>.localdomain` in its DNS).
2. `nvidia` on hosts in `[gpu]` — install driver (autodetected via `ubuntu-drivers`) + NVIDIA Container Toolkit. Auto-reboots if a new driver was installed.

### What `cluster.yml` does
1. `k3s-server` on control — install k3s (pinned), capture join token.
2. `k3s-agent` on every agent — join the cluster. GPU nodes also get the `nvidia.com/gpu=true` label + `NoSchedule` taint and containerd NVIDIA runtime config.
3. `argocd` on control — install Argo CD (pinned) and apply the root Application that owns everything under `clusters/homelab/applications/children/`.

## Pulling models

Ollama is reachable on NodePort `31434` on any cluster node. Pass `--host` to hit the GPU node directly:

```bash
./scripts/cluster_manager.py pull-model llama3.3:70b --host k3s-gpu.localdomain:31434
```

## Adding a new app

Pure git workflow — no Ansible involved:

1. Create `clusters/homelab/apps/<name>/` with raw Kubernetes manifests.
2. For AI workloads, include `nodeSelector: nvidia.com/gpu: "true"` and the matching toleration.
3. Create `clusters/homelab/applications/children/<name>.yaml` — an Argo `Application` pointing at that path.
4. Commit + push. Argo picks it up automatically via `selfHeal: true`.

## Adding a new node later

1. Install Ubuntu on the new machine (any method — this repo doesn't care how).
2. Add it to `ansible/inventory.ini` in the appropriate group.
3. Authorize its SSH host key: `ssh-keyscan -H <new-host> >> ~/.ssh/known_hosts`.
4. `./scripts/cluster_manager.py prep-node <new-host>`
5. `./scripts/cluster_manager.py bootstrap` — idempotent; only the new node actually changes.

## Repo layout

```
.
├── README.md
├── scripts/
│   └── cluster_manager.py              # typer CLI
├── ansible/
│   ├── ansible.cfg
│   ├── inventory.ini.example           # committed template
│   ├── inventory.ini                   # gitignored (site-specific)
│   ├── prep.yml                        # per-node: base + nvidia
│   ├── cluster.yml                     # cluster-wide: k3s + argocd
│   ├── group_vars/all.yml              # pinned versions, cluster_name
│   └── roles/
│       ├── base/                       # apt, hostname, unattended-upgrades
│       ├── nvidia/                     # GPU only; auto-reboots
│       ├── k3s-server/
│       ├── k3s-agent/                  # GPU variant adds label/taint/containerd
│       └── argocd/                     # applies root Application
└── clusters/
    └── homelab/
        ├── applications/
        │   ├── root.yaml               # app-of-apps, applied by Ansible
        │   └── children/               # reconciled by root
        │       ├── ollama.yaml
        │       └── nvidia-device-plugin.yaml
        └── apps/                       # raw k8s manifests, reconciled by Argo
            └── ollama/
```

## Version pinning

All pinned in `ansible/group_vars/all.yml`:

| Component | Version |
|---|---|
| k3s | `v1.32.3+k3s1` |
| Argo CD | `v2.14.3` |
| Ollama | `0.6.5` |
| NVIDIA device plugin Helm chart | `0.17.0` |

Bump deliberately; re-run `./scripts/cluster_manager.py bootstrap` to apply.

## Known sharp edges

- **Unattended-upgrades on the GPU node can break `nvidia-smi`.** A kernel upgrade without a DKMS rebuild silently breaks GPU access. If it happens, re-run `prep-node <gpu-host>` — the `nvidia` role will reinstall drivers.
- **local-path PVCs don't survive OS reinstall.** Reinstalling the GPU node's OS means re-pulling models. By design — treat the OS as ephemeral.
- **Ollama has no auth.** LAN only. See top-of-readme warning.

## Key decisions

| Decision | Choice | Reason |
|---|---|---|
| Cluster topology | 1 server + N agents, no HA | Simple; rebuild on failure |
| Control plane placement | Non-GPU node | Stable; GPU node can reboot freely |
| GPU scheduling | Label + taint + toleration on `nvidia.com/gpu` | Matches NVIDIA GPU Operator convention |
| Bootstrap driver | Ansible behind a typer CLI | Idempotent roles, one operator entrypoint |
| Node addressability | Router DNS (`<name>.localdomain`) | No DHCP-reservation bookkeeping |
| GitOps tool | Argo CD | UI is useful; app-of-apps pattern |
| App delivery | Committed Application manifests + `init-fork` | Fork-friendly; adding apps is pure git |
| Model storage | Persistent local-path PVC on GPU node | Ephemeral = OS; don't re-pull large models |
| External access | LAN only (NodePort) | No public exposure |
| Secrets | None in v1 | Trusted LAN. Add Sealed Secrets later if needed. |
| Model management | Runtime-only via API | No model names in repo |
| Version pinning | All in `group_vars/all.yml` | Reproducible re-runs |

## Non-goals

- HA control plane
- Public internet exposure
- Model pre-pulling / init containers
- Backup of model weights

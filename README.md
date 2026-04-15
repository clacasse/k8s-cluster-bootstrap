# GPU Workstation Homelab

Ephemeral, reproducible-from-git k3s cluster for a GPU workstation + worker nodes. Drop in 3 x86_64 Ubuntu boxes (one with an NVIDIA GPU), clone, run two scripts — end up with a cluster running Ollama on the GPU node managed by Argo CD.

> **LAN-only by design.** Ollama's API has no authentication. Do NOT deploy this on a cloud VM, a box with a public IP, or any network you don't fully trust without adding your own auth layer (reverse proxy with basic auth, Tailscale ACLs, etc.).

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
                                    LAN (DHCP reservations)

                   AI workloads (Ollama) nodeSelector+tolerate nvidia.com/gpu=true
                   → only schedule on GPU node
```

- **One server** (no HA). If it dies, PXE + re-bootstrap.
- **GPU node is tainted** so random workloads don't steal its resources.
- **Other workloads** (future) run on the two non-GPU nodes.

## Requirements

- **Nodes**: 3× x86_64 Ubuntu hosts (25.10+ recommended)
- **Network**: shared LAN, DHCP reservations (stable IPs)
- **GPU**: NVIDIA card on one node (any model supported by `ubuntu-drivers`)
- **SSH**: key-based access from your workstation to all 3 nodes with sudo (same password across nodes — PXE autoinstall handles this)
- **Workstation**: Ansible installed locally (`brew install ansible` / `apt install ansible`)

## Usage

**On your workstation (Mac/Linux):**

```bash
# 1. Fork this repo on GitHub, then clone your fork
git clone https://github.com/<you>/gpu-workstation-homelab.git
cd gpu-workstation-homelab

# 2. One-time: rewrite Argo Application manifests to point at your fork
./scripts/init-fork.sh

# 3. Configure your nodes
cp ansible/inventory.ini.example ansible/inventory.ini
$EDITOR ansible/inventory.ini

# 4. Pre-authorize each node's SSH host key (one line per node)
for host in k3s-server.lan k3s-worker.lan k3s-gpu.lan; do
    ssh-keyscan -H "$host" >> ~/.ssh/known_hosts
done

# 5. Run it (prompts for sudo password)
./bootstrap.sh
```

The playbook orchestrates:

1. `base` on all nodes — apt upgrade, utilities, unattended-upgrades
2. `nvidia` on GPU node — driver (autodetected) + Container Toolkit; auto-reboots if driver newly installed
3. `k3s-server` on control node — installs k3s (pinned version), captures join token
4. `k3s-agent` on workers + GPU node — joins cluster; GPU node additionally gets `nvidia.com/gpu=true` label and taint, plus containerd NVIDIA runtime config
5. `argocd` on control node — installs Argo CD (pinned version), applies the root Application

Argo CD then reconciles everything under `clusters/homelab/applications/children/` — Ollama and the NVIDIA device plugin — on its own.

## Pulling models

Ollama is reachable at NodePort `31434` on any node.

```bash
./scripts/pull-model.sh llama3.3:70b                 # from control node
./scripts/pull-model.sh gemma3:27b <any-node>:31434  # from workstation
```

## Adding a new app

Pure git workflow — no Ansible involved:

1. Create `clusters/homelab/apps/<name>/` with raw k8s manifests
2. For AI workloads, add `nodeSelector: nvidia.com/gpu: "true"` + matching toleration
3. Create `clusters/homelab/applications/children/<name>.yaml` pointing at that path
4. Commit + push — Argo picks it up automatically via `selfHeal: true`

## Repo layout

```
.
├── README.md
├── bootstrap.sh                         # runs from workstation
├── scripts/
│   ├── init-fork.sh                     # one-time REPO_URL rewrite
│   └── pull-model.sh                    # runtime, not declarative state
├── ansible/
│   ├── ansible.cfg
│   ├── inventory.ini.example            # committed template
│   ├── inventory.ini                    # gitignored (site-specific)
│   ├── site.yml                         # multi-play orchestration
│   ├── group_vars/all.yml               # pinned versions, cluster_name
│   └── roles/
│       ├── base/
│       ├── nvidia/                      # GPU node only; auto-reboots
│       ├── k3s-server/
│       ├── k3s-agent/                   # GPU variant adds label/taint/containerd
│       └── argocd/                      # applies root Application
└── clusters/
    └── homelab/
        ├── applications/
        │   ├── root.yaml                # app-of-apps, applied by Ansible
        │   └── children/                # reconciled by root
        │       ├── ollama.yaml
        │       └── nvidia-device-plugin.yaml
        └── apps/                        # raw k8s manifests, reconciled by Argo
            └── ollama/
```

## Version pinning

All pinned in `ansible/group_vars/all.yml`:

| Component | Version |
|-----------|---------|
| k3s | `v1.32.3+k3s1` |
| Argo CD | `v2.14.3` |
| Ollama | `0.6.5` |
| NVIDIA device plugin Helm chart | `0.17.0` |

Bump deliberately; re-run `./bootstrap.sh` to apply.

## Known sharp edges

- **Unattended-upgrades on the GPU node can break nvidia-smi** — a kernel upgrade without a DKMS re-run silently breaks GPU access. Ephemeral philosophy applies: if `nvidia-smi` stops working after an upgrade, re-run `./bootstrap.sh` (the `nvidia` role will reinstall drivers) or PXE re-install.
- **local-path PVC doesn't survive OS reinstall.** If you reinstall the GPU node's OS, models must be re-pulled. By design.
- **Ollama has no auth.** LAN only. See top-of-readme warning.

## Key decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Cluster topology | 1 server + 2 agents, no HA | Simple; rebuild on failure |
| Control plane placement | Non-GPU worker node | Stable; GPU node can reboot freely |
| GPU scheduling | Node label + taint + toleration using `nvidia.com/gpu` key | Matches NVIDIA GPU Operator convention |
| GitOps tool | Argo CD | UI useful; app-of-apps pattern |
| App delivery | Committed Application manifests + one-time `init-fork.sh` | Fork-friendly; adding apps is pure git |
| Model storage | Persistent local-path PVC on GPU node | Ephemeral = OS; don't re-pull large models |
| External access | LAN only (NodePort) | No public exposure |
| Secrets | None in v1 | Trusted LAN. Add Sealed Secrets later if needed. |
| Model management | Runtime-only via API | No model names in repo |
| Version pinning | All in `group_vars/all.yml` | Reproducible re-runs |

## Non-goals

- HA control plane
- Multi-GPU node
- Public internet exposure
- Model pre-pulling / init containers
- Backup of model weights

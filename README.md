# GPU Workstation Homelab

Ephemeral, reproducible-from-git GPU workstation for running local LLMs on Kubernetes.

**Hardware**: RTX 5080 16GB, 16-core x86_64, 32GB RAM, NVMe
**OS**: Ubuntu (provisioned via [pxe-homelab](https://github.com/clacasse/pxe-homelab))
**Stack**: k3s → Argo CD → Ollama + OpenWebUI (on a separate host)

## Design goals

1. **Ephemeral** — OS is throwaway. PXE re-install + clone + one command = running Ollama again.
2. **Everything in git** — no hand-edits on the box. If it's not in this repo (or pullable at runtime), it doesn't exist.
3. **Minimum imperative, maximum declarative** — `bootstrap.sh` does just enough to get k3s + Argo running; Argo reconciles everything else.
4. **Model-agnostic** — no model names in any manifest. Pull/swap/run any model at runtime without redeploying.

## Bootstrap flow

```
PXE install Ubuntu
      │
      ▼
ssh user@gpu-box
      │
      ▼
git clone <this repo> && cd gpu-workstation-homelab && ./bootstrap.sh
      │
      ▼
bootstrap.sh installs ansible, runs site.yml
      │
      ├─ role: base           — apt upgrade, unattended-upgrades, timezone
      ├─ role: nvidia         — driver + NVIDIA Container Toolkit
      ├─ role: k3s            — single-node k3s, containerd nvidia runtime
      └─ role: argocd         — install Argo CD, apply root Application
                                       │
                                       ▼
                          Argo reconciles clusters/gpu-workstation/apps/:
                            - nvidia-device-plugin (exposes nvidia.com/gpu)
                            - ollama (Deployment + PVC + Service)
      │
      ▼
Ollama running, empty cache. Pull any model on demand:
   curl http://gpu-box:11434/api/pull -d '{"name":"<any-model>"}'
```

## Repo layout

```
gpu-workstation-homelab/
├── README.md
├── bootstrap.sh                    # one-shot entrypoint on fresh Ubuntu
├── ansible/
│   ├── site.yml                    # inventory = localhost
│   └── roles/
│       ├── base/
│       ├── nvidia/
│       ├── k3s/
│       └── argocd/
├── clusters/gpu-workstation/
│   ├── root-app.yaml               # Argo root Application (app-of-apps)
│   └── apps/
│       ├── nvidia-device-plugin/
│       └── ollama/                 # no model names anywhere
├── scripts/
│   └── pull-model.sh               # convenience wrapper, not part of state
└── .github/workflows/lint.yml
```

## Key decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| GitOps tool | Argo CD | UI is useful for homelab; app-of-apps pattern fits single-cluster |
| Model storage | Persistent local-path PVC on NVMe | Ephemeral = OS layer. Re-pulling 17GB+ models every rebuild is silly. |
| External access | LAN only | No public exposure; OpenWebUI on separate LAN host calls directly. |
| Secrets | None in v1 | Ollama has no auth; trusted LAN. Add Sealed Secrets if/when needed. |
| Model management | Runtime-only via API | No model names in repo. Truly model-agnostic. |

## Non-goals (for now)

- Multi-node cluster — single node, can expand later
- Public internet exposure — LAN only; add Tailscale/Cloudflare Tunnel later if needed
- Model pre-pulling / init containers — manage models at runtime, not deploy time
- Backup of model weights — weights are re-downloadable; any user config on OpenWebUI host is its own backup concern

## Status

Planning. PXE server works. GPU box not yet PXE booted.

## Next steps

1. PXE boot the GPU workstation (pending Ubuntu 25.10 install via pxe-homelab)
2. Write `bootstrap.sh` + `ansible/` roles
3. Write `clusters/gpu-workstation/` manifests
4. First end-to-end run
5. Iterate

# k8s Cluster Bootstrap

Ephemeral, reproducible-from-git k3s cluster for a small fleet of Ubuntu boxes — optionally with NVIDIA GPU nodes for AI workloads. You fork this template, edit an inventory, run a handful of commands; you end up with a k3s cluster managed by Argo CD, a fully local LLM stack (llama.cpp chat + embed servers, the Hermes agent runtime, a RAG pipeline backed by ChromaDB), and the infra to support real apps (CloudNativePG, Garage S3, Prometheus + Grafana) — all reachable on your LAN.

> **LAN-only by design.** In-cluster services (LLM inference, Ingress endpoints) don't carry their own authentication. Do NOT deploy this on a cloud VM, a box with a public IP, or any network you don't fully trust without adding your own auth layer.

See [docs/architecture.md](./docs/architecture.md) for a Mermaid diagram of the cluster + a generic application reference architecture showing how apps expose themselves to the agent through MCP.

## What you get

**Cluster + GitOps**
- **k3s** cluster: 1 server + N agents (no HA). Node roles: `control`, `worker`, `gpu`, `storage`.
- **Argo CD** on the control node, reconciling from your instance repo via the app-of-apps pattern — at `https://argocd.apps.home.arpa`
- **Traefik Ingress** (shipped with k3s) fronted by one wildcard DNS record — new apps never require touching the router

**Local LLM stack** (all runs on your hardware, no API calls out)
- **llama.cpp** chat server on the GPU node + embed server on CPU. OpenAI-compatible `/v1` API. Init containers pull GGUF weights from HuggingFace on first boot. Built-in support for MoE expert offloading (`--cpu-moe` / `--n-cpu-moe`) so 30B-class MoEs run on a 16 GB-class GPU.
- **llm-proxy** in front of the chat server — logs every request as JSONL, exports per-call latency metrics, lets you replay traffic without enabling llama.cpp's own slow-path logger.
- **Hermes** agent runtime at `https://hermes.apps.home.arpa`. Telegram + Discord + Slack channels for chat; MCP support for tool wiring; Obsidian vault as the workspace; persistent memory + skills. Image consumed directly from Docker Hub (`docker.io/nousresearch/hermes-agent`); model defaults overlaid via a bootstrap-config init container that reads from the `hermes-model` ConfigMap so `llama set-chat` propagates atomically.
- **RAG pipeline**: ChromaDB + a vault indexer + an MCP server exposing semantic search over your notes.
- Model + tunable settings managed via `cluster_manager.py llama …` — chat model choice is per-deployment (the upstream template ships no opinion about which model your cluster runs).

**Infrastructure**
- **CloudNativePG operator** in `cnpg-system` — consumer apps create their own `Cluster` CRs; no databases installed by default
- **Garage (S3-compatible object store)** pinned to the storage node with `local-path` PVC — consumer apps provision their own buckets via `provision-s3-app`
- **Prometheus + Grafana** at `https://grafana.apps.home.arpa` with node + GPU + LLM-inference dashboards out of the box
- **Loki + Grafana Alloy** for cluster-wide log aggregation — Alloy DaemonSet tails every pod's logs, ships to Loki (chunks land in Garage S3, 7-day retention), and the Loki datasource is wired into Grafana automatically. Pair with the Grafana MCP server (`setup-grafana-mcp`) to let Claude Code query logs while you debug.
- **DCGM exporter** for NVIDIA GPU metrics; **NFD** auto-labels nodes with hardware info; **NVIDIA device plugin** so pods can request `nvidia.com/gpu: 1`

**Tooling**
- A single Python CLI (`cluster_manager.py`) that drives the whole lifecycle — bootstrap, secrets, model setup, app provisioning, plus registering one or more **private apps repos** that Argo watches alongside this one for the workloads you don't want in the public template

## How the two repos work

This is a **public template repo**. It contains the generic infrastructure code — Ansible roles, CLI, and default app manifests with `REPO_URL` and `APPS_DOMAIN` placeholders. You don't modify this repo to deploy your cluster.

Instead, you create your own **instance repo** from it. The `init-fork` command rewrites the placeholders with your repo's URL and your LAN's domain. Argo CD reconciles from your instance repo.

```
k8s-cluster-bootstrap (upstream)        my-cluster (instance)
├── ansible/                          ├── ansible/
├── scripts/cluster_manager.py        ├── scripts/cluster_manager.py
├── clusters/                         ├── clusters/
│   repoURL: REPO_URL                 │   repoURL: https://github.com/you/my-cluster
│   host: argocd.APPS_DOMAIN          │   host: argocd.apps.home.arpa
└── README.md                         ├── ansible/inventory.ini
                                      └── your custom apps...
```

To pull upstream improvements into your instance later:
```bash
./scripts/cluster_manager.py sync-upstream
```

## Topology

Four roles, one VLAN, wildcard DNS funnels every app hostname through Traefik on the control node. See [docs/architecture.md](./docs/architecture.md) for a rendered diagram covering the cluster + the generic application reference architecture.

- **control** node runs the k3s server, Traefik Ingress, Argo CD, and the agent runtime. Stable, always-on. `*.apps.home.arpa` resolves here via a single wildcard A record on your router.
- **gpu** node is single-purpose: NVIDIA GPU + 32 GB-ish host RAM, runs `llama-chat` and `llama-embed`. Tainted with `nvidia.com/gpu=true:NoSchedule` so random workloads don't steal it.
- **storage** node holds all the stateful services — Postgres clusters, the Garage object store, ChromaDB. Tainted with `role=storage:NoSchedule` and PVCs pin there via `local-path`.
- **worker** nodes are optional general compute; scale however you like.

**One server, no HA.** If it dies, rebuild from git. **Minimum useful cluster** is 1 control + 1 GPU; the storage node is optional (its pods fall back to the control node when absent).

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

Add one wildcard DNS A record to your router so `*.apps.home.arpa` resolves to the control node's IP. After this, every future app gets a free hostname — no per-app DNS.

**UniFi / Ubiquiti:**
1. Network app → **Settings** → **Routing** → **DNS** → **DNS Entries**
2. **Create Entry**:
   - Record type: `A`
   - Hostname: `*.apps.home.arpa`
   - IP Address: the control node's IP (check with `ssh k3s-control hostname -I`)
3. Apply. Verify: `dig +short argocd.apps.home.arpa` should print the control node IP.

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

This rewrites `REPO_URL`, `APPS_DOMAIN`, and `IMAGE_REPO` in the cluster manifests to point at your instance repo, your LAN domain, and your GHCR namespace. The container-registry substitution lets the bundled apps (`llm-proxy`, `rag-indexer`, `rag-mcp`) pull images from your fork's own GHCR — the GitHub Actions workflows in this template push to `ghcr.io/${{ github.repository }}/<name>` automatically, and `IMAGE_REPO` substitutes to that same `<owner>/<repo>` so deployments and builds stay in sync. It prompts for the domain (default: `apps`).

```bash
./scripts/cluster_manager.py init-fork
git commit -am "Initialize instance"
git push
```

**Public vs private instance repo.** `init-fork` reads the URL from your `git remote get-url origin` and substitutes it into the manifests as-is. Use HTTPS to keep the repo public and let Argo fetch anonymously, or use SSH (`git@github.com:you/my-cluster.git`) to keep the repo private. When `init-fork` sees an SSH URL it also generates an ed25519 deploy key at `~/.ssh/argocd-instance-<repo>.key` and prints the public half — paste that into your repo's GitHub deploy keys page (read-only). The matching Argo Repository Secret is applied later, in step 6, once the cluster exists.

To flip an existing instance repo from public→private after the fact, see [Convert instance repo from public to private](#convert-instance-repo-from-public-to-private).

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

> **Note:** `inventory.ini` contains real IPs and hostnames. It's gitignored by default. If your instance repo is public, keep it that way. If private, you can optionally track it with `git add -f ansible/inventory.ini`.

### 5. Bootstrap the cluster

```bash
./scripts/cluster_manager.py bootstrap
```

### 6. Create secrets + pick initial chat model

Generates secrets and runtime config that aren't stored in git. Run once after bootstrap.

```bash
./scripts/cluster_manager.py setup-secrets              # app-level secrets
./scripts/cluster_manager.py bootstrap-infra-secrets    # infrastructure-level
./scripts/cluster_manager.py setup-instance-repo        # private repo only — no-op for HTTPS
./scripts/cluster_manager.py llama setup                # pick chat model
```

`setup-secrets` creates:
- Wildcard TLS certificate for `*.APPS_DOMAIN` (self-signed, 10-year)
- Grafana admin password

`bootstrap-infra-secrets` creates:
- `garage-auth` Secret in the `garage` namespace (`rpc_secret`, `admin_token`, `metrics_token`)
- Applies the single-node Garage layout (idempotent: detects already-applied layout)
- Prints the Garage admin token — save it if you want to use the admin API directly

`setup-instance-repo` (only meaningful when the instance repo is private/SSH):
- Applies the Argo Repository Secret pairing your repo's SSH URL with the deploy key generated by `init-fork`
- Patches the live `root` Application's repoURL so the change takes effect immediately
- No-op when the configured repoURL is HTTPS — public repos don't need this

`llama setup` creates:
- `llama-cpp/llama-cpp-model` ConfigMap — every per-deployment chat knob: model repo / file / served-as alias / ctx size / GPU layer offload / parallel slots / KV cache type / flash-attn / `--cpu-moe` / `--n-cpu-moe` / `--override-tensor` / extra flags. Each one is also individually settable later via `llama set-<knob>`.
- `hermes/hermes-model` ConfigMap — `active-model` key, kept in sync with the chat-model alias. Hermes's bootstrap-config init container reads it and overlays the value into `/opt/data/config.yaml` as `model.default` on every pod start.

Both ConfigMaps are **imperatively managed** — not reconciled from git — so
edits via `llama setup` or `llama set-chat` persist without needing a commit.
The upstream template deliberately ships NO opinion about which chat model
your deployment should run.

All three commands are safe to re-run.

### 7. Verify

```bash
./scripts/cluster_manager.py status
```

After `bootstrap` finishes, Argo CD reconciles the NVIDIA device plugin, its own Ingress, and other installed Applications — typically in under a minute. Watch:

```bash
ssh k3s-control sudo k3s kubectl -n argocd get applications
```

Then open **`https://argocd.apps.home.arpa`**. The initial admin password:

```bash
ssh k3s-control sudo k3s kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d
```

Change it immediately after first login.

## Local LLM stack architecture

Two `llama-server` pods backing the agent + RAG pipeline, fronted by a small request-logging proxy:

```
namespace: llama-cpp
┌────────────────────────────────┐    ┌────────────────────────────────┐
│  llama-chat  (GPU pod)         │    │  llama-embed  (CPU pod)        │
│  pinned to nvidia.com/gpu node │    │  ~140 MB nomic-embed-text      │
│  serves chat completions       │    │  serves /v1/embeddings         │
│  /v1/chat/completions, /v1/... │    │  /v1/embeddings, /metrics      │
└──────────────┬─────────────────┘    └──────────────┬─────────────────┘
               │                                     │
               ▼                                     │
       ┌───────────────┐                             │
       │   llm-proxy   │   JSONL request log +       │
       │  (logs/metrics)│  per-call latency metrics  │
       └───────┬───────┘                             │
               │  OpenAI-compatible HTTP             │
               │                                     │
       ┌───────┴───────┐                     ┌───────┴────────┐
       │    Hermes     │                     │  rag-indexer   │
       │   (hermes)    │  ◄──MCP /sse──┐     │ + rag-mcp      │
       └───────────────┘               │     │   (rag ns)     │
                                       └─────┤                │
                                             └────────────────┘
```

The proxy isn't load-balancing — it's sitting in the request path so every chat completion gets logged to a JSONL file (analyzable with `dev/analyze-prompts.py`) and exported as Prometheus metrics. Embeds bypass it; they're cheap and high-volume so the log noise outweighs the value.

### Two ConfigMaps for chat-model config

The chat server reads its env from two ConfigMaps merged in order:

| ConfigMap | Lives in | Lifecycle | Contains |
|---|---|---|---|
| `llama-cpp-defaults` | git (`apps/llama-cpp/configmap.yaml`) | Argo-managed | Embed model + sensible defaults for every chat knob (ctx 32768, ngl 999, parallel 1, kv q8_0, flash-attn on) |
| `llama-cpp-model` | imperatively created by `llama setup` | NOT in git, NOT reconciled | Per-deployment chat: `CHAT_MODEL_REPO`, `CHAT_MODEL_FILE`, `CHAT_SERVED_MODEL`, plus any tunables you've overridden |

The pod's `envFrom` lists both with `llama-cpp-model` second, so anything set there wins. This is what lets the upstream template carry NO opinion about which chat model your fork runs while still booting cleanly out of the box (you'll see a clear "run llama setup" crashloop on a fresh cluster).

`llama setup` writes both `llama-cpp-model` and `hermes-model` (the alias Hermes uses) atomically — they stay consistent. Subsequent quick swaps go through `llama set-chat`, targeted tweaks through `llama set-ctx` / `set-ngl` / etc. All of these survive Argo syncs because they touch the imperative ConfigMap, not the git-managed one.

### Tunable knobs

Every per-deployment knob is a first-class field with validation, exposed via dedicated CLI verbs:

| Field | CLI flag | Default | What it does |
|---|---|---|---|
| `CHAT_CTX_SIZE` | `--ctx N` / `set-ctx N` | 32768 | Context window in tokens |
| `CHAT_GPU_LAYERS` | `--ngl N` / `set-ngl N` | 999 (all) | Layers on GPU; lower = partial CPU offload |
| `CHAT_PARALLEL_SLOTS` | `--parallel N` / `set-parallel N` | 1 | Concurrent request slots; raising shrinks per-request ctx |
| `CHAT_KV_TYPE` | `--kv-type T` / `set-kv-type T` | q8_0 | KV cache dtype: `q8_0`, `q4_0`, `q5_0`, `f16` |
| `CHAT_FLASH_ATTN` | `--flash-attn V` / `set-flash-attn V` | on | `on`, `off`, `auto` |
| `CHAT_REASONING_BUDGET` | (configmap edit) | 0 | Token budget for thinking models. `0` = no thinking, `-1` = unrestricted. Default 0 because thinking-block tokens generated in one turn don't render identically on the next, blowing llama.cpp's prompt cache. |
| `CHAT_REPEAT_PENALTY` | (configmap edit) | 1.15 | Sampling penalty for repeated tokens. Tames Qwen3-A3B's degenerate-loop tendency on short-reply tasks (`HEARTBEAT_OKNO_REPLYNO_REPLY...`). Conservative band 1.0–1.3; >1.3 breaks legitimate repetition (proper nouns, boilerplate). |
| `CHAT_REPEAT_LAST_N` | (configmap edit) | 64 | Lookback window for the repeat penalty. Higher catches longer-range repetition; sampling cost rises linearly with the window. |
| `CHAT_CPU_MOE` | `--cpu-moe V` / `set-cpu-moe V` | off | `on` adds `--cpu-moe`: MoE expert FFN tensors stay in host RAM, only attention/router on GPU. Required to fit a 35B-class MoE on a 16 GB card. `off` for dense models. |
| `CHAT_N_CPU_MOE` | `--n-cpu-moe N` / `set-n-cpu-moe N` | 0 | Partial MoE offload: first N expert layers to CPU, the rest stay on GPU. Use when full `--cpu-moe` leaves VRAM headroom unused. 0 = off. Ignored if `CHAT_CPU_MOE=on`. |
| `CHAT_OVERRIDE_TENSOR` | `--override-tensor REGEX` / `set-override-tensor "<re>"` | "" | Tensor placement regex (advanced). Use when neither `--cpu-moe` nor `--n-cpu-moe` express the layout you want. |
| `CHAT_KV_UNIFIED` | `set-kv-unified V` | off | `on` adds `--kv-unified`: shared KV buffer pool across slots. Required to keep idle slot prefixes warm via `--cache-idle-slots`. Worth turning on for multi-slot setups where slot-switch evictions show up in TTFT. |
| `CHAT_EXTRA_FLAGS` | `--flags STR` / `set-flags "<str>"` | "" | Escape hatch: `--rope-scaling`, etc. — anything not yet promoted to its own field. |

### Tuning for your hardware

The pod's resource block in [`clusters/default/apps/llama-cpp/deployment-chat.yaml`](./clusters/default/apps/llama-cpp/deployment-chat.yaml) is sized for a single-purpose GPU node with **8+ CPU cores, 32 GB host RAM, and a 16 GB-class GPU** running an MoE model with `--cpu-moe` (expert weights in host RAM). If your GPU node is smaller, edit those numbers down before bootstrap or your llama-chat pod will OOMKill on first start.

| Constraint | What to change |
|---|---|
| Less than 16 GB VRAM | Lower `CHAT_GPU_LAYERS` (`llama set-ngl N`) for partial CPU offload, or pick a smaller-quant GGUF in `llama setup` |
| Less than 32 GB host RAM | Lower `resources.limits.memory` to ~70% of available, lower `requests.memory` proportionally (deployment-chat.yaml lines ~131-161) |
| Fewer CPU cores | Lower `resources.requests.cpu` and `limits.cpu` to your physical core count |
| Other workloads on the GPU node | Tighten the resource block so the scheduler leaves room |
| Thinking model + long agent loops | Keep `CHAT_REASONING_BUDGET=0` (default). Set to `-1` only if you genuinely need CoT and can absorb the cache invalidation per turn |

The CLI knobs (`set-ctx`, `set-ngl`, `set-parallel`, etc.) survive Argo syncs because they write to the imperative `llama-cpp-model` ConfigMap, not the git-managed defaults.

## Day-to-day operations

### Manage models

```bash
# Show the active chat + embed models, all tunable knobs, and what's
# cached on the PVCs. Keys marked `set` are in the imperative
# ConfigMap; `default` = falling through to llama-cpp-defaults.
./scripts/cluster_manager.py llama list

# First-time setup OR full reconfigure — prompts for repo, file, alias,
# ctx size, GPU layer offload, parallel slots, KV cache type, flash
# attention, MoE offloading (cpu-moe + n-cpu-moe), tensor-placement
# regex, and extra flags. Skips prompts for keys already set unless
# --force.
./scripts/cluster_manager.py llama setup

# Swap the chat model and optionally retune knobs in one shot.
# Required positional args: <hf-repo> <gguf-filename>. Every tunable
# option is optional; unspecified = keeps current value.
./scripts/cluster_manager.py llama set-chat \
    bartowski/Qwen_Qwen3-14B-GGUF Qwen_Qwen3-14B-Q5_K_M.gguf \
    --ctx 32768 --ngl 999 --kv-type q8_0

# Targeted single-knob tweaks (each restarts llama-chat once).
./scripts/cluster_manager.py llama set-ctx 65536
./scripts/cluster_manager.py llama set-ngl 52
./scripts/cluster_manager.py llama set-parallel 2
./scripts/cluster_manager.py llama set-kv-type q4_0
./scripts/cluster_manager.py llama set-flash-attn on
./scripts/cluster_manager.py llama set-cpu-moe on            # all expert FFN tensors to host RAM
./scripts/cluster_manager.py llama set-n-cpu-moe 28          # OR partial: first 28 layers to CPU
./scripts/cluster_manager.py llama set-override-tensor ""    # advanced: clear or set a regex
./scripts/cluster_manager.py llama set-flags "--rope-scaling yarn --rope-freq-scale 2.0"

# Swap the embed model (rare — changing dimensions means re-indexing
# the vault; see --wipe-rag in `restart`).
./scripts/cluster_manager.py llama set-embed \
    nomic-ai/nomic-embed-text-v1.5-GGUF nomic-embed-text-v1.5.Q8_0.gguf

# Tail logs
./scripts/cluster_manager.py llama logs chat -f
./scripts/cluster_manager.py llama logs chat -c pull-model -f   # init-container
```

### Check cluster status

```bash
./scripts/cluster_manager.py status
```

### Register a private apps repo

Argo CD can watch one or more private repositories alongside your instance repo — for applications you don't want in the (public) instance repo. Each private repo gets its own Argo CD AppProject, Repository Secret, root Application, and SSH deploy key; they coexist without interfering.

```bash
# 1. Scaffold a starter private apps repo on disk.
./scripts/cluster_manager.py private-apps scaffold ~/my-apps

# 2. Push it to a private git host of your choice (GitHub example):
cd ~/my-apps
git init && git add . && git commit -m "Initial scaffold"
gh repo create my-apps --private --source . --push

# 3. Register it with the cluster. Generates a per-project ed25519 deploy key,
#    prompts you to add the public key to the repo, then creates the Argo CD
#    Repository Secret + AppProject + root Application.
#
#    Project name is derived from the repo URL by default — in this example
#    it becomes "my-apps". Override with --project-name if you want.
./scripts/cluster_manager.py private-apps setup \
    --repo-url git@github.com:you/my-apps.git
```

Afterwards, anything you commit under `apps/<name>/` in the private repo is picked up by Argo automatically. Each `apps/<name>/` can either be raw manifests OR a child Argo Application pointing elsewhere (e.g. a separate repo with a Helm chart). See the scaffold's `README.md` for both patterns.

List what's registered:

```bash
./scripts/cluster_manager.py private-apps list
```

Outputs a table with project name, repo URL, and Argo sync/health status.

Remove one:

```bash
./scripts/cluster_manager.py private-apps unregister --project-name my-apps
```

(Does not delete the SSH key on disk or the deploy key on your git host — remove those manually if no longer needed.)

#### Multiple private repos

Register as many as you want — one setup invocation per repo:

```bash
./scripts/cluster_manager.py private-apps setup --repo-url git@github.com:you/homelab-apps.git
./scripts/cluster_manager.py private-apps setup --repo-url git@github.com:you/work-apps.git
```

Setup refuses if a project with the same derived (or explicit) name already points at a different URL, and lists current registrations so you can pick a different `--project-name` or unregister the conflicting one first.

### Connect Telegram (optional)

```bash
./scripts/cluster_manager.py setup-telegram
```

Prompts for your bot token from BotFather. Stores it in `hermes-secrets` and restarts Hermes. `remove-telegram` deletes it. The `--target <agent>` flag exists for future multi-agent setups; with only Hermes registered today the default just works.

### Set up Obsidian Sync workspace (optional)

Hermes runs an `obsidian-sync` sidecar in its own namespace pulling from upstream Obsidian Sync into the `hermes-vault` PVC. The agent reads/writes notes at `/vault`.

```bash
# 1. Get your auth token (one-time, interactive — prompts for email/password/MFA)
docker run --rm -it --entrypoint get-token ghcr.io/belphemur/obsidian-headless-sync-docker:latest

# 2. Configure the cluster
./scripts/cluster_manager.py setup-obsidian
```

The sync pod keeps `hermes-vault` in lockstep with the upstream Obsidian Sync vault. Hermes reads from `/vault` as its workspace, alongside its own `/opt/data` for runtime state (sessions, memories, the FTS5 SQLite session store).

### Connect Claude Code to Grafana (logs + metrics MCP)

The cluster ships Loki + Alloy out of the box — every pod's logs land in Loki with 7-day retention. Wire that into Claude Code via the [Grafana MCP server](https://github.com/grafana/mcp-grafana) so Claude can run LogQL/PromQL queries while you debug a freshly deployed service.

```bash
# 1. Provision a Garage S3 bucket + Secret for Loki (one-time, after first
#    `bootstrap-infra-secrets`). Names align with what `loki.yaml` expects:
./scripts/cluster_manager.py provision-s3-app \
    --app loki --namespace loki --buckets loki-chunks

# 2. Mint a Viewer-role Grafana service account token and print a
#    ready-to-paste Claude Code MCP config:
./scripts/cluster_manager.py setup-grafana-mcp

# 3. Install the mcp-grafana binary on your workstation:
brew install grafana/grafana/mcp-grafana
# or download v0.13.1+ from https://github.com/grafana/mcp-grafana/releases
#   and put it on your $PATH.
```

Paste the printed JSON into `~/.claude.json` (or any project's `.mcp.json`) and restart Claude Code. The session will have a `grafana` MCP server with tools for querying Loki logs, Prometheus metrics, listing dashboards, and a few more. The token is read-only — write/delete operations against Grafana fail with 403.

Self-signed wildcard cert means the snippet sets `GRAFANA_TLS_SKIP_VERIFY=true`. Fine for LAN-only homelab; flip it back if Grafana ever leaves the LAN.

### Convert instance repo from public to private

Mirrors the flow used by `private-apps setup`: a per-repo ed25519 deploy key, paired with an Argo Repository Secret, replacing anonymous-HTTPS git access. Useful when an instance repo started public (e.g. while iterating on the upstream template) and you now want to keep its history off the public internet.

```bash
# 1. On GitHub, change your remote URL to SSH locally so init-fork picks it up.
cd ~/Documents/projects/my-cluster
git remote set-url origin git@github.com:you/my-cluster.git

# 2. Re-run init-fork. It detects the prior HTTPS URL in the manifests and
#    rewrites every repoURL: https://...your-repo... line to the new SSH URL,
#    then generates ~/.ssh/argocd-instance-my-cluster.key and prints the
#    public key.
./scripts/cluster_manager.py init-fork

# 3. Add the printed public key as a deploy key on GitHub.
#    Settings → Deploy keys → Add deploy key. Read-only is fine.

# 4. Apply the matching Argo Repository Secret AND patch the running root
#    Application's repoURL so Argo can re-sync via the new URL immediately
#    (without waiting on a manifest push).
./scripts/cluster_manager.py setup-instance-repo

# 5. Push the manifest changes from step 2.
git commit -am "Switch to SSH repoURL"
git push

# 6. Verify Argo is syncing cleanly via the new URL.
ssh k3s-control "sudo k3s kubectl -n argocd get app root -o jsonpath='{.status.sync.status}'"

# 7. Now flip the repo to private on GitHub:
#    Settings → General → Danger Zone → Change visibility → Private
```

`init-fork` is idempotent for the URL substitution — it only rewrites `repoURL: <prior-URL>` lines, so other repos referenced in your manifests (e.g. a deal-signal chart Application pointing at a separate repo) aren't touched. `setup-instance-repo` is also idempotent and safe to re-run.

### Sync upstream improvements

When the public template gets bug fixes or new features:

```bash
./scripts/cluster_manager.py sync-upstream
git push
```

This fetches from `upstream/main`, merges, and re-runs `init-fork` to replace any new placeholders that came with the merge. If there are merge conflicts, resolve them manually, then run `./scripts/cluster_manager.py init-fork && git commit`.

### Add a new app

Pure git workflow — no Ansible, no DNS:

1. Create `clusters/default/apps/<name>/` with raw Kubernetes manifests (include an Ingress for `<name>.apps.home.arpa` if you want a hostname).
2. For AI workloads, include `nodeSelector: nvidia.com/gpu: "true"` and the matching toleration.
3. Create `clusters/default/applications/children/<name>.yaml` — an Argo `Application` pointing at that path.
4. Commit + push. Argo picks it up automatically via `selfHeal: true`.

### Add a new node

1. Install Ubuntu on the new machine.
2. `./scripts/cluster_manager.py prep-node <ip>` — prompts for hostname and role, adds to inventory, preps the node.
3. `./scripts/cluster_manager.py bootstrap` — idempotent; only the new node actually changes.

## The CLI

`scripts/cluster_manager.py` is the single entrypoint. Run `./scripts/cluster_manager.py --help` (or `<cmd> --help`) for full options on any command.

### Bootstrap + nodes

| Command | Purpose |
|---|---|
| `init-fork [URL] [--apps-domain D]` | One-time: rewrite `REPO_URL` + `APPS_DOMAIN` placeholders in cluster manifests. |
| `prep-node <ip> [--hostname H] [--role R]` | Add node to inventory, authorize SSH key, run prep playbook (apt upgrade, hostname, NVIDIA). |
| `bootstrap [-- <ansible-args>]` | Install/upgrade k3s on every node + Argo CD on control. Idempotent and version-aware (re-runs the installer when `k3s_version` in `group_vars/all.yml` differs from what's on the node). |
| `remove-node <hostname>` | Cordon, drain, uninstall k3s, and remove from inventory. |
| `sync-upstream [--remote R] [--branch B]` | Pull upstream changes into your instance repo and re-apply `init-fork` placeholders. |

### Secrets + per-deployment config

| Command | Purpose |
|---|---|
| `setup-secrets` | Generate wildcard TLS cert + Grafana admin password. Idempotent. |
| `bootstrap-infra-secrets` | Generate Garage's auth tokens, create the `garage-auth` Secret, apply the single-node Garage layout. |
| `setup-instance-repo` | Apply the Argo Repository Secret + patch live root Application when the instance repo is private (SSH). No-op for HTTPS repos. |
| `setup-grafana-mcp [--rotate]` | Create a Viewer-role Grafana service account `claude-mcp`, mint a token, and print a Claude Code MCP config snippet. Pairs with the `mcp-grafana` binary running locally. `--rotate` deletes the existing service account and issues a fresh token. |
| `llama setup` | Interactive prompt for every chat-model knob — repo / file / served-as alias / ctx / GPU layers / parallel slots / KV cache type / flash-attn / `--cpu-moe` / `--n-cpu-moe` / `--override-tensor` / extra flags. Writes the imperative `llama-cpp/llama-cpp-model` ConfigMap + keeps `hermes/hermes-model` in sync. |

### Day-to-day

| Command | Purpose |
|---|---|
| `status` | `kubectl get nodes,pods -A` via SSH to the control node. |
| `restart [--wipe-rag]` | Restart the full app stack in the right order. `--wipe-rag` deletes ChromaDB data and re-indexes. |
| `llama list` | Show active chat + embed models, every tunable knob (with `set` vs `default` markers), and what's cached on the PVCs. |
| `llama set-chat <repo> <file> [--ctx N --ngl N --parallel N --kv-type T --flash-attn V --cpu-moe V --n-cpu-moe N --override-tensor RE --served-as NAME --flags STR]` | Swap chat model and optionally retune any knob in one shot. Warns + prompts when the model changes without `--served-as` (use `--keep-alias` to suppress). |
| `llama set-ctx N` / `set-ngl N` / `set-parallel N` / `set-kv-type T` / `set-flash-attn on│off│auto` / `set-cpu-moe on│off` / `set-n-cpu-moe N` / `set-override-tensor "<re>"` / `set-flags "<str>"` | Targeted single-knob tweaks. Each restarts `llama-chat`. |
| `llama set-embed <repo> <file>` | Swap embed model. Changing dimensions requires `restart --wipe-rag`. |
| `llama logs <chat│embed> [-c <container>] [-f]` | Tail llama-chat / llama-embed pod logs. `-c pull-model` for the GGUF init container. |

### App + integration plumbing

| Command | Purpose |
|---|---|
| `app-provision <name>` | Run a per-app provisioning manifest — idempotent one-stop for buckets, image-pull secrets, repo secrets. |
| `provision-s3-app <name>` | Create a Garage bucket + access key + Kubernetes Secret for one app. |
| `add-image-pull-secret <namespace> <name>` | Provision an image-pull Secret for a private registry. |
| `add-repo-secret <name>` | Register a git repository with Argo CD as a Repository Secret. |
| `setup-telegram [--target <agent>]` / `remove-telegram [--target <agent>]` | Configure or remove the Telegram bot token for the agent's Secret. Default target is `hermes`. One bot per polling client — migrate a token by `remove`-ing from the old target first, then `setup`-ing on the new one. |
| `setup-obsidian [--target <agent>]` | Configure Obsidian Sync for the agent's workspace. Default target is `hermes`. The agent's `obsidian-sync` sidecar pulls into its vault PVC, kept aligned via upstream Obsidian Sync. |
| `remove-obsidian [--target <agent>]` | Stop syncing for the agent — removes the auth-token Secret key + deletes the obsidian-config ConfigMap, restarts the sync sidecar if present. Vault PVC contents preserved. |
| `approve-pairing <channel> <code> [--target <agent>]` | Approve a user's pairing request (e.g. `telegram HPP2WU9B`). Default target is `hermes`. The agent replies to a new chat with a pairing code; the operator approves it here. |
| `allow-user <id> [--target <agent>]` | Allowlist a numeric Telegram user ID (or comma-separated list). Get your numeric ID from `@userinfobot` on Telegram. Each call REPLACES the existing list. |

### Private apps repos

| Command | Purpose |
|---|---|
| `private-apps scaffold <path>` | Scaffold a starter private apps repo on disk. |
| `private-apps setup --repo-url <url>` | Generate a deploy key, register the repo with Argo CD as an AppProject + root Application. |
| `private-apps list` | List every registered private apps repo with sync/health status. |
| `private-apps unregister --project-name <name>` | Remove the Argo Application + AppProject + Repository Secret. |

### What `prep.yml` does
1. `base` on every targeted host — apt upgrade, utilities, unattended-upgrades, set hostname (then DHCP renew so the router registers `<name>`).
2. `nvidia` on hosts in `[gpu]` — install driver (autodetected via `ubuntu-drivers`) + NVIDIA Container Toolkit. Auto-reboots if a new driver was installed.
3. `storage` on hosts in `[storage]` — prepare the local-path storage directory + fstrim service.

### What `cluster.yml` does
1. `k3s-server` on control — installs k3s, OR upgrades it in-place if `k3s_version` in `group_vars/all.yml` doesn't match what's on disk. Captures the join token. The `--check` mode (`bootstrap -- --check`) reports whether an upgrade would fire without performing it.
2. `k3s-agent` on every agent — joins the cluster, OR upgrades in-place under the same version-mismatch rules as the server. GPU nodes also get the `nvidia.com/gpu=true` label + `NoSchedule` taint and containerd NVIDIA runtime config; storage nodes get `role=storage:NoSchedule`.
3. `argocd` on control — installs Argo CD (pinned), sets `server.insecure=true` for HTTP Ingress, and applies the root Application of the app-of-apps tree.

## Repo layout

```
.
├── README.md
├── requirements.txt                    # Python deps for the CLI
├── scripts/
│   ├── cluster_manager.py              # typer CLI (every command above)
│   └── private_apps_template/          # starter content for `private-apps scaffold`
├── rag-indexer/                        # Vault → ChromaDB indexer (built via CI)
├── rag-mcp/                            # MCP server: search_notes, list_recent_notes, read_note
├── ansible/
│   ├── ansible.cfg
│   ├── inventory.ini.example           # committed template (public)
│   ├── inventory.ini                   # your real inventory (instance repo only)
│   ├── prep.yml                        # per-node: base + nvidia + storage
│   ├── cluster.yml                     # cluster-wide: k3s + argocd
│   ├── group_vars/all.yml              # pinned versions, apps_domain
│   └── roles/
│       ├── base/                       # apt, hostname, unattended-upgrades
│       ├── nvidia/                     # GPU only; auto-reboots
│       ├── storage/                    # storage-node prep (local-path dir, fstrim)
│       ├── k3s-server/                 # version-aware: install or in-place upgrade
│       ├── k3s-agent/                  # version-aware; GPU variant adds label/taint/runtime
│       └── argocd/                     # installs Argo CD, applies root Application
└── clusters/
    └── default/
        ├── applications/
        │   ├── root.yaml               # app-of-apps, applied by Ansible
        │   └── children/               # reconciled by root
        │       ├── alloy.yaml          # Grafana Alloy DaemonSet — pod log shipper
        │       ├── alloy-config.yaml   # Alloy scrape config (River) reconciled from git
        │       ├── argocd-ingress.yaml
        │       ├── chromadb.yaml
        │       ├── cloudnative-pg.yaml  # CNPG operator (Helm; no Cluster CR)
        │       ├── dcgm-exporter.yaml
        │       ├── garage.yaml          # single-node Garage, pinned to storage node
        │       ├── grafana-dashboards.yaml
        │       ├── kube-prometheus-stack.yaml
        │       ├── llama-cpp.yaml       # llama-chat + llama-embed pods
        │       ├── loki.yaml            # Loki SingleBinary, chunks → Garage S3
        │       ├── node-feature-discovery.yaml
        │       ├── nvidia-device-plugin.yaml
        │       ├── hermes.yaml          # the agent runtime
        │       ├── rag-obsidian-sync.yaml  # rag-side vault sync (independent of hermes)
        │       ├── prometheus-crds.yaml
        │       ├── rag-indexer.yaml
        │       ├── rag-mcp.yaml
        │       └── traefik-tls.yaml
        └── apps/                       # raw k8s manifests, reconciled by Argo
            ├── alloy/                   # ConfigMap with Alloy scrape config (config.alloy)
            ├── argocd-ingress/
            ├── chromadb/                # vector DB for the RAG pipeline
            ├── garage/                  # configmap + service + statefulset
            ├── grafana-dashboards/      # ConfigMap-shipped dashboards (LLM, GPU, nodes)
            ├── llama-cpp/               # default ConfigMap + chat + embed deployments
            ├── hermes/                   # the agent runtime
            │                             #   - configmap.yaml: hermes-model active served-as
            │                             #   - obsidian-sync.yaml: agent-side vault sync sidecar
            │                             #   - deployment.yaml: bootstrap-config init container
            │                             #     overlays config.yaml from configmap on every boot
            ├── rag-obsidian-sync/        # rag-side vault sync — own PVC, own sidecar.
            │                             # RAG pipeline is fully decoupled from any agent.
            ├── rag-indexer/             # CronJob/Deployment that indexes the vault
            ├── rag-mcp/                 # MCP search server backed by ChromaDB
            └── traefik-tls/             # wildcard TLS Secret for *.APPS_DOMAIN
```

## Version pinning

Versions live in two places:

**`ansible/group_vars/all.yml`** — the pieces installed by the Ansible roles. Bumping a value here and re-running `./scripts/cluster_manager.py bootstrap` triggers an in-place upgrade on every node:

| Component | Variable | Version |
|---|---|---|
| k3s | `k3s_version` | `v1.35.3+k3s1` |
| Argo CD | `argocd_version` | `v3.0.23` |
| NVIDIA device plugin Helm chart | `nvidia_device_plugin_chart_version` | `0.17.4` |
| Node Feature Discovery Helm chart | `nfd_chart_version` | `0.18.3` |

**Per-app Argo Application manifests** (`clusters/default/applications/children/`) — Helm charts and images for everything Argo manages. Edit the `targetRevision` / `image` field in the relevant child YAML and Argo reconciles. Currently:

| Component | Where | Version |
|---|---|---|
| kube-prometheus-stack | `children/kube-prometheus-stack.yaml` | `83.6.0` |
| Prometheus Operator CRDs | `children/prometheus-crds.yaml` | `28.0.1` |
| DCGM exporter | `children/dcgm-exporter.yaml` | `4.4.1` |
| CloudNativePG operator | `children/cloudnative-pg.yaml` | `0.24.0` |
| ChromaDB image | `apps/chromadb/deployment.yaml` | `1.5.8` |
| Garage image | `apps/garage/statefulset.yaml` | `dxflrs/garage:v1.2.0` |
| llama.cpp server (chat + embed) | `apps/llama-cpp/deployment-{chat,embed}.yaml` | `ghcr.io/ggml-org/llama.cpp:server-cuda-b8895` |
| Hermes agent | `apps/hermes/deployment.yaml` | `docker.io/nousresearch/hermes-agent:v2026.4.23` |

Chat-model GGUF + tunables are deliberately NOT pinned in git — see [Local LLM stack](#local-llm-stack-architecture) below.

## Known sharp edges

- **Unattended-upgrades on the GPU node can break `nvidia-smi`.** A kernel upgrade without a DKMS rebuild silently breaks GPU access. Re-run `prep-node <gpu-host>` — the `nvidia` role will reinstall drivers.
- **local-path PVCs don't survive OS reinstall.** Reinstalling the GPU node's OS means re-pulling models. By design — treat the OS as ephemeral.
- **Self-signed TLS cert.** `setup-secrets` generates a wildcard cert for `*.APPS_DOMAIN`. Your browser will show a warning on first visit — accept it once per browser.
- **In-cluster LLM inference has no auth.** LAN only. See top-of-readme warning.

## Key decisions

| Decision | Choice | Reason |
|---|---|---|
| Repo model | Public template + instance repo | Generic upstream stays clean; instance holds your config |
| Cluster topology | 1 server + N agents, no HA | Simple; rebuild on failure |
| Control plane placement | Non-GPU node | Stable; GPU node can reboot freely |
| GPU scheduling | Label + taint + toleration on `nvidia.com/gpu` | Matches NVIDIA GPU Operator convention |
| Bootstrap driver | Ansible behind a typer CLI | Idempotent roles, one operator entrypoint |
| Node addressability | Router DNS (`<name>`) | No DHCP-reservation bookkeeping |
| App addressability | Wildcard DNS (`*.apps.home.arpa`) + Traefik Ingress | One-time DNS; new apps add no manual steps |
| GitOps tool | Argo CD | UI is useful; app-of-apps pattern |
| App delivery | Committed Application manifests + `init-fork` | Adding apps is pure git |
| Model storage | Persistent local-path PVC on GPU node | Ephemeral = OS; don't re-pull large models |
| External access | LAN only (HTTPS Ingress, self-signed) | No public exposure; secure context for web apps |
| Secrets | Kubernetes Secrets created imperatively by CLI (`setup-secrets`, `bootstrap-infra-secrets`) | Not in git; migrate to Sealed Secrets / ExternalSecrets later |
| Private apps | Separate repo watched by Argo, wired up via `private-apps setup` | Keeps private/personal workloads out of the public template and any public instance forks |
| Shared infra vs. apps | Operator + object store only in this repo; no `Cluster` CRs or buckets | Consumer apps (private) create their own DB clusters + buckets in their own namespaces |
| Model management | Defaults in git (`llama-cpp-defaults`), per-deployment chat model in imperative `llama-cpp-model` ConfigMap | Upstream template carries no model opinion; CLI edits stick across Argo syncs |
| Model weights | Pulled from HuggingFace by an init container, cached on a local-path PVC | First boot is a one-time download; rollouts reuse the cached blob |
| Version pinning | Ansible-installed components in `group_vars/all.yml`, Argo-managed in per-app YAML | Each layer pinned at the point it's owned |

## Non-goals

- HA control plane
- Public internet exposure
- CA-signed TLS (self-signed is sufficient for LAN)
- Backup of model weights (re-pull from HuggingFace if you need them after a node OS reinstall)
- A frontier-class chat model in the box. The defaults work for ~14B-class GGUFs on a 16 GB GPU; bigger/better needs a bigger card or a hosted-API provider configured in Hermes.

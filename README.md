# k8s Cluster Bootstrap

Ephemeral, reproducible-from-git k3s cluster for a small fleet of Ubuntu boxes — optionally with NVIDIA GPU nodes for AI workloads. Create an instance repo from this template, edit an inventory, run a handful of commands: you end up with a k3s cluster managed by Argo CD, a full local LLM stack (llama.cpp chat + embed servers, OpenClaw agent, RAG pipeline, ChromaDB), and infra (CloudNativePG, Garage S3, Prometheus + Grafana) pre-wired and reachable on your LAN.

> **LAN-only by design.** In-cluster services (LLM inference, Ingress endpoints) don't carry their own authentication. Do NOT deploy this on a cloud VM, a box with a public IP, or any network you don't fully trust without adding your own auth layer.

## What you get

**Cluster + GitOps**
- **k3s** cluster: 1 server + N agents (no HA). Node roles: `control`, `worker`, `gpu`, `storage`.
- **Argo CD** on the control node, reconciling from your instance repo via the app-of-apps pattern — at `https://argocd.apps`
- **Traefik Ingress** (shipped with k3s) fronted by one wildcard DNS record — new apps never require touching the router

**Local LLM stack** (all runs on your hardware, no API calls out)
- **llama.cpp** chat server on the GPU node + embed server on CPU. OpenAI-compatible `/v1` API. Init containers pull GGUF weights from HuggingFace on first boot.
- **OpenClaw** agent at `https://openclaw.apps` with built-in MCP support, Slack + Telegram channels, Obsidian-vault workspace
- **RAG pipeline**: ChromaDB + a vault indexer + an MCP server exposing semantic search over your notes
- All model + tunable settings managed via `cluster_manager.py llama …` — chat model choice is per-deployment (the upstream template has no opinion)

**Infrastructure**
- **CloudNativePG operator** in `cnpg-system` — consumer apps create their own `Cluster` CRs; no databases installed by default
- **Garage (S3-compatible object store)** pinned to the storage node with `local-path` PVC — consumer apps provision their own buckets via `provision-s3-app`
- **Prometheus + Grafana** at `https://grafana.apps` with node + GPU + LLM-inference dashboards out of the box
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
                   GPU-pinned: llama-chat, dcgm-exporter, nfd-worker
                   Storage-pinned: garage, CNPG cluster PVCs
                   Everything else (control plane + light pods): control
```

- **One server, no HA.** If it dies, rebuild from git.
- **GPU node is tainted** with `nvidia.com/gpu=true:NoSchedule` so random workloads don't steal its resources. Only pods that explicitly tolerate the taint and pin to it (the chat LLM, GPU monitoring) land there.
- **Storage node is tainted** with `role=storage:NoSchedule` for the same reason — Garage and any DB volume PVCs pin there.
- **Minimum useful cluster** is 1 control + 1 GPU; the storage node is optional (its pods fall back to the control node when absent). Scale workers however you like.

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
- OpenClaw gateway token (save it — needed for the web UI)
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
- `llama-cpp/llama-cpp-model` ConfigMap (CHAT_MODEL_REPO/FILE/alias/ctx/flags)
- `openclaw/openclaw-model` ConfigMap (active-model, kept in sync with the alias)

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

Then open **`https://argocd.apps`**. The initial admin password:

```bash
ssh k3s-control sudo k3s kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d
```

Change it immediately after first login.

## Local LLM stack architecture

Two `llama-server` pods backing the agent + RAG pipeline:

```
namespace: llama-cpp
┌────────────────────────────────┐    ┌────────────────────────────────┐
│  llama-chat  (GPU pod)         │    │  llama-embed  (CPU pod)        │
│  pinned to nvidia.com/gpu node │    │  ~140 MB nomic-embed-text      │
│  serves chat completions       │    │  serves /v1/embeddings         │
│  /v1/chat/completions, /v1/... │    │  /v1/embeddings, /metrics      │
└──────────────┬─────────────────┘    └──────────────┬─────────────────┘
               │                                     │
               │  OpenAI-compatible HTTP             │
               │                                     │
       ┌───────┴───────┐                     ┌───────┴────────┐
       │   OpenClaw    │                     │  rag-indexer   │
       │  (openclaw)   │  ◄──MCP /sse──┐     │ + rag-mcp      │
       └───────────────┘               │     │  (openclaw ns) │
                                       └─────┤                │
                                             └────────────────┘
```

### Two ConfigMaps for chat-model config

The chat server reads its env from two ConfigMaps merged in order:

| ConfigMap | Lives in | Lifecycle | Contains |
|---|---|---|---|
| `llama-cpp-defaults` | git (`apps/llama-cpp/configmap.yaml`) | Argo-managed | Embed model + sensible defaults for every chat knob (ctx 32768, ngl 999, parallel 1, kv q8_0, flash-attn on) |
| `llama-cpp-model` | imperatively created by `llama setup` | NOT in git, NOT reconciled | Per-deployment chat: `CHAT_MODEL_REPO`, `CHAT_MODEL_FILE`, `CHAT_SERVED_MODEL`, plus any tunables you've overridden |

The pod's `envFrom` lists both with `llama-cpp-model` second, so anything set there wins. This is what lets the upstream template carry NO opinion about which chat model your fork runs while still booting cleanly out of the box (you'll see a clear "run llama setup" crashloop on a fresh cluster).

`llama setup` writes both `llama-cpp-model` and `openclaw-model` (the alias OpenClaw advertises) atomically — they stay consistent. Subsequent quick swaps go through `llama set-chat`, targeted tweaks through `llama set-ctx` / `set-ngl` / etc. All of these survive Argo syncs because they touch the imperative ConfigMap, not the git-managed one.

### Tunable knobs

Every per-deployment knob is a first-class field with validation, exposed via dedicated CLI verbs:

| Field | CLI flag | Default | What it does |
|---|---|---|---|
| `CHAT_CTX_SIZE` | `--ctx N` / `set-ctx N` | 32768 | Context window in tokens |
| `CHAT_GPU_LAYERS` | `--ngl N` / `set-ngl N` | 999 (all) | Layers on GPU; lower = partial CPU offload |
| `CHAT_PARALLEL_SLOTS` | `--parallel N` / `set-parallel N` | 1 | Concurrent request slots; raising shrinks per-request ctx |
| `CHAT_KV_TYPE` | `--kv-type T` / `set-kv-type T` | q8_0 | KV cache dtype: `q8_0`, `q4_0`, `q5_0`, `f16` |
| `CHAT_FLASH_ATTN` | `--flash-attn V` / `set-flash-attn V` | on | `on`, `off`, `auto` |
| `CHAT_EXTRA_FLAGS` | `--flags STR` / `set-flags "<str>"` | "" | Escape hatch: `--rope-scaling`, `--override-tensor`, sampling defaults, etc. |

## Day-to-day operations

### Manage models

```bash
# Show the active chat + embed models, all tunable knobs, and what's
# cached on the PVCs. Keys marked `set` are in the imperative
# ConfigMap; `default` = falling through to llama-cpp-defaults.
./scripts/cluster_manager.py llama list

# First-time setup OR full reconfigure — prompts for repo, file, alias,
# ctx size, GPU layer offload, parallel slots, KV cache type, flash
# attention, and extra flags. Skips prompts for keys already set
# unless --force.
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

### Connect Slack (optional)

```bash
./scripts/cluster_manager.py setup-slack
```

Prompts for your Slack Bot Token (`xoxb-...`) and App Token (`xapp-...`) from https://api.slack.com/apps. Stores them in the cluster Secret, restarts OpenClaw. Run again to rotate tokens; `remove-slack` to delete.

### Connect Telegram (optional)

```bash
./scripts/cluster_manager.py setup-telegram
```

Prompts for your bot token from BotFather. Stores it, restarts OpenClaw. `remove-telegram` to delete.

### Set up Obsidian Sync workspace (optional)

OpenClaw's workspace can be synced with your Obsidian vault via the official headless sync client. Edit notes on any device — they appear in the agent's workspace automatically.

```bash
# 1. Get your auth token (one-time, interactive — prompts for email/password/MFA)
docker run --rm -it --entrypoint get-token ghcr.io/belphemur/obsidian-headless-sync-docker:latest

# 2. Configure the cluster with the token and vault name
./scripts/cluster_manager.py setup-obsidian
```

The sync pod runs continuously in the `openclaw` namespace, keeping the vault PVC in sync with Obsidian Sync. OpenClaw reads and writes to the same PVC as its workspace.

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

1. Create `clusters/default/apps/<name>/` with raw Kubernetes manifests (include an Ingress for `<name>.apps` if you want a hostname).
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
| `setup-secrets` | Generate wildcard TLS cert, OpenClaw gateway token, Grafana admin password. Idempotent. |
| `bootstrap-infra-secrets` | Generate Garage's auth tokens, create the `garage-auth` Secret, apply the single-node Garage layout. |
| `setup-instance-repo` | Apply the Argo Repository Secret + patch live root Application when the instance repo is private (SSH). No-op for HTTPS repos. |
| `llama setup` | Interactive prompt for chat-model repo / file / alias / context size / GPU layers / parallel slots / KV cache type / flash-attn / extra flags. Writes the imperative `llama-cpp/llama-cpp-model` ConfigMap + keeps `openclaw/openclaw-model` in sync. |

### Day-to-day

| Command | Purpose |
|---|---|
| `status` | `kubectl get nodes,pods -A` via SSH to the control node. |
| `restart [--wipe-rag]` | Restart the full app stack in the right order. `--wipe-rag` deletes ChromaDB data and re-indexes. |
| `llama list` | Show active chat + embed models, every tunable knob (with `set` vs `default` markers), and what's cached on the PVCs. |
| `llama set-chat <repo> <file> [--ctx N --ngl N --kv-type T --parallel N --flash-attn V --served-as NAME --flags STR]` | Swap chat model and optionally retune knobs in one shot. Warns + prompts when the model changes without `--served-as` (use `--keep-alias` to suppress). |
| `llama set-ctx N` / `set-ngl N` / `set-parallel N` / `set-kv-type TYPE` / `set-flash-attn on│off│auto` / `set-flags "<str>"` | Targeted single-knob tweaks. Each restarts `llama-chat`. |
| `llama set-embed <repo> <file>` | Swap embed model. Changing dimensions requires `restart --wipe-rag`. |
| `llama logs <chat│embed> [-c <container>] [-f]` | Tail llama-chat / llama-embed pod logs. `-c pull-model` for the GGUF init container. |

### App + integration plumbing

| Command | Purpose |
|---|---|
| `app-provision <name>` | Run a per-app provisioning manifest — idempotent one-stop for buckets, image-pull secrets, repo secrets. |
| `provision-s3-app <name>` | Create a Garage bucket + access key + Kubernetes Secret for one app. |
| `add-image-pull-secret <namespace> <name>` | Provision an image-pull Secret for a private registry. |
| `add-repo-secret <name>` | Register a git repository with Argo CD as a Repository Secret. |
| `setup-slack` / `remove-slack` | Configure or remove Slack bot + app tokens for OpenClaw. |
| `setup-telegram` / `remove-telegram` | Configure or remove Telegram bot token for OpenClaw. |
| `setup-obsidian` | Configure Obsidian Sync for the OpenClaw workspace. |
| `approve-pairing <channel> <code>` | Approve a user's pairing request (e.g. `slack HPP2WU9B`). |

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
        │       ├── argocd-ingress.yaml
        │       ├── chromadb.yaml
        │       ├── cloudnative-pg.yaml  # CNPG operator (Helm; no Cluster CR)
        │       ├── dcgm-exporter.yaml
        │       ├── garage.yaml          # single-node Garage, pinned to storage node
        │       ├── grafana-dashboards.yaml
        │       ├── kube-prometheus-stack.yaml
        │       ├── llama-cpp.yaml       # llama-chat + llama-embed pods
        │       ├── node-feature-discovery.yaml
        │       ├── nvidia-device-plugin.yaml
        │       ├── obsidian-sync.yaml
        │       ├── openclaw.yaml
        │       ├── prometheus-crds.yaml
        │       ├── rag-indexer.yaml
        │       ├── rag-mcp.yaml
        │       └── traefik-tls.yaml
        └── apps/                       # raw k8s manifests, reconciled by Argo
            ├── argocd-ingress/
            ├── chromadb/                # vector DB for the RAG pipeline
            ├── garage/                  # configmap + service + statefulset
            ├── grafana-dashboards/      # ConfigMap-shipped dashboards (LLM, GPU, nodes)
            ├── llama-cpp/               # default ConfigMap + chat + embed deployments
            ├── obsidian-sync/           # Headless Obsidian Sync for workspace
            ├── openclaw/                # the agent gateway
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
| OpenClaw | `openclaw_image` | `ghcr.io/openclaw/openclaw:2026.4.23` |
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
| App addressability | Wildcard DNS (`*.apps`) + Traefik Ingress | One-time DNS; new apps add no manual steps |
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
- A frontier-class chat model in the box. The defaults work for ~14B-class GGUFs on a 16 GB GPU; bigger/better needs a bigger card or a hosted-API provider in OpenClaw.

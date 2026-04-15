#!/usr/bin/env bash
# Run from your workstation (Mac/Linux) — NOT on the target nodes.
# Provisions a 3-node k3s cluster (1 control + 2 agents including GPU) via SSH.
# Idempotent: safe to re-run.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "=== GPU Workstation Homelab Bootstrap ==="

if [[ ! -f ansible/inventory.ini ]]; then
    echo
    echo "ERROR: ansible/inventory.ini not found."
    echo "Copy the example and edit with your nodes' IPs/hostnames:"
    echo
    echo "  cp ansible/inventory.ini.example ansible/inventory.ini"
    echo "  \$EDITOR ansible/inventory.ini"
    echo
    exit 1
fi

if grep -rq 'repoURL: REPO_URL' clusters 2>/dev/null; then
    echo
    echo "ERROR: Argo Application manifests still contain REPO_URL placeholder."
    echo "Run: ./scripts/init-fork.sh"
    echo
    exit 1
fi

if ! command -v ansible-playbook &>/dev/null; then
    echo
    echo "ERROR: Ansible not installed. Install it first:"
    echo "  macOS:  brew install ansible"
    echo "  Linux:  sudo apt install ansible  (or: pipx install ansible-core)"
    echo
    exit 1
fi

echo "Running playbook against inventory..."
ansible-playbook -i ansible/inventory.ini ansible/site.yml --ask-become-pass "$@"

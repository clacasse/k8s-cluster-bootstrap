#!/usr/bin/env bash
# Entrypoint run on a fresh Ubuntu install. Idempotent - safe to re-run.
# If a reboot is required (NVIDIA driver install), script prompts and exits.
# Re-run after reboot to continue.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "=== GPU Workstation Bootstrap ==="
echo "Repo: $REPO_DIR"
echo

if [[ $EUID -eq 0 ]]; then
    echo "Do not run as root. Run as your normal user; sudo is invoked where needed."
    exit 1
fi

if ! command -v ansible-playbook &>/dev/null; then
    echo "Installing Ansible..."
    sudo apt-get update
    sudo apt-get install -y ansible
fi

echo "Running playbook..."
ansible-playbook -i ansible/inventory.ini ansible/site.yml --ask-become-pass "$@"

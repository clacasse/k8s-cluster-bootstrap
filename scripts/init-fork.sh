#!/usr/bin/env bash
# Run once after forking + cloning, before bootstrap.sh.
# Replaces the REPO_URL placeholder in Argo Application manifests with your
# fork's actual URL (auto-detected from the local git remote).
#
# Usage: ./scripts/init-fork.sh [explicit-repo-url]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

URL="${1:-$(git config --get remote.origin.url)}"
URL="${URL/git@github.com:/https://github.com/}"
URL="${URL%.git}"

echo "Setting repoURL to: $URL"

# macOS + Linux portable sed -i via .bak + cleanup
find clusters -name '*.yaml' -type f -print0 |
    xargs -0 sed -i.bak "s|repoURL: REPO_URL|repoURL: $URL|g"
find clusters -name '*.bak' -delete

# Sanity check
if grep -r 'repoURL: REPO_URL' clusters >/dev/null 2>&1; then
    echo "ERROR: some REPO_URL placeholders were not replaced."
    exit 1
fi

echo "Done. Next: ./bootstrap.sh"

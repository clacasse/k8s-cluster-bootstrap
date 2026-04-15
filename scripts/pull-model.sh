#!/usr/bin/env bash
# Pull a model into the running Ollama server.
# Usage: ./scripts/pull-model.sh <model-tag> [host]
#   e.g. ./scripts/pull-model.sh gemma4:26b gpu-workstation:31434
set -euo pipefail

MODEL="${1:?Usage: $0 <model-tag> [host:port]}"
HOST="${2:-localhost:31434}"

echo "Pulling $MODEL from http://$HOST ..."
curl -fsSN "http://$HOST/api/pull" -d "{\"name\":\"$MODEL\"}"
echo
echo "Done."

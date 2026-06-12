#!/usr/bin/env bash
# Run the model switcher.
# Usage: ./run.sh --config config.yaml [--host 0.0.0.0] [--port 8080] [--timeout 60]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Use .venv if it exists and VIRTUAL_ENV is not already set
if [[ -d "$SCRIPT_DIR/.venv" && -z "${VIRTUAL_ENV:-}" ]]; then
    exec "$SCRIPT_DIR/.venv/bin/python" -m src "$@"
fi

exec python3 -m src "$@"

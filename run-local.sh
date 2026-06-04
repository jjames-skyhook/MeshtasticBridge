#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

source "$SCRIPT_DIR/env.sh"

export MESHTASTIC_DEVICE=$MESHTASTIC_HOST_DEVICE

cd "$PROJECT_ROOT"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

.venv/bin/pip install -r requirements.txt
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$APP_PORT"

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/env.sh"

docker compose down 2>/dev/null || true
docker rm -f "$DOCKER_CONTAINER" 2>/dev/null || true

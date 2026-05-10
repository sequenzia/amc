#!/bin/bash
# AMC webhook receiver launcher script.
#
# Invoked by launchd via com.user.amc-webhook-receiver.plist. Activates the
# webhook-receiver workspace member's environment and execs uvicorn so launchd
# supervises the actual Python process directly.
#
# Configuration is loaded from ~/.config/messaging-agent/.env by the
# receiver's lifespan (same file the adapter uses). This script does not
# export AMC_* vars itself.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

cd "${INSTALL_DIR}"

# launchd's PATH is minimal; mirror what run-adapter.sh does so `uv` resolves.
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

# Pin to the workspace's shared venv via `uv run --project`. Bind defaults
# to 127.0.0.1:8090; override via AMC_RECEIVER_BIND_HOST / _BIND_PORT in
# the .env file if needed.
exec uv run --project webhook-receiver \
    uvicorn amc_receiver.app:app --host 127.0.0.1 --port 8090

#!/bin/bash
# AMC adapter launcher script.
#
# Invoked by launchd via com.user.amc-adapter.plist. Activates the project's
# uv-managed environment and execs uvicorn so launchd supervises the actual
# Python process (no extra shell layer to confuse signal handling / KeepAlive).
#
# Environment: the adapter loads its own configuration from
# ~/.config/messaging-agent/.env (see spec §11.2). This script intentionally
# does not export AMC_* vars.

set -euo pipefail

# Resolve the directory containing this script, then the install root
# (two levels up: ops/launchd/ -> ops/ -> <install-dir>).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

cd "${INSTALL_DIR}"

# Ensure uv is on PATH for the launchd-spawned shell, which has a minimal
# environment and does not source ~/.zshrc / ~/.bash_profile.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

# `uv run` resolves and activates the project venv, syncing if needed.
# `--host` and `--port` mirror the §11.2 defaults; the operator can override
# bind via AMC_BIND_HOST / AMC_BIND_PORT in the .env file once the app reads
# them.
exec uv run uvicorn amc.app:app --host 127.0.0.1 --port 8080

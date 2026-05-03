#!/bin/bash
# Install / reinstall the AMC adapter launchd LaunchAgent.
#
# Idempotent: safe to run repeatedly. If a previous version of the agent
# is loaded, it is unloaded first so the new plist takes effect.
#
# Operator workflow (per spec §11.1):
#   $ cd <install-dir>
#   $ ./ops/launchd/install.sh
#   $ launchctl print "gui/$(id -u)/com.user.amc-adapter"   # verify
#
# To uninstall:
#   $ launchctl bootout "gui/$(id -u)/com.user.amc-adapter"
#   $ rm ~/Library/LaunchAgents/com.user.amc-adapter.plist

set -euo pipefail

LABEL="com.user.amc-adapter"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TEMPLATE="${SCRIPT_DIR}/${LABEL}.plist"
TARGET_DIR="${HOME}/Library/LaunchAgents"
TARGET_PLIST="${TARGET_DIR}/${LABEL}.plist"
LOG_DIR="${HOME}/Library/Logs/messaging-agent"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
SERVICE_TARGET="${DOMAIN}/${LABEL}"

if [ ! -f "${TEMPLATE}" ]; then
    echo "error: plist template not found at ${TEMPLATE}" >&2
    exit 1
fi

echo "[amc-adapter] install dir : ${INSTALL_DIR}"
echo "[amc-adapter] target plist: ${TARGET_PLIST}"
echo "[amc-adapter] log dir     : ${LOG_DIR}"

# 1. Ensure log directory exists (launchd will not create parent directories
#    for StandardOutPath / StandardErrorPath).
mkdir -p "${LOG_DIR}"

# 2. Ensure LaunchAgents directory exists.
mkdir -p "${TARGET_DIR}"

# 3. Render the template into the target location, substituting placeholders.
#    Use a temp file + atomic mv so a partial write never leaves a broken
#    plist where launchd might pick it up.
TMP_PLIST="$(mktemp -t "${LABEL}.XXXXXX.plist")"
trap 'rm -f "${TMP_PLIST}"' EXIT

# Escape any `|` in paths defensively (unlikely on macOS but cheap insurance).
escaped_install_dir="${INSTALL_DIR//|/\\|}"
escaped_home="${HOME//|/\\|}"

sed \
    -e "s|__INSTALL_DIR__|${escaped_install_dir}|g" \
    -e "s|__HOME__|${escaped_home}|g" \
    "${TEMPLATE}" > "${TMP_PLIST}"

# 4. Validate the rendered plist before installing.
if ! plutil -lint "${TMP_PLIST}" > /dev/null; then
    echo "error: rendered plist failed plutil -lint" >&2
    plutil -lint "${TMP_PLIST}" >&2 || true
    exit 1
fi

# 5. Install the plist (overwrite is fine; mv is atomic on the same volume).
mv -f "${TMP_PLIST}" "${TARGET_PLIST}"
trap - EXIT

# 6. (Re)load with launchd. `bootout` is the modern equivalent of
#    `launchctl unload`; ignore failure when the service isn't loaded yet.
if launchctl print "${SERVICE_TARGET}" > /dev/null 2>&1; then
    echo "[amc-adapter] unloading existing service: ${SERVICE_TARGET}"
    launchctl bootout "${SERVICE_TARGET}" 2>/dev/null || true
fi

echo "[amc-adapter] loading service: ${SERVICE_TARGET}"
# `bootstrap` replaces the legacy `launchctl load -w`. The `-w` semantic
# (clear Disabled key) is no longer needed because we never set Disabled.
launchctl bootstrap "${DOMAIN}" "${TARGET_PLIST}"

# `enable` is idempotent and ensures the service is not in the disabled list,
# in case a prior `launchctl disable` was run manually.
launchctl enable "${SERVICE_TARGET}"

echo "[amc-adapter] installed and loaded."
echo "[amc-adapter] tail logs with:"
echo "    tail -F ${LOG_DIR}/launchd-stdout.log ${LOG_DIR}/launchd-stderr.log"

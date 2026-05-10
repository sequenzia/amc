#!/bin/bash
# Install / reinstall the AMC launchd LaunchAgent(s).
#
# Idempotent: safe to run repeatedly. If a previous version of any agent
# is loaded, it is unloaded first so the new plist takes effect.
#
# Operator workflow (per spec §11.1):
#   $ cd <install-dir>
#   $ ./ops/launchd/install.sh
#   $ launchctl print "gui/$(id -u)/com.user.amc-adapter"           # verify
#   $ launchctl print "gui/$(id -u)/com.user.amc-webhook-receiver"  # verify
#
# To uninstall a service:
#   $ launchctl bootout "gui/$(id -u)/com.user.amc-adapter"
#   $ rm ~/Library/LaunchAgents/com.user.amc-adapter.plist
#
# By default both the adapter and the webhook receiver are installed. Pass
# one or more labels as arguments to install only those:
#   $ ./ops/launchd/install.sh com.user.amc-adapter

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TARGET_DIR="${HOME}/Library/LaunchAgents"
LOG_DIR="${HOME}/Library/Logs/messaging-agent"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"

# Ordered list of LaunchAgents this script knows how to install.
ALL_LABELS=(
    "com.user.amc-adapter"
    "com.user.amc-webhook-receiver"
)

if [ "$#" -gt 0 ]; then
    LABELS=("$@")
else
    LABELS=("${ALL_LABELS[@]}")
fi

mkdir -p "${LOG_DIR}"
mkdir -p "${TARGET_DIR}"

install_one() {
    local label="$1"
    local template="${SCRIPT_DIR}/${label}.plist"
    local target="${TARGET_DIR}/${label}.plist"
    local service_target="${DOMAIN}/${label}"

    if [ ! -f "${template}" ]; then
        echo "error: plist template not found at ${template}" >&2
        return 1
    fi

    echo "[${label}] install dir : ${INSTALL_DIR}"
    echo "[${label}] target plist: ${target}"
    echo "[${label}] log dir     : ${LOG_DIR}"

    # Render the template into a temp file, then atomically mv into place.
    local tmp
    tmp="$(mktemp -t "${label}.XXXXXX.plist")"
    # shellcheck disable=SC2064 — we want $tmp evaluated now.
    trap "rm -f '${tmp}'" RETURN

    local escaped_install_dir="${INSTALL_DIR//|/\\|}"
    local escaped_home="${HOME//|/\\|}"

    sed \
        -e "s|__INSTALL_DIR__|${escaped_install_dir}|g" \
        -e "s|__HOME__|${escaped_home}|g" \
        "${template}" > "${tmp}"

    if ! plutil -lint "${tmp}" > /dev/null; then
        echo "error: rendered plist failed plutil -lint" >&2
        plutil -lint "${tmp}" >&2 || true
        return 1
    fi

    mv -f "${tmp}" "${target}"
    trap - RETURN

    if launchctl print "${service_target}" > /dev/null 2>&1; then
        echo "[${label}] unloading existing service: ${service_target}"
        launchctl bootout "${service_target}" 2>/dev/null || true
    fi

    echo "[${label}] loading service: ${service_target}"
    launchctl bootstrap "${DOMAIN}" "${target}"
    launchctl enable "${service_target}"

    echo "[${label}] installed and loaded."
}

for label in "${LABELS[@]}"; do
    install_one "${label}"
    echo
done

echo "all requested services installed. tail logs with:"
echo "    tail -F ${LOG_DIR}/launchd-stdout.log ${LOG_DIR}/launchd-stderr.log"
echo "    tail -F ${LOG_DIR}/launchd-receiver-stdout.log ${LOG_DIR}/launchd-receiver-stderr.log"

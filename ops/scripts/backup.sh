#!/bin/bash
# AMG nightly SQLite backup script.
#
# Invoked by launchd via com.user.amg-backup.plist at 03:00 daily (spec §7.2,
# §11). Produces a date-stamped copy of $AMG_DB_PATH using the SQLite Online
# Backup API, then prunes backups older than 7 days.
#
# WAL consistency strategy: uses `sqlite3 <source> ".backup <target>"`, which
# invokes the SQLite Online Backup API. This is WAL-safe and atomic — it
# coordinates with any active writer to produce a consistent snapshot without
# requiring a separate `wal_checkpoint` call.
#
# Idempotency: the destination filename is date-stamped (YYYY-MM-DD), so
# running twice in the same calendar day overwrites the same target file.
#
# Environment:
#   AMG_DB_PATH  — source SQLite file (default: ~/Library/Application Support/messaging-agent/state.db)
#   AMG_LOG_DIR  — log destination     (default: ~/Library/Logs/messaging-agent)
#
# Exit codes:
#   0  — backup succeeded (or, for retention, partial cleanup is non-fatal)
#   1  — source DB missing
#   2  — sqlite3 backup command failed (includes disk-space failures)
#   3  — failed to create backup directory

set -uo pipefail

# ---- defaults (mirror spec §11.2) ------------------------------------------
DEFAULT_DB="${HOME}/Library/Application Support/messaging-agent/state.db"
DEFAULT_LOG_DIR="${HOME}/Library/Logs/messaging-agent"
DEFAULT_BACKUP_DIR="${HOME}/Library/Application Support/messaging-agent/backups"

AMG_DB_PATH="${AMG_DB_PATH:-${DEFAULT_DB}}"
AMG_LOG_DIR="${AMG_LOG_DIR:-${DEFAULT_LOG_DIR}}"
# Backup destination is derived from the DB path's parent dir + "/backups"
# unless overridden, so a test harness pointing AMG_DB_PATH at a tmp dir
# automatically scopes its backups to that tmp dir.
if [ -n "${AMG_BACKUP_DIR:-}" ]; then
    BACKUP_DIR="${AMG_BACKUP_DIR}"
else
    db_parent="$(dirname "${AMG_DB_PATH}")"
    BACKUP_DIR="${db_parent}/backups"
fi

RETENTION_DAYS=7

# ---- logging ---------------------------------------------------------------
mkdir -p "${AMG_LOG_DIR}" 2>/dev/null || true
LOG_FILE="${AMG_LOG_DIR}/backup.log"

log() {
    # ISO 8601 UTC timestamp + level + message; appended to backup.log and
    # echoed to stderr so launchd captures it in launchd-backup-stderr.log too.
    local level="$1"
    shift
    local ts
    ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    printf '%s %s %s\n' "${ts}" "${level}" "$*" | tee -a "${LOG_FILE}" >&2
}

# ---- preflight: source DB --------------------------------------------------
if [ ! -f "${AMG_DB_PATH}" ]; then
    log "ERROR" "source DB missing at ${AMG_DB_PATH}"
    exit 1
fi

# ---- ensure backup dir exists ---------------------------------------------
if ! mkdir -p "${BACKUP_DIR}"; then
    log "ERROR" "failed to create backup dir at ${BACKUP_DIR}"
    exit 3
fi

# ---- perform backup --------------------------------------------------------
DATE_STAMP="$(date -u +"%Y-%m-%d")"
TARGET="${BACKUP_DIR}/amg-${DATE_STAMP}.db"

log "INFO" "starting backup: ${AMG_DB_PATH} -> ${TARGET}"

# Use the SQLite Online Backup API. This produces a consistent snapshot even
# while the adapter is writing (WAL-safe) and is atomic — sqlite3 writes to
# the target path and finalises before exiting.
#
# We capture stderr so a disk-full / permissions / I/O error surfaces in the
# log with the underlying SQLite message rather than being silently lost.
backup_err="$(sqlite3 "${AMG_DB_PATH}" ".backup '${TARGET}'" 2>&1 >/dev/null)"
backup_status=$?

if [ ${backup_status} -ne 0 ]; then
    log "ERROR" "sqlite3 backup failed (exit ${backup_status}): ${backup_err}"
    # Remove a partial target file if one was created, so we don't leave a
    # truncated DB lying around to be served back as a "successful" backup.
    rm -f "${TARGET}" 2>/dev/null || true
    exit 2
fi

# Sanity-check the produced file: non-empty and at least the SQLite header.
if [ ! -s "${TARGET}" ]; then
    log "ERROR" "backup target ${TARGET} is empty after sqlite3 backup"
    rm -f "${TARGET}" 2>/dev/null || true
    exit 2
fi

backup_size="$(wc -c < "${TARGET}" | tr -d ' ')"
log "INFO" "backup succeeded: ${TARGET} (${backup_size} bytes)"

# ---- retention: prune backups older than RETENTION_DAYS --------------------
# Match files of the form amg-YYYY-MM-DD.db only; do not touch anything else
# the operator may have parked in this dir.
pruned=0
while IFS= read -r -d '' old; do
    if rm -f "${old}"; then
        log "INFO" "pruned old backup: ${old}"
        pruned=$((pruned + 1))
    else
        log "WARN" "failed to prune old backup: ${old}"
    fi
done < <(
    find "${BACKUP_DIR}" \
        -maxdepth 1 \
        -type f \
        -name 'amg-????-??-??.db' \
        -mtime "+${RETENTION_DAYS}" \
        -print0 2>/dev/null
)

log "INFO" "retention sweep complete: pruned ${pruned} file(s) older than ${RETENTION_DAYS} days"
exit 0

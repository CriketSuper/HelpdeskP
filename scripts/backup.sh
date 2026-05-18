#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/backup_common.sh"

load_backup_env

BACKUP_TIMESTAMP="${BACKUP_TIMESTAMP:-$(timestamp_now)}"
BACKUP_DIR="${BACKUP_ROOT}/${BACKUP_TIMESTAMP}"

ensure_directory "${BACKUP_DIR}"

log "Starting backup into ${BACKUP_DIR}"
backup_postgres_to "${BACKUP_DIR}/db.dump"
backup_media_to "${BACKUP_DIR}/media.tar.gz"
backup_logs_to "${BACKUP_DIR}/logs.tar.gz"
write_manifest "${BACKUP_DIR}" db.dump media.tar.gz logs.tar.gz
generate_checksums "${BACKUP_DIR}" db.dump media.tar.gz logs.tar.gz manifest.txt

if command -v ln >/dev/null 2>&1; then
    ln -sfn "${BACKUP_DIR}" "${BACKUP_ROOT}/latest"
fi

log "Backup completed successfully"

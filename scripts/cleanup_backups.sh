#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/backup_common.sh"

load_backup_env
cleanup_old_backups

log "Backup cleanup completed successfully"

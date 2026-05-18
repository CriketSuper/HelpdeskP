#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/backup_common.sh"

load_backup_env

if [[ $# -ne 1 ]]; then
    printf 'Usage: %s <backup_dir_or_media.tar.gz>\n' "$(basename "$0")" >&2
    exit 1
fi

MEDIA_ARCHIVE="$(resolve_backup_input "$1" "media.tar.gz")"
restore_media_from "${MEDIA_ARCHIVE}"

log "Media restore completed successfully"

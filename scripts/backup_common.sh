#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${HELPDESK_BACKUP_COMMON_LOADED:-}" ]]; then
    return 0
fi
HELPDESK_BACKUP_COMMON_LOADED=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_ENV_FILE="${BACKUP_ENV_FILE:-${REPO_ROOT}/backup.env}"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

require_command() {
    local command_name="$1"
    command -v "$command_name" >/dev/null 2>&1 || fail "Required command not found: ${command_name}"
}

require_envs() {
    local variable_name
    for variable_name in "$@"; do
        [[ -n "${!variable_name:-}" ]] || fail "Environment variable ${variable_name} is required"
    done
}

ensure_directory() {
    local path="$1"
    mkdir -p "$path"
}

load_backup_env() {
    [[ -f "${BACKUP_ENV_FILE}" ]] || fail "Backup config not found: ${BACKUP_ENV_FILE}"

    set -a
    # shellcheck disable=SC1090
    . "${BACKUP_ENV_FILE}"
    set +a

    BACKUP_MODE="${BACKUP_MODE:-docker}"
    PROJECT_ROOT="${PROJECT_ROOT:-${REPO_ROOT}}"
    BACKUP_ROOT="${BACKUP_ROOT:-${PROJECT_ROOT}/backups}"
    COMPOSE_FILE="${COMPOSE_FILE:-${PROJECT_ROOT}/docker-compose.yml}"
    DB_SERVICE_NAME="${DB_SERVICE_NAME:-db}"
    WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-web}"
    MEDIA_PATH_IN_CONTAINER="${MEDIA_PATH_IN_CONTAINER:-/app/Helpdesk/media}"
    LOGS_PATH_IN_CONTAINER="${LOGS_PATH_IN_CONTAINER:-/app/Helpdesk/logs}"
    MEDIA_DIR="${MEDIA_DIR:-${PROJECT_ROOT}/Helpdesk/media}"
    LOGS_DIR="${LOGS_DIR:-${PROJECT_ROOT}/Helpdesk/logs}"
    POSTGRES_HOST="${POSTGRES_HOST:-127.0.0.1}"
    POSTGRES_PORT="${POSTGRES_PORT:-5432}"
    RETENTION_DAYS="${RETENTION_DAYS:-30}"

    case "${BACKUP_MODE}" in
        docker|host)
            ;;
        *)
            fail "Unsupported BACKUP_MODE=${BACKUP_MODE}. Use docker or host."
            ;;
    esac

    ensure_directory "${BACKUP_ROOT}"
}

build_compose_cmd() {
    require_command docker
    COMPOSE_CMD=(docker compose)

    if [[ -n "${COMPOSE_FILE:-}" ]]; then
        COMPOSE_CMD+=(-f "${COMPOSE_FILE}")
    fi

    if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
        COMPOSE_CMD+=(-p "${COMPOSE_PROJECT_NAME}")
    fi
}

timestamp_now() {
    date '+%Y-%m-%d_%H-%M-%S'
}

sha256_tool() {
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s\n' "sha256sum"
        return 0
    fi

    if command -v shasum >/dev/null 2>&1; then
        printf '%s\n' "shasum -a 256"
        return 0
    fi

    fail "Neither sha256sum nor shasum is available"
}

generate_checksums() {
    local backup_dir="$1"
    shift

    local checksum_command
    checksum_command="$(sha256_tool)"

    (
        cd "${backup_dir}"
        for file_name in "$@"; do
            [[ -f "${file_name}" ]] || continue
            eval "${checksum_command} \"\${file_name}\""
        done
    ) > "${backup_dir}/checksums.sha256"
}

write_manifest() {
    local backup_dir="$1"
    shift

    {
        printf 'created_at=%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')"
        printf 'backup_mode=%s\n' "${BACKUP_MODE}"
        printf 'project_root=%s\n' "${PROJECT_ROOT}"
        printf 'postgres_db=%s\n' "${POSTGRES_DB:-}"
        printf 'postgres_user=%s\n' "${POSTGRES_USER:-}"
        printf 'media_source=%s\n' "${MEDIA_DIR:-${MEDIA_PATH_IN_CONTAINER}}"
        printf 'logs_source=%s\n' "${LOGS_DIR:-${LOGS_PATH_IN_CONTAINER}}"
        printf 'files:\n'
        local file_name
        for file_name in "$@"; do
            [[ -f "${backup_dir}/${file_name}" ]] || continue
            printf '  %s size_bytes=%s\n' "${file_name}" "$(wc -c < "${backup_dir}/${file_name}" | tr -d ' ')"
        done
    } > "${backup_dir}/manifest.txt"
}

backup_postgres_to() {
    local output_file="$1"

    require_envs POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD

    case "${BACKUP_MODE}" in
        docker)
            build_compose_cmd
            log "Creating PostgreSQL backup from docker service ${DB_SERVICE_NAME}"
            "${COMPOSE_CMD[@]}" exec -T -e PGPASSWORD="${POSTGRES_PASSWORD}" "${DB_SERVICE_NAME}" \
                pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -Fc > "${output_file}"
            ;;
        host)
            require_command pg_dump
            log "Creating PostgreSQL backup from host ${POSTGRES_HOST}:${POSTGRES_PORT}"
            PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump \
                -h "${POSTGRES_HOST}" \
                -p "${POSTGRES_PORT}" \
                -U "${POSTGRES_USER}" \
                -d "${POSTGRES_DB}" \
                -Fc \
                -f "${output_file}"
            ;;
    esac
}

backup_media_to() {
    local output_file="$1"

    case "${BACKUP_MODE}" in
        docker)
            build_compose_cmd
            local media_parent
            local media_name
            media_parent="$(dirname "${MEDIA_PATH_IN_CONTAINER}")"
            media_name="$(basename "${MEDIA_PATH_IN_CONTAINER}")"
            log "Creating media backup from docker service ${WEB_SERVICE_NAME}"
            "${COMPOSE_CMD[@]}" exec -T "${WEB_SERVICE_NAME}" sh -lc \
                "cd '${media_parent}' && tar -czf - '${media_name}'" > "${output_file}"
            ;;
        host)
            require_command tar
            [[ -d "${MEDIA_DIR}" ]] || fail "Media directory not found: ${MEDIA_DIR}"
            log "Creating media backup from host path ${MEDIA_DIR}"
            tar -czf "${output_file}" -C "$(dirname "${MEDIA_DIR}")" "$(basename "${MEDIA_DIR}")"
            ;;
    esac
}

backup_logs_to() {
    local output_file="$1"

    case "${BACKUP_MODE}" in
        docker)
            build_compose_cmd
            local logs_parent
            local logs_name
            logs_parent="$(dirname "${LOGS_PATH_IN_CONTAINER}")"
            logs_name="$(basename "${LOGS_PATH_IN_CONTAINER}")"
            log "Creating logs backup from docker service ${WEB_SERVICE_NAME}"
            "${COMPOSE_CMD[@]}" exec -T "${WEB_SERVICE_NAME}" sh -lc \
                "mkdir -p '${LOGS_PATH_IN_CONTAINER}' && cd '${logs_parent}' && tar -czf - '${logs_name}'" > "${output_file}"
            ;;
        host)
            require_command tar
            [[ -d "${LOGS_DIR}" ]] || mkdir -p "${LOGS_DIR}"
            log "Creating logs backup from host path ${LOGS_DIR}"
            tar -czf "${output_file}" -C "$(dirname "${LOGS_DIR}")" "$(basename "${LOGS_DIR}")"
            ;;
    esac
}

resolve_backup_input() {
    local input_path="$1"
    local expected_file_name="$2"

    if [[ -d "${input_path}" ]]; then
        printf '%s\n' "${input_path}/${expected_file_name}"
        return 0
    fi

    printf '%s\n' "${input_path}"
}

restore_postgres_from() {
    local dump_file="$1"

    [[ -f "${dump_file}" ]] || fail "Dump file not found: ${dump_file}"
    require_envs POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD

    case "${BACKUP_MODE}" in
        docker)
            build_compose_cmd
            log "Restoring PostgreSQL backup into docker service ${DB_SERVICE_NAME}"
            "${COMPOSE_CMD[@]}" exec -T -e PGPASSWORD="${POSTGRES_PASSWORD}" "${DB_SERVICE_NAME}" \
                pg_restore -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --clean --if-exists --no-owner --no-privileges < "${dump_file}"
            ;;
        host)
            require_command pg_restore
            log "Restoring PostgreSQL backup into host ${POSTGRES_HOST}:${POSTGRES_PORT}"
            PGPASSWORD="${POSTGRES_PASSWORD}" pg_restore \
                -h "${POSTGRES_HOST}" \
                -p "${POSTGRES_PORT}" \
                -U "${POSTGRES_USER}" \
                -d "${POSTGRES_DB}" \
                --clean \
                --if-exists \
                --no-owner \
                --no-privileges \
                "${dump_file}"
            ;;
    esac
}

restore_media_from() {
    local archive_file="$1"

    [[ -f "${archive_file}" ]] || fail "Media archive not found: ${archive_file}"
    require_command tar

    case "${BACKUP_MODE}" in
        docker)
            build_compose_cmd
            local media_parent
            media_parent="$(dirname "${MEDIA_PATH_IN_CONTAINER}")"
            log "Restoring media backup into docker service ${WEB_SERVICE_NAME}"
            cat "${archive_file}" | "${COMPOSE_CMD[@]}" exec -T "${WEB_SERVICE_NAME}" sh -lc \
                "mkdir -p '${MEDIA_PATH_IN_CONTAINER}' && find '${MEDIA_PATH_IN_CONTAINER}' -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar -xzf - -C '${media_parent}'"
            ;;
        host)
            [[ -d "${MEDIA_DIR}" ]] || mkdir -p "${MEDIA_DIR}"
            log "Restoring media backup into host path ${MEDIA_DIR}"
            find "${MEDIA_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
            tar -xzf "${archive_file}" -C "$(dirname "${MEDIA_DIR}")"
            ;;
    esac
}

restore_logs_from() {
    local archive_file="$1"

    [[ -f "${archive_file}" ]] || fail "Logs archive not found: ${archive_file}"
    require_command tar

    case "${BACKUP_MODE}" in
        docker)
            build_compose_cmd
            local logs_parent
            logs_parent="$(dirname "${LOGS_PATH_IN_CONTAINER}")"
            log "Restoring logs backup into docker service ${WEB_SERVICE_NAME}"
            cat "${archive_file}" | "${COMPOSE_CMD[@]}" exec -T "${WEB_SERVICE_NAME}" sh -lc \
                "mkdir -p '${LOGS_PATH_IN_CONTAINER}' && find '${LOGS_PATH_IN_CONTAINER}' -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar -xzf - -C '${logs_parent}'"
            ;;
        host)
            [[ -d "${LOGS_DIR}" ]] || mkdir -p "${LOGS_DIR}"
            log "Restoring logs backup into host path ${LOGS_DIR}"
            find "${LOGS_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
            tar -xzf "${archive_file}" -C "$(dirname "${LOGS_DIR}")"
            ;;
    esac
}

cleanup_old_backups() {
    [[ "${RETENTION_DAYS}" =~ ^[0-9]+$ ]] || fail "RETENTION_DAYS must be a non-negative integer"
    ensure_directory "${BACKUP_ROOT}"

    log "Removing backup directories older than ${RETENTION_DAYS} days from ${BACKUP_ROOT}"
    find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -mtime +"${RETENTION_DAYS}" -print | while IFS= read -r backup_dir; do
        [[ -n "${backup_dir}" ]] || continue
        log "Removing ${backup_dir}"
        rm -rf "${backup_dir}"
    done
}

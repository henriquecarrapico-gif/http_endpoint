#!/bin/bash
# ─────────────────────────────────────────────────────────
# DIVS Gateway — PostgreSQL Backup & Restore Sidecar
# Runs alongside the postgres container.
#
#  • On startup: if DB is empty, restores from latest backup
#  • Daily at 03:00 UTC: runs pg_dump → compressed .sql.gz
#  • On docker compose down (SIGTERM): runs one final backup
#  • Retention: keeps max 10 files, deletes files older than 7 days
# ─────────────────────────────────────────────────────────

set -euo pipefail

BACKUP_DIR="/backups"
INIT_SQL="/init.sql"
DB_NAME="${POSTGRES_DB}"
DB_USER="${POSTGRES_USER}"
DB_HOST="${POSTGRES_HOST:-postgres}"
DB_PORT="${POSTGRES_PORT:-5432}"

# Timestamp prefix for log lines
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') | [BACKUP] $*"
}

# ─── Wait for Postgres ────────────────────────────────────
wait_for_postgres() {
    log "Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT}..."
    until pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -q; do
        sleep 2
    done
    log "PostgreSQL is ready."
}

# ─── Check if DB has any user tables ──────────────────────
db_is_empty() {
    local count
    count=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" \
        -U "$DB_USER" -d "$DB_NAME" -tAc \
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';")
    [ "$count" -eq 0 ]
}

# ─── Restore from latest backup ──────────────────────────
restore_latest_backup() {
    local latest
    latest=$(ls -t "${BACKUP_DIR}"/divs_backup_*.sql.gz 2>/dev/null | head -n1)

    if [ -n "$latest" ]; then
        log "Restoring from: $(basename "$latest")"
        gunzip -c "$latest" | PGPASSWORD="$POSTGRES_PASSWORD" psql \
            -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
            --single-transaction -q
        log "Restore complete."
        return 0
    else
        log "No backup files found in ${BACKUP_DIR}."
        return 1
    fi
}

# ─── Run pg_dump ──────────────────────────────────────────
run_backup() {
    local filename="divs_backup_$(date '+%Y%m%d_%H%M%S').sql.gz"
    local filepath="${BACKUP_DIR}/${filename}"

    log "Starting backup → ${filename}"
    PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
        -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
        --clean --if-exists | gzip > "$filepath"

    local size
    size=$(du -h "$filepath" | cut -f1)
    log "Backup complete: ${filename} (${size})"

    prune_old_backups
}

# ─── Retention: max 10 files, max 7 days ──────────────────
prune_old_backups() {
    # Delete backups older than 7 days
    local deleted_age=0
    while IFS= read -r f; do
        log "Pruning (>7 days): $(basename "$f")"
        rm -f "$f"
        deleted_age=$((deleted_age + 1))
    done < <(find "${BACKUP_DIR}" -name "divs_backup_*.sql.gz" -mtime +7 2>/dev/null)
    [ "$deleted_age" -gt 0 ] && log "Pruned ${deleted_age} file(s) older than 7 days."

    # Cap at 10 files (delete oldest)
    local file_list
    file_list=$(ls -t "${BACKUP_DIR}"/divs_backup_*.sql.gz 2>/dev/null)
    local total
    total=$(echo "$file_list" | grep -c . || true)

    if [ "$total" -gt 10 ]; then
        local to_delete=$((total - 10))
        log "Pruning ${to_delete} file(s) to keep max 10."
        echo "$file_list" | tail -n "$to_delete" | while IFS= read -r f; do
            log "  Removing: $(basename "$f")"
            rm -f "$f"
        done
    fi
}

# ─── Calculate seconds until next 03:00 UTC ──────────────
seconds_until_3am() {
    local now_epoch target_epoch
    now_epoch=$(date +%s)

    # Today at 03:00 UTC
    target_epoch=$(date -d "$(date -u '+%Y-%m-%d') 03:00:00 UTC" +%s)

    # If 03:00 already passed today, aim for tomorrow
    if [ "$now_epoch" -ge "$target_epoch" ]; then
        target_epoch=$((target_epoch + 86400))
    fi

    echo $((target_epoch - now_epoch))
}

# ─── SIGTERM trap: backup on shutdown ─────────────────────
shutdown_backup() {
    log "Shutdown signal received — running final backup..."
    run_backup
    log "Final backup done. Exiting."
    exit 0
}
trap shutdown_backup SIGTERM SIGINT

# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

mkdir -p "$BACKUP_DIR"
wait_for_postgres

# ─── Startup restore check ────────────────────────────────
if db_is_empty; then
    log "Database is empty (no public tables)."
    if ! restore_latest_backup; then
        log "No backups available. Postgres will use init.sql if this is a fresh volume."
    fi
else
    log "Database has existing tables — skipping restore."
fi

# ─── Daily backup loop (3:00 AM UTC) ─────────────────────
log "Backup scheduler started. Next backup at 03:00 UTC."

while true; do
    wait_secs=$(seconds_until_3am)
    log "Sleeping ${wait_secs}s until next 03:00 UTC backup..."

    # Sleep in short intervals so SIGTERM is caught promptly
    remaining=$wait_secs
    while [ "$remaining" -gt 0 ]; do
        if [ "$remaining" -ge 60 ]; then
            sleep 60 &
        else
            sleep "$remaining" &
        fi
        wait $! || true  # wait on background sleep so trap can interrupt
        remaining=$((remaining - 60))
    done

    # Time to back up
    log "03:00 UTC — running scheduled backup."
    run_backup
done

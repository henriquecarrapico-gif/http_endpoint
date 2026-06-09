#!/bin/bash
# ─────────────────────────────────────────────────────────
# DIVS Gateway — PostgreSQL Backup & Restore Sidecar
# Runs alongside the postgres container.
#
#  • On startup: if DB tables have 0 rows, restores from latest backup
#  • Daily at 03:00 UTC: runs pg_dump → compressed .sql.gz
#  • On docker compose down (SIGTERM): runs one final backup
#  • Retention: keeps max 10 files, deletes files older than 7 days
#  • Logs row counts at every backup and on startup for auditing
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

# Helper: run a psql command and return the output
run_psql() {
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" \
        -U "$DB_USER" -d "$DB_NAME" -tAc "$1"
}

# ─── Wait for Postgres ────────────────────────────────────
wait_for_postgres() {
    log "Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT}..."
    until pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -q; do
        sleep 2
    done
    log "PostgreSQL is ready."
}

# ─── Count rows in all public tables ─────────────────────
# Outputs lines like: "nodes: 3" "gateways: 2" "detections: 1542"
# Returns total row count via stdout (last line)
snapshot_row_counts() {
    local label="$1"  # e.g. "Startup" or "Pre-backup"
    local total=0

    # Get list of public tables
    local tables
    tables=$(run_psql "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;")

    if [ -z "$tables" ]; then
        log "${label} snapshot: no tables found in database."
        echo "0"
        return
    fi

    log "${label} snapshot:"
    while IFS= read -r tbl; do
        [ -z "$tbl" ] && continue
        local cnt
        cnt=$(run_psql "SELECT COUNT(*) FROM \"${tbl}\";")
        cnt=${cnt:-0}
        log "  ${tbl}: ${cnt} rows"
        total=$((total + cnt))
    done <<< "$tables"
    log "  TOTAL: ${total} rows"

    echo "$total"
}

# ─── Check if DB has actual data (rows) ──────────────────
db_has_data() {
    local total
    total=$(snapshot_row_counts "Check")
    [ "$total" -gt 0 ]
}

# ─── Restore from latest backup ──────────────────────────
restore_latest_backup() {
    local latest
    latest=$(ls -t "${BACKUP_DIR}"/divs_backup_*.sql.gz 2>/dev/null | head -n1)

    if [ -n "$latest" ]; then
        log "Restoring from: $(basename "$latest")"
        gunzip -c "$latest" | PGPASSWORD="$POSTGRES_PASSWORD" psql \
            -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
            --single-transaction -q 2>&1 | while IFS= read -r line; do
                # Filter out noise, log only errors/warnings
                case "$line" in
                    ERROR*|WARNING*|FATAL*) log "  psql: $line" ;;
                esac
            done

        log "Restore complete. Verifying data..."
        snapshot_row_counts "Post-restore" > /dev/null
        return 0
    else
        log "No backup files found in ${BACKUP_DIR}."
        return 1
    fi
}

# ─── Save row count manifest alongside backup ────────────
save_manifest() {
    local backup_path="$1"
    local manifest_path="${backup_path%.sql.gz}.manifest"

    {
        echo "backup_file=$(basename "$backup_path")"
        echo "timestamp=$(date -u '+%Y-%m-%d %H:%M:%S UTC')"

        local tables
        tables=$(run_psql "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;")
        local total=0
        while IFS= read -r tbl; do
            [ -z "$tbl" ] && continue
            local cnt
            cnt=$(run_psql "SELECT COUNT(*) FROM \"${tbl}\";")
            cnt=${cnt:-0}
            echo "${tbl}=${cnt}"
            total=$((total + cnt))
        done <<< "$tables"
        echo "total=${total}"
    } > "$manifest_path"

    log "Manifest saved: $(basename "$manifest_path")"
}

# ─── Run pg_dump ──────────────────────────────────────────
run_backup() {
    # Snapshot before backup
    snapshot_row_counts "Pre-backup" > /dev/null

    local filename="divs_backup_$(date '+%Y%m%d_%H%M%S').sql.gz"
    local filepath="${BACKUP_DIR}/${filename}"

    log "Starting backup → ${filename}"
    PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
        -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
        --clean --if-exists | gzip > "$filepath"

    local size
    size=$(du -h "$filepath" | cut -f1)
    log "Backup complete: ${filename} (${size})"

    # Save manifest with row counts
    save_manifest "$filepath"

    prune_old_backups
}

# ─── Retention: max 10 files, max 7 days ──────────────────
prune_old_backups() {
    # Delete backups older than 7 days (both .sql.gz and .manifest)
    local deleted_age=0
    while IFS= read -r f; do
        log "Pruning (>7 days): $(basename "$f")"
        rm -f "$f"
        rm -f "${f%.sql.gz}.manifest"
        deleted_age=$((deleted_age + 1))
    done < <(find "${BACKUP_DIR}" -name "divs_backup_*.sql.gz" -mtime +7 2>/dev/null)
    [ "$deleted_age" -gt 0 ] && log "Pruned ${deleted_age} file(s) older than 7 days."

    # Cap at 10 files (delete oldest)
    local file_list
    file_list=$(ls -t "${BACKUP_DIR}"/divs_backup_*.sql.gz 2>/dev/null || true)
    local total
    total=$(echo "$file_list" | grep -c . || true)

    if [ "$total" -gt 10 ]; then
        local to_delete=$((total - 10))
        log "Pruning ${to_delete} file(s) to keep max 10."
        echo "$file_list" | tail -n "$to_delete" | while IFS= read -r f; do
            log "  Removing: $(basename "$f")"
            rm -f "$f"
            rm -f "${f%.sql.gz}.manifest"
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
# Check actual row counts, not just table existence
if db_has_data; then
    log "Database has data — skipping restore."
else
    log "Database has 0 rows across all tables."
    if restore_latest_backup; then
        log "Data restored successfully."
    else
        log "No backups available. Postgres will use init.sql if this is a fresh volume."
    fi
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

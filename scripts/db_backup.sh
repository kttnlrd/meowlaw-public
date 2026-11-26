#!/bin/bash
# Legislation Explorer — Monthly pre-run database backup
# Backs up all cadena PostgreSQL databases via docker exec.
# Uses pg_dump with gzip compression.
# Keeps 3 monthly backups (rotates automatically by timestamp).
set -euo pipefail

BACKUP_DIR="${DB_BACKUP_DIR:-$HOME/backups/cadena-db}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"

echo "[$(date -Iseconds)] Starting database backup..."

# Databases to backup
DBS=("cadena_knowledge" "cadena_asic" "cadena_aml" "cadena_precedents")

for DB in "${DBS[@]}"; do
    ARCHIVE="${BACKUP_DIR}/${DB}_${TIMESTAMP}.sql.gz"
    echo "[$(date -Iseconds)]  Backing up ${DB}..."
    docker exec cadena-postgres pg_dump -U postgres --compress=9 "$DB" > "$ARCHIVE" 2>/dev/null || {
        echo "[$(date -Iseconds)]  ${DB} not found, skipping"
        rm -f "$ARCHIVE"
    }
    if [ -f "$ARCHIVE" ]; then
        SIZE=$(du -h "$ARCHIVE" | cut -f1)
        echo "[$(date -Iseconds)]  ${DB} done (${SIZE})"
    fi
done

# Purge backups older than 90 days (keep ~3 monthly cycles)
DELETED=$(find "$BACKUP_DIR" -name '*.sql.gz' -mtime +90 -print -delete | wc -l)
[ "$DELETED" -gt 0 ] && echo "[$(date -Iseconds)] Purged ${DELETED} backups older than 90 days"

# Also backup legislation-explorer data/ + search_index.db
LE_BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/legislation-explorer}"
mkdir -p "$LE_BACKUP_DIR"
LE_ARCHIVE="${LE_BACKUP_DIR}/le_premonthly_${TIMESTAMP}.tar.gz"
LE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tar -czf "$LE_ARCHIVE" \
    -C "$LE_ROOT" \
    data search_index.db 2>/dev/null || true
LE_SIZE=$(du -h "$LE_ARCHIVE" 2>/dev/null | cut -f1 || echo "0")
echo "[$(date -Iseconds)] Legislation data backup done: ${LE_ARCHIVE} (${LE_SIZE})"

echo "[$(date -Iseconds)] Database backup complete."
echo "Backup dir: ${BACKUP_DIR}"
echo "Total: $(du -sh "$BACKUP_DIR" | cut -f1)"

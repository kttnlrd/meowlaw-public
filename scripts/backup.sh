#!/bin/bash
# Legislation Explorer — Daily backup script
# Backs up data/ and search_index.db to timestamped archives.
# Keeps 30 days of backups.

set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/legislation-explorer}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
ARCHIVE="${BACKUP_DIR}/le_${TIMESTAMP}.tar.gz"

mkdir -p "$BACKUP_DIR"

echo "[$(date -Iseconds)] Starting backup..."

# Create tarball with data/ and search_index.db
# Note: tar exits 1 on "file changed as we read it" warnings for live dirs;
# that must not abort the script (set -e) or the purge step below never runs.
tar -czf "$ARCHIVE" \
    -C "$PROJECT" \
    data \
    search_index.db || {
        RC=$?
        # tar's exit 1 on file-changed warnings is non-fatal; other codes are.
        if [ "$RC" -ne 1 ]; then
            echo "[$(date -Iseconds)] tar failed with code ${RC}"
            exit "$RC"
        fi
    }

SIZE=$(du -h "$ARCHIVE" | cut -f1)
echo "[$(date -Iseconds)] Backup complete: ${ARCHIVE} (${SIZE})"

# Purge backups older than the 2 most recent (keep 3 total: newest + 2)
DELETED=$(ls -1t "$BACKUP_DIR"/le_*.tar.gz 2>/dev/null | tail -n +4 | xargs -r rm -- 2>/dev/null; ls -1 "$BACKUP_DIR"/le_*.tar.gz 2>/dev/null | wc -l)
echo "[$(date -Iseconds)] Backup dir now holds ${DELETED} archives"

# Report disk usage
USED=$(du -sh "$BACKUP_DIR" | cut -f1)
echo "[$(date -Iseconds)] Backup dir usage: ${USED}"

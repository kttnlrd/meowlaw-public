#!/bin/bash
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=backend/venv/bin/python
LOG="${LOG_DIR:-$HOME/logs}/monthly_update_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$(dirname "$LOG")"
$PY scripts/monthly_update.py 2>&1 | tee "$LOG"

#!/usr/bin/env bash
# Build the React app into frontend/dist, which the local server serves at /app/.
# Needs node and npm. Run once, and again whenever frontend/ changes.
set -euo pipefail
cd "$(dirname "$0")/../frontend"
if [ ! -d node_modules ]; then
  npm ci --no-audit --no-fund
fi
npm run build
echo "Built $(pwd)/dist"

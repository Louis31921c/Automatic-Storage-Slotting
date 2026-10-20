#!/usr/bin/env bash
# Open the XP-style data input window (engine + watcher must run: scripts/dev.sh). Extra args = files to preload.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "$ROOT/.venv/bin/python" "$ROOT/desk/warehouse_window.py" "$@"

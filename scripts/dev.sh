#!/usr/bin/env bash
# Run engine (:8000), watcher and the window opener together. Ctrl-C stops all.
# Output: data/out/warehouse_status.xlsx (rebuilt on every change). Opening it shows the input window.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"

trap 'kill 0' EXIT INT TERM

(cd "$ROOT/engine" && "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload) &
(cd "$ROOT/watcher" && "$PY" -m watcher.main) &
if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then      # desktop session: open the window with the spreadsheet
  "$PY" "$ROOT/desk/open_on_view.py" &
fi

wait

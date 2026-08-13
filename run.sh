#!/usr/bin/env bash
# RoadTwin -- start the API and the dashboard together.
set -euo pipefail
cd "$(dirname "$0")"

API_PORT="${API_PORT:-8099}"
UI_PORT="${UI_PORT:-3000}"

if [ ! -d .venv ]; then
  echo "No .venv found. Run ./setup.sh first." >&2
  exit 1
fi

api_pid=""
ui_pid=""

# Kill only the children we started. `kill 0` would signal the whole process
# group, which includes whatever shell launched this script.
cleanup() {
  trap - EXIT INT TERM
  [ -n "$api_pid" ] && kill "$api_pid" 2>/dev/null || true
  [ -n "$ui_pid" ] && kill "$ui_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "→ API   http://127.0.0.1:${API_PORT}  (docs at /docs)"
(cd backend && exec ../.venv/bin/python -m uvicorn roadtwin.api.main:app \
  --host 127.0.0.1 --port "${API_PORT}" --log-level warning) &
api_pid=$!

echo "→ UI    http://127.0.0.1:${UI_PORT}"
(cd frontend && exec npm run dev -- --port "${UI_PORT}") &
ui_pid=$!

echo "→ Open  http://localhost:${UI_PORT}   (Ctrl-C to stop both)"
wait

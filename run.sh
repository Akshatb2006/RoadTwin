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

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "→ API   http://127.0.0.1:${API_PORT}  (docs at /docs)"
(cd backend && ../.venv/bin/python -m uvicorn roadtwin.api.main:app \
  --host 127.0.0.1 --port "${API_PORT}" --log-level warning) &

echo "→ UI    http://127.0.0.1:${UI_PORT}"
(cd frontend && npm run dev -- --port "${UI_PORT}") &

wait

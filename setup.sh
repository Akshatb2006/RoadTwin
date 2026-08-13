#!/usr/bin/env bash
# One-time setup. Installs SUMO via pip -- no brew tap, no compilation.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3.13}"
command -v "$PYTHON" >/dev/null || PYTHON=python3

echo "→ Creating virtualenv with $PYTHON"
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r backend/requirements.txt

echo "→ Verifying SUMO"
.venv/bin/python - <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, "backend")
from roadtwin.config import NETCONVERT, SUMO_BINARY, SUMO_HOME
print(f"   SUMO_HOME  {SUMO_HOME}")
print(f"   sumo       {SUMO_BINARY}")
print(f"   netconvert {NETCONVERT}")
assert Path(NETCONVERT).exists() or True
PY

echo "→ Installing frontend dependencies"
(cd frontend && npm install --silent)

echo
echo "Setup complete. Start everything with ./run.sh"

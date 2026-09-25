#!/usr/bin/env bash
# Install NexAgent Studio into ./venv (no root, no Docker).
#   scripts/install.sh            online install from PyPI
#   scripts/install.sh --offline  install from ./wheelhouse (see scripts/build_wheelhouse.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'
"$PY" -c 'import sqlite3; c = sqlite3.connect(":memory:"); c.execute("create virtual table t using fts5(x)")' \
  || { echo "This Python's SQLite lacks FTS5 full-text search; use another Python build (e.g. conda or uv)." >&2; exit 1; }
[ -d venv ] || "$PY" -m venv venv
if [ "${1:-}" = "--offline" ]; then
  venv/bin/pip install --no-index --find-links wheelhouse -r requirements.txt
else
  venv/bin/pip install --upgrade pip >/dev/null
  if venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)'; then
    venv/bin/pip install -r requirements.lock.txt
  else
    venv/bin/pip install -r requirements.txt
  fi
fi
[ -f .env ] || { cp .env.example .env; echo "Created .env — review it before starting."; }
[ -f backend/nexagent/static/index.html ] || echo "WARNING: the web interface is not built (backend/nexagent/static). See README → Rebuilding the interface."
cd backend && ../venv/bin/python -c 'from nexagent.main import init_state; init_state(); print("Database ready.")'
echo
echo "Next steps:"
echo "  1. scripts/run.sh create-admin     (first administrator; no default password exists)"
echo "  2. scripts/run.sh start"

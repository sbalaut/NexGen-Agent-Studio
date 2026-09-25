#!/usr/bin/env bash
# On an internet-connected machine with the SAME OS/CPU/Python version as the target server:
# download every wheel needed, so the server can install with scripts/install.sh --offline.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
mkdir -p wheelhouse
"$PY" -m pip download -d wheelhouse -r requirements.txt
[ "${1:-}" = "--with-training" ] && "$PY" -m pip download -d wheelhouse -r requirements-train.txt
echo "Copy the whole folder (including wheelhouse/) to the server."

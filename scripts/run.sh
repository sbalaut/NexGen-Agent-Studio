#!/usr/bin/env bash
# Start/stop/status NexAgent Studio without systemd (works inside a JupyterHub terminal).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
set -a; [ -f .env ] && . ./.env; set +a
DATA="${NEXAGENT_DATA_DIR:-data}"; case "$DATA" in /*) ;; *) DATA="$ROOT/$DATA" ;; esac; mkdir -p "$DATA/logs"
PID="$DATA/nexagent.pid"
case "${1:-start}" in
  start)
    if [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; then echo "Already running (pid $(cat "$PID"))"; exit 0; fi
    cd backend
    nohup ../venv/bin/python -m nexagent.cli serve >> "$DATA/logs/nexagent.log" 2>&1 &
    echo $! > "$PID"; sleep 3
    if kill -0 "$(cat "$PID")" 2>/dev/null; then echo "Started (pid $(cat "$PID")). Open ${NEXAGENT_ORIGIN:-http://127.0.0.1:8600}"; else echo "Failed to start; see $DATA/logs/nexagent.log"; tail -20 "$DATA/logs/nexagent.log"; exit 1; fi ;;
  stop)
    [ -f "$PID" ] && kill "$(cat "$PID")" 2>/dev/null && rm -f "$PID" && echo "Stopped" || echo "Not running" ;;
  status)
    [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null && echo "Running (pid $(cat "$PID"))" || echo "Not running" ;;
  create-admin)
    cd backend && ../venv/bin/python -m nexagent.cli create-admin ;;
  backup)
    cd backend && ../venv/bin/python -m nexagent.cli backup "${2:-$ROOT/backups}" ;;
  check)
    cd backend && ../venv/bin/python -m nexagent.cli check ;;
  logs)
    tail -f "$DATA/logs/nexagent.log" ;;
  *) echo "usage: scripts/run.sh start|stop|status|check|logs|create-admin|backup [dir]"; exit 2 ;;
esac

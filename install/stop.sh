#!/usr/bin/env bash
# Stop Elvin. Running AI tasks are interrupted safely (the journal rolls back half-applied changes).
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose stop
HOME_DIR="$(grep -E '^ELVIN_HOME_HOST=' .env 2>/dev/null | cut -d= -f2-)"
PIDFILE="${HOME_DIR:-$HOME/Elvin}/run/ollama.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then kill "$(cat "$PIDFILE")" && echo "Stopped Elvin's Ollama."; rm -f "$PIDFILE"; fi
echo "Elvin stopped. Your projects and data are kept."

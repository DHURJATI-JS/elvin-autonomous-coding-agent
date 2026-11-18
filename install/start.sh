#!/usr/bin/env bash
# Start Elvin (and, on Apple Silicon, Elvin's own Ollama) and open the browser.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "Run the installer first (Install-Elvin-Mac.command or install/install.sh)."; exit 1; }
get() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }
PORT="$(get ELVIN_PORT)"; PORT="${PORT:-8765}"
if ! docker info >/dev/null 2>&1; then
  [ "$(uname -s)" = Darwin ] && (open -ga Docker || true) || sudo systemctl start docker 2>/dev/null || true
  echo "Waiting for Docker..."; for _ in $(seq 1 100); do docker info >/dev/null 2>&1 && break; sleep 3; done
fi
if [ "$(get LOCAL_LLM_BACKEND)" = ollama ] && [ "$(uname -s)" = Darwin ]; then
  bash install/lib/macos-ollama.sh "$(get ELVIN_HOME_HOST)" "$(get QWEN_MODEL)" "$(get QWEN_AUTOCOMPLETE_MODEL)" --no-pull || echo "Ollama could not start - Elvin will fall back to other models."
fi
# CPU threads for the local Docker Gemma (performance cores) unless .env pins a value
. install/lib/cpu-threads.sh
[ -n "$(get LOCAL_GEMMA_THREADS)" ] || export LOCAL_GEMMA_THREADS="${LOCAL_GEMMA_THREADS:-$(gemma_threads)}"
docker compose up -d
for _ in $(seq 1 90); do curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 2; done
echo "Elvin is running at http://localhost:$PORT"
if [ "$(uname -s)" = Darwin ]; then open "http://localhost:$PORT"; else (xdg-open "http://localhost:$PORT" >/dev/null 2>&1 &) || true; fi

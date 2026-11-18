#!/usr/bin/env bash
# Installs Elvin's own copy of Ollama (Apple-Silicon Metal acceleration) and the
# Qwen Coder model that fits this Mac. Elvin's Ollama listens on 127.0.0.1:11435
# so it never collides with another Ollama on the default port 11434.
#   usage: macos-ollama.sh [ELVIN_HOME] [chat-model] [autocomplete-model] [--no-pull]
set -euo pipefail
ELVIN_HOME="${1:-$HOME/Elvin}"
OLLAMA_VERSION="v0.40.15"
OLLAMA_SHA256="607c79d5d6315ef9d896db83adde736a4c0057196329843544e79b95afb260a8"   # ollama-darwin.tgz (GitHub release digest)
PORT="${ELVIN_OLLAMA_PORT:-11435}"
BIN_DIR="$ELVIN_HOME/bin/ollama-$OLLAMA_VERSION"
mkdir -p "$ELVIN_HOME/bin" "$ELVIN_HOME/models" "$ELVIN_HOME/logs" "$ELVIN_HOME/run"

ram_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
if [ -n "${2:-}" ]; then CHAT_MODEL="$2"
elif [ "$ram_gb" -ge 64 ]; then CHAT_MODEL="qwen2.5-coder:32b"
elif [ "$ram_gb" -ge 32 ]; then CHAT_MODEL="qwen2.5-coder:14b"
elif [ "$ram_gb" -ge 16 ]; then CHAT_MODEL="qwen2.5-coder:7b"
else CHAT_MODEL="qwen2.5-coder:3b"; fi
if [ -n "${3:-}" ]; then AUTO_MODEL="$3"
elif [ "$ram_gb" -ge 16 ]; then AUTO_MODEL="qwen2.5-coder:1.5b-base"
else AUTO_MODEL="$CHAT_MODEL"; fi

if [ ! -x "$BIN_DIR/ollama" ]; then
  echo "==> Downloading Ollama $OLLAMA_VERSION for macOS"
  tmp="$(mktemp -d)"
  curl -fL --progress-bar "https://github.com/ollama/ollama/releases/download/$OLLAMA_VERSION/ollama-darwin.tgz" -o "$tmp/ollama.tgz"
  got="$(shasum -a 256 "$tmp/ollama.tgz" | awk '{print $1}')"
  if [ "$got" != "$OLLAMA_SHA256" ]; then echo "Checksum mismatch for ollama-darwin.tgz - aborting" >&2; exit 1; fi
  mkdir -p "$BIN_DIR"; tar -xzf "$tmp/ollama.tgz" -C "$BIN_DIR"; rm -rf "$tmp"
  [ -x "$BIN_DIR/ollama" ] || { echo "ollama binary missing after extract" >&2; exit 1; }
  xattr -dr com.apple.quarantine "$BIN_DIR" 2>/dev/null || true
fi
ln -sfn "$BIN_DIR" "$ELVIN_HOME/bin/ollama-current"

export OLLAMA_HOST="127.0.0.1:$PORT" OLLAMA_MODELS="$ELVIN_HOME/models" OLLAMA_CONTEXT_LENGTH=16384 \
       OLLAMA_KEEP_ALIVE=30m OLLAMA_NUM_PARALLEL=2 OLLAMA_MAX_LOADED_MODELS=2
if ! curl -fs "http://127.0.0.1:$PORT/api/version" >/dev/null 2>&1; then
  echo "==> Starting Elvin's Ollama on 127.0.0.1:$PORT (Metal)"
  nohup "$BIN_DIR/ollama" serve >> "$ELVIN_HOME/logs/ollama.log" 2>&1 &
  echo $! > "$ELVIN_HOME/run/ollama.pid"
  for _ in $(seq 1 60); do curl -fs "http://127.0.0.1:$PORT/api/version" >/dev/null 2>&1 && break; sleep 1; done
fi
curl -fs "http://127.0.0.1:$PORT/api/version" >/dev/null || { echo "Ollama did not start - see $ELVIN_HOME/logs/ollama.log" >&2; exit 1; }

if [ "${4:-}" = "--no-pull" ]; then echo "==> Elvin's Ollama is running on 127.0.0.1:$PORT"; exit 0; fi
for m in "$CHAT_MODEL" "$AUTO_MODEL"; do
  echo "==> Pulling $m (RAM: ${ram_gb} GB)"
  "$BIN_DIR/ollama" pull "$m"
done
printf 'QWEN_MODEL=%s\nQWEN_AUTOCOMPLETE_MODEL=%s\n' "$CHAT_MODEL" "$AUTO_MODEL" > "$ELVIN_HOME/run/qwen-models.env"
echo "==> Qwen ready: chat=$CHAT_MODEL autocomplete=$AUTO_MODEL"

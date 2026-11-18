#!/usr/bin/env bash
# Picks Elvin's local fallback models in Docker (CPU, one loaded at a time) from the memory
# Docker may use and the CPU's performance cores. Used by install/install.sh (macOS + Linux);
# install/install.ps1 mirrors the same table for Windows.
#
#   tier    Docker memory              Gemma 3       Qwen Coder   container limit
#   large   >= 16 GB and >= 8 cores    gemma3:12b    14b          12g
#   medium  >= 10 GB                   gemma3:4b     7b           7g
#   small   >= 6 GB                    gemma3:4b     3b           5g
#   weak    below 6 GB                 gemma3:1b     1.5b         2g   (no image reading)
#
# Sets LOCAL_TIER, LOCAL_GEMMA_MODEL, LOCAL_QWEN_MODEL and LOCAL_GEMMA_MEM.
pick_local_models() {
  local mem_gb="${1:-0}" cores="${2:-0}"
  case "$mem_gb" in ''|*[!0-9]*) mem_gb=0 ;; esac
  case "$cores" in ''|*[!0-9]*) cores=0 ;; esac
  if [ "$mem_gb" -ge 16 ] && [ "$cores" -ge 8 ]; then
    LOCAL_TIER=large;  LOCAL_GEMMA_MODEL="gemma3:12b"; LOCAL_QWEN_MODEL="qwen2.5-coder:14b";  LOCAL_GEMMA_MEM="12g"
  elif [ "$mem_gb" -ge 10 ]; then
    LOCAL_TIER=medium; LOCAL_GEMMA_MODEL="gemma3:4b";  LOCAL_QWEN_MODEL="qwen2.5-coder:7b";   LOCAL_GEMMA_MEM="7g"
  elif [ "$mem_gb" -ge 6 ]; then
    LOCAL_TIER=small;  LOCAL_GEMMA_MODEL="gemma3:4b";  LOCAL_QWEN_MODEL="qwen2.5-coder:3b";   LOCAL_GEMMA_MEM="5g"
  else
    LOCAL_TIER=weak;   LOCAL_GEMMA_MODEL="gemma3:1b";  LOCAL_QWEN_MODEL="qwen2.5-coder:1.5b"; LOCAL_GEMMA_MEM="2g"
  fi
}

# Is a coding model too small to rely on (under 3B parameters)?
tiny_model() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    *0.5b*|*1.5b*|*:1b|*:1b-*|*-1b*) return 0 ;;
    *) return 1 ;;
  esac
}

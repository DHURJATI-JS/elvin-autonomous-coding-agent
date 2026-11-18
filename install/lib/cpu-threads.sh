#!/usr/bin/env bash
# Prints the number of CPU threads the local Docker Gemma should use.
# Hybrid CPUs (Apple Silicon: performance + efficiency cores) generate fastest on
# their performance cores only - measured on an Apple M5 (4P+6E): 4 threads gave
# 10.4 tokens/s, all 10 gave 2.9. Elsewhere: physical cores. Never more than Docker has.
gemma_threads() {
  local p=0 d=0
  if [ "$(uname -s)" = Darwin ]; then
    p="$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null || sysctl -n hw.physicalcpu 2>/dev/null || echo 0)"
  elif command -v lscpu >/dev/null 2>&1; then
    p="$(lscpu -p=SOCKET,CORE 2>/dev/null | grep -v '^#' | sort -u | wc -l | tr -d ' ')"
  fi
  d="$(docker info --format '{{.NCPU}}' 2>/dev/null || echo 0)"
  case "$p" in ''|*[!0-9]*) p=0 ;; esac
  case "$d" in ''|*[!0-9]*) d=0 ;; esac
  if [ "$d" -gt 0 ] && [ "$p" -gt "$d" ]; then p="$d"; fi
  echo "$p"
}

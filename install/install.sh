#!/usr/bin/env bash
# =============================================================================
#  Elvin - one-click installer for macOS and Linux
#  * installs Docker if needed and starts it
#  * detects the hardware and picks the best local Qwen Coder model:
#      Apple Silicon -> Elvin's own Ollama with Metal (vLLM has no Metal support)
#      NVIDIA        -> vLLM container (CUDA)
#      AMD (Linux)   -> vLLM container (ROCm)
#      anything else -> not compatible: a Claude or ChatGPT API key is REQUIRED
#  * creates every folder, writes .env, builds and starts all containers
#  Re-running it is safe: secrets in an existing .env are kept.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
NONINTERACTIVE="${ELVIN_NONINTERACTIVE:-0}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  \033[36m›\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n  \033[31m✗ %s\033[0m\n\n' "$*" >&2; exit 1; }
ask() {  # ask VAR "question" "default" [secret]
  local __var="$1" __q="$2" __def="${3:-}" __secret="${4:-}" __ans=""
  local __env="ELVIN_${__var}"
  if [ -n "${!__env:-}" ]; then printf -v "$__var" '%s' "${!__env}"; return; fi
  if [ "$NONINTERACTIVE" = "1" ]; then printf -v "$__var" '%s' "$__def"; return; fi
  if [ -n "$__secret" ]; then read -r -s -p "  $__q: " __ans; echo; else read -r -p "  $__q [${__def}]: " __ans; fi
  printf -v "$__var" '%s' "${__ans:-$__def}"
}
env_get() { [ -f .env ] && grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }
rnd() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }

cat <<'BANNER'

   ███████╗██╗    ██╗   ██╗██╗███╗   ██╗
   ██╔════╝██║    ██║   ██║██║████╗  ██║    autonomous agentic coder
   █████╗  ██║    ██║   ██║██║██╔██╗ ██║    one-click installer
   ██╔══╝  ██║    ╚██╗ ██╔╝██║██║╚██╗██║
   ███████╗███████╗╚████╔╝ ██║██║ ╚████║
   ╚══════╝╚══════╝ ╚═══╝  ╚═╝╚═╝  ╚═══╝

BANNER

OS="$(uname -s)"; ARCH="$(uname -m)"
case "$OS" in Darwin) HOST_OS=macos ;; Linux) HOST_OS=linux ;; *) die "Unsupported system $OS - use the Windows installer on Windows." ;; esac
[ "$ARCH" = "aarch64" ] && ARCH=arm64; [ "$ARCH" = "x86_64" ] && ARCH=amd64
bold "1/7  System"; ok "$HOST_OS $ARCH"

# ----------------------------------------------------------------------------- Docker
bold "2/7  Docker"
docker_ready() { docker info >/dev/null 2>&1; }
if ! command -v docker >/dev/null 2>&1; then
  if [ "$HOST_OS" = macos ]; then
    info "Docker Desktop is not installed - downloading it from docker.com"
    DMG="$(mktemp -d)/Docker.dmg"
    curl -fL --progress-bar "https://desktop.docker.com/mac/main/${ARCH}/Docker.dmg" -o "$DMG"
    MNT="$(hdiutil attach -nobrowse -readonly "$DMG" | awk -F'\t' '/\/Volumes\//{print $NF}' | tail -1)"
    if ! cp -R "$MNT/Docker.app" /Applications/ 2>/dev/null; then sudo cp -R "$MNT/Docker.app" /Applications/; fi
    hdiutil detach "$MNT" -quiet || true
    ok "Docker Desktop installed in /Applications"
  else
    info "Installing Docker Engine with Docker's official script (needs sudo)"
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sudo sh /tmp/get-docker.sh
    sudo usermod -aG docker "$USER" || true
    sudo systemctl enable --now docker || true
    ok "Docker Engine installed"
  fi
fi
if ! docker_ready; then
  if [ "$HOST_OS" = macos ]; then open -ga Docker || open -a Docker || true; else sudo systemctl start docker 2>/dev/null || true; fi
  info "Waiting for Docker to start (first start can take a few minutes)..."
  for _ in $(seq 1 120); do docker_ready && break; sleep 3; done
fi
docker_ready || die "Docker is not running. Start Docker and run the installer again (on Linux you may need to log out and in once after installing Docker)."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (it ships with current Docker)."
ok "$(docker --version)"

# ----------------------------------------------------------------------------- hardware
bold "3/7  Hardware and the best local model"
if [ "$HOST_OS" = macos ]; then RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 )); else RAM_GB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1048576 )); fi
GPU=none; GPU_NAME=""; VRAM_GB=0; BACKEND=none; COMPAT=false
QWEN_MODEL=""; QWEN_AUTO=""; LOCAL_URL=""; COMPOSE_FILE="docker-compose.yml"; QWEN_CONTEXT=16384; ROCM_IMAGE=""
if [ "$HOST_OS" = macos ]; then
  GPU_NAME="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo Mac)"
  if [ "$ARCH" = arm64 ] && [ "$RAM_GB" -ge 8 ]; then
    GPU=apple; VRAM_GB=$RAM_GB; BACKEND=ollama; COMPAT=true; LOCAL_URL="http://host.docker.internal:11435"
    if   [ "$RAM_GB" -ge 64 ]; then QWEN_MODEL="qwen2.5-coder:32b"
    elif [ "$RAM_GB" -ge 32 ]; then QWEN_MODEL="qwen2.5-coder:14b"
    elif [ "$RAM_GB" -ge 16 ]; then QWEN_MODEL="qwen2.5-coder:7b"
    else QWEN_MODEL="qwen2.5-coder:3b"; fi
    if [ "$RAM_GB" -ge 16 ]; then QWEN_AUTO="qwen2.5-coder:1.5b-base"; else QWEN_AUTO="$QWEN_MODEL"; fi
  fi
else
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    GPU=nvidia; GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
    VRAM_GB=$(( $(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1) / 1024 ))
    if [ "$VRAM_GB" -ge 6 ]; then
      if ! docker info --format '{{json .Runtimes}}' | grep -q nvidia; then
        warn "The NVIDIA Container Toolkit is missing - installing it (needs sudo)"
        if command -v apt-get >/dev/null 2>&1; then
          curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
          curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
            | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
            | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
          sudo apt-get update -qq && sudo apt-get install -y -qq nvidia-container-toolkit
        elif command -v dnf >/dev/null 2>&1; then
          curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo >/dev/null
          sudo dnf install -y -q nvidia-container-toolkit
        fi
        sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker || true
      fi
      if docker info --format '{{json .Runtimes}}' | grep -q nvidia; then
        BACKEND=vllm; COMPAT=true; LOCAL_URL="http://vllm:8000/v1"; COMPOSE_FILE="docker-compose.yml:docker-compose.nvidia.yml"
        if   [ "$VRAM_GB" -ge 40 ]; then QWEN_MODEL="Qwen/Qwen2.5-Coder-32B-Instruct-AWQ"; QWEN_CONTEXT=32768
        elif [ "$VRAM_GB" -ge 20 ]; then QWEN_MODEL="Qwen/Qwen2.5-Coder-32B-Instruct-AWQ"; QWEN_CONTEXT=12288
        elif [ "$VRAM_GB" -ge 12 ]; then QWEN_MODEL="Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"
        elif [ "$VRAM_GB" -ge 8 ];  then QWEN_MODEL="Qwen/Qwen2.5-Coder-7B-Instruct-AWQ"
        else QWEN_MODEL="Qwen/Qwen2.5-Coder-3B-Instruct-AWQ"; QWEN_CONTEXT=8192; fi
        QWEN_AUTO="$QWEN_MODEL"
      else warn "GPU containers are not available - continuing without a local model"; fi
    fi
  elif [ -e /dev/kfd ] && command -v rocminfo >/dev/null 2>&1; then
    GPU=amd; GPU_NAME="$(rocminfo | awk -F: '/Marketing Name/{gsub(/^ +/,"",$2); print $2; exit}')"
    GFX="$(rocminfo | grep -o 'gfx[0-9a-f]*' | head -1)"
    VRAM_GB=$(( $(cat /sys/class/drm/card*/device/mem_info_vram_total 2>/dev/null | sort -n | tail -1 || echo 0) / 1073741824 ))
    if [ "$VRAM_GB" -ge 8 ]; then
      BACKEND=vllm; COMPAT=true; LOCAL_URL="http://vllm:8000/v1"; COMPOSE_FILE="docker-compose.yml:docker-compose.amd.yml"
      case "$GFX" in gfx9*) ROCM_IMAGE="rocm/vllm:rocm7.14.1_cdna_ubuntu24.04_py3.14_pytorch_2.11_vllm_0.23.0" ;;
                     *)     ROCM_IMAGE="rocm/vllm:rocm7.14.1_rdna_ubuntu24.04_py3.14_pytorch_2.11_vllm_0.23.0" ;; esac
      if   [ "$VRAM_GB" -ge 48 ]; then QWEN_MODEL="Qwen/Qwen2.5-Coder-14B-Instruct"
      elif [ "$VRAM_GB" -ge 24 ]; then QWEN_MODEL="Qwen/Qwen2.5-Coder-7B-Instruct"
      elif [ "$VRAM_GB" -ge 12 ]; then QWEN_MODEL="Qwen/Qwen2.5-Coder-3B-Instruct"
      else QWEN_MODEL="Qwen/Qwen2.5-Coder-1.5B-Instruct"; QWEN_CONTEXT=8192; fi
      QWEN_AUTO="$QWEN_MODEL"
    fi
  fi
fi
if [ "$GPU" = none ]; then   # look for integrated / unsupported graphics so we can say why the CPU is used
  if [ "$HOST_OS" = macos ]; then
    FOUND_GPU="$(system_profiler SPDisplaysDataType 2>/dev/null | awk -F': ' '/Chipset Model/{print $2; exit}')"
  else
    FOUND_GPU="$(lspci 2>/dev/null | grep -iE 'vga|3d controller|display' | head -1 | sed 's/^[^:]*: //' | cut -c1-80)"
  fi
  if [ -n "$FOUND_GPU" ]; then
    GPU_NAME="$FOUND_GPU"
    case "$(printf '%s' "$FOUND_GPU" | tr '[:upper:]' '[:lower:]')" in
      *intel*|*iris*|*uhd*|*"radeon(tm) graphics"*|*"radeon graphics"*|*vega*|*780m*|*760m*|*680m*|*660m*) GPU=igpu ;;
      *) GPU=other ;;
    esac
  fi
fi
ok "RAM ${RAM_GB} GB · GPU: ${GPU} ${GPU_NAME} ${VRAM_GB:+(${VRAM_GB} GB)}"
case "$GPU" in
  igpu)  warn "Integrated GPU ($GPU_NAME): it shares system memory and Docker can't use it for AI, so local models run on the CPU." ;;
  other) warn "This GPU ($GPU_NAME) isn't supported for local AI (Elvin uses NVIDIA, AMD with ROCm or Apple Silicon), so local models run on the CPU." ;;
esac
# Local fallbacks in Docker (CPU, one loaded at a time): sized by the memory Docker may use and the performance cores
DOCKER_MEM_GB=$(( $(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0) / 1073741824 ))
. install/lib/cpu-threads.sh
LOCAL_GEMMA_THREADS="$(gemma_threads)"
. install/lib/local-models.sh
pick_local_models "$DOCKER_MEM_GB" "$LOCAL_GEMMA_THREADS"
ok "Local fallbacks in Docker ($LOCAL_TIER): $LOCAL_GEMMA_MODEL + $LOCAL_QWEN_MODEL - Docker has ${DOCKER_MEM_GB} GB and ${LOCAL_GEMMA_THREADS} performance cores (effort is capped at medium while they answer)"
if [ "$COMPAT" = true ]; then ok "Local Qwen Coder: $QWEN_MODEL via $BACKEND"
else warn "This computer can't run a local Qwen Coder model well. Elvin will use Claude or ChatGPT instead."; fi

# ----------------------------------------------------------------------------- settings
bold "4/7  Settings"
ask GATEWAY_URL "Gemma 3 gateway URL, e.g. http://192.168.1.50:8000/v1 (OpenAI-compatible; leave empty if you don't have one)" "$(env_get GATEWAY_URL)"
case "${GATEWAY_URL%/}" in ""|*/v1) GATEWAY_URL="${GATEWAY_URL%/}" ;; *) GATEWAY_URL="${GATEWAY_URL%/}/v1" ;; esac
GATEWAY_API_KEY=""
if [ -n "$GATEWAY_URL" ]; then
  ask GATEWAY_API_KEY "Gateway API key" "$(env_get GATEWAY_API_KEY)"
  if curl -fs -m 5 -H "Authorization: Bearer $GATEWAY_API_KEY" "$GATEWAY_URL/models" >/dev/null 2>&1; then ok "Gemma gateway reachable"; else warn "Gemma gateway not reachable right now (Elvin will retry later and use its local Gemma meanwhile)"; fi
else
  ok "No gateway - Elvin uses its local Gemma in Docker (add one later on the System Status page)"
fi
ANTHROPIC_API_KEY="$(env_get ANTHROPIC_API_KEY)"; OPENAI_API_KEY="$(env_get OPENAI_API_KEY)"
# Too weak to run Elvin on local models alone -> a cloud key is compulsory
REQUIRE_KEY=""
if [ "$COMPAT" != true ]; then REQUIRE_KEY="it has no GPU that can run Qwen Coder (integrated GPUs and the CPU only get the slower Docker models)"
elif tiny_model "$QWEN_MODEL"; then REQUIRE_KEY="its GPU only fits a Qwen Coder smaller than 3B ($QWEN_MODEL)"; fi
if [ -z "$GATEWAY_URL" ] && [ "$LOCAL_TIER" = weak ]; then
  REQUIRE_KEY="${REQUIRE_KEY:+$REQUIRE_KEY; also, }there is no Gemma gateway and Docker has too little memory for a local Gemma that can read images"
fi
if [ -n "$REQUIRE_KEY" ]; then
  info "A Claude (Anthropic) or ChatGPT (OpenAI) API key is REQUIRED on this computer: $REQUIRE_KEY."
  while [ -z "$ANTHROPIC_API_KEY" ] && [ -z "$OPENAI_API_KEY" ]; do
    ask ANTHROPIC_API_KEY "Anthropic API key (blank to skip)" "" secret
    [ -z "$ANTHROPIC_API_KEY" ] && ask OPENAI_API_KEY "OpenAI API key (blank to skip)" "" secret
    if [ -z "$ANTHROPIC_API_KEY" ] && [ -z "$OPENAI_API_KEY" ]; then
      [ "$NONINTERACTIVE" = "1" ] && die "No API key given - Elvin cannot run on this computer without one."
      warn "Elvin cannot run here without one of these keys."
    fi
  done
else
  if [ -z "$ANTHROPIC_API_KEY" ]; then ask ANTHROPIC_API_KEY "Anthropic API key for Claude fallbacks (optional, Enter to skip)" "" secret; fi
  if [ -z "$OPENAI_API_KEY" ]; then ask OPENAI_API_KEY "OpenAI API key for ChatGPT (optional, Enter to skip)" "" secret; fi
fi
DEF_HOME="$(env_get ELVIN_HOME_HOST)"; ask ELVIN_HOME_HOST "Folder for your projects and Elvin's data" "${DEF_HOME:-$HOME/Elvin}"
DEF_PORT="$(env_get ELVIN_PORT)"; ask ELVIN_PORT "Port" "${DEF_PORT:-8765}"
DEF_BIND="$(env_get ELVIN_BIND)"; ask LAN "Allow other devices on your network to open Elvin? (y/N)" "$([ "${DEF_BIND:-127.0.0.1}" = 0.0.0.0 ] && echo y || echo n)"
case "$LAN" in [yY]*) ELVIN_BIND=0.0.0.0 ;; *) ELVIN_BIND=127.0.0.1 ;; esac

# ----------------------------------------------------------------------------- folders + .env
bold "5/7  Folders and configuration"
mkdir -p "$ELVIN_HOME_HOST"/{workspace,data,logs,bin,models,run}
ok "Created $ELVIN_HOME_HOST/{workspace,data,logs,bin,models,run}"
SECRET_KEY="$(env_get SECRET_KEY)"; SECRET_KEY="${SECRET_KEY:-$(rnd 64)}"
POSTGRES_PASSWORD="$(env_get POSTGRES_PASSWORD)"; POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(rnd 32)}"
RUNNER_TOKEN="$(env_get RUNNER_TOKEN)"; RUNNER_TOKEN="${RUNNER_TOKEN:-$(rnd 40)}"
if [ "$HOST_OS" = linux ]; then E_UID="$(id -u)"; E_GID="$(id -g)"; else E_UID=1000; E_GID=1000; fi
[ "$ELVIN_BIND" = 0.0.0.0 ] && LAN_IP="$( (ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}') | head -1)" || LAN_IP=""
umask 077
cat > .env <<EOF
# Written by install/install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Re-run the installer to change it.
SECRET_KEY=$SECRET_KEY
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
RUNNER_TOKEN=$RUNNER_TOKEN
ELVIN_HOME_HOST=$ELVIN_HOME_HOST
ELVIN_PORT=$ELVIN_PORT
ELVIN_BIND=$ELVIN_BIND
ELVIN_UID=$E_UID
ELVIN_GID=$E_GID
BASE_URL=http://localhost:$ELVIN_PORT
TRUSTED_ORIGINS=${LAN_IP:+http://$LAN_IP:$ELVIN_PORT}
HOST_HOME_HINT=$ELVIN_HOME_HOST
COMPOSE_FILE=$COMPOSE_FILE
COMPOSE_PATH_SEPARATOR=:
HOST_OS=$HOST_OS
HOST_ARCH=$ARCH
HOST_GPU=$GPU
HOST_GPU_NAME=$GPU_NAME
HOST_RAM_GB=$RAM_GB
HOST_VRAM_GB=$VRAM_GB
HOST_COMPATIBLE=$COMPAT
GATEWAY_URL=$GATEWAY_URL
GATEWAY_API_KEY=$GATEWAY_API_KEY
GATEWAY_MODEL=gemma
LOCAL_LLM_BACKEND=$BACKEND
LOCAL_LLM_URL=$LOCAL_URL
QWEN_MODEL=$QWEN_MODEL
QWEN_AUTOCOMPLETE_MODEL=$QWEN_AUTO
QWEN_CONTEXT=$QWEN_CONTEXT
ROCM_VLLM_IMAGE=$ROCM_IMAGE
LOCAL_GEMMA_MODEL=$LOCAL_GEMMA_MODEL
LOCAL_GEMMA_MEM=$LOCAL_GEMMA_MEM
LOCAL_QWEN_MODEL=$LOCAL_QWEN_MODEL
LOCAL_GEMMA_THREADS=$LOCAL_GEMMA_THREADS
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
OPENAI_API_KEY=$OPENAI_API_KEY
CLAMAV_REQUIRED=true
LOADTEST_ALLOW_PUBLIC=false
EOF
umask 022
ok ".env written (secrets kept private, mode 600)"

# ----------------------------------------------------------------------------- local model
bold "6/7  Local AI model"
if [ "$BACKEND" = ollama ]; then
  bash install/lib/macos-ollama.sh "$ELVIN_HOME_HOST" "$QWEN_MODEL" "$QWEN_AUTO"
elif [ "$BACKEND" = vllm ]; then
  ok "vLLM will download $QWEN_MODEL from Hugging Face on first start (several GB)."
else
  ok "Using cloud models only."
fi

# ----------------------------------------------------------------------------- build + start
bold "7/7  Building and starting Elvin's containers"
docker compose build
docker compose up -d
info "Waiting for Elvin to become ready..."
for _ in $(seq 1 90); do curl -fs "http://127.0.0.1:$ELVIN_PORT/healthz" >/dev/null 2>&1 && break; sleep 2; done
curl -fs "http://127.0.0.1:$ELVIN_PORT/healthz" >/dev/null 2>&1 || die "Elvin did not start - see: docker compose logs app"
ok "Elvin is running"
echo
bold "  Open http://localhost:$ELVIN_PORT and create your account."
[ -n "$LAN_IP" ] && echo "  Other devices on your network: http://$LAN_IP:$ELVIN_PORT"
echo "  The virus scanner downloads its signatures on first start (a few minutes)."
echo "  Start/stop later with Start-Elvin-Mac.command / Stop-Elvin-Mac.command (or install/start.sh, install/stop.sh)."
echo
if [ "$NONINTERACTIVE" != "1" ]; then
  if [ "$HOST_OS" = macos ]; then open "http://localhost:$ELVIN_PORT"; else (xdg-open "http://localhost:$ELVIN_PORT" >/dev/null 2>&1 &) || true; fi
fi

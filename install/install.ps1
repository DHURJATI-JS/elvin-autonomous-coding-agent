# =============================================================================
#  Elvin - one-click installer for Windows 10/11 (run via Install-Elvin-Windows.bat)
#  * installs WSL2 + Docker Desktop with winget if needed and starts Docker
#  * detects the GPU and picks the best local Qwen Coder model:
#      NVIDIA -> vLLM container (Docker Desktop + WSL2 GPU support)
#      AMD    -> Elvin's own Ollama for Windows (ROCm build) on 127.0.0.1:11435
#      none   -> not compatible: a Claude or ChatGPT API key is REQUIRED
#  * creates every folder, writes .env, builds and starts all containers
# =============================================================================
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$Root = (Get-Location).Path
$NonInteractive = $env:ELVIN_NONINTERACTIVE -eq "1"

function Say($t) { Write-Host "  > $t" -ForegroundColor Cyan }
function Ok($t) { Write-Host "  + $t" -ForegroundColor Green }
function Warn($t) { Write-Host "  ! $t" -ForegroundColor Yellow }
function Die($t) { Write-Host "`n  x $t`n" -ForegroundColor Red; Read-Host "Press Enter to close"; exit 1 }
function Head($t) { Write-Host "`n$t" -ForegroundColor White }
function EnvGet($k) {
  if (Test-Path ".env") { $l = Select-String -Path ".env" -Pattern "^$k=(.*)$" | Select-Object -First 1; if ($l) { return $l.Matches[0].Groups[1].Value } }
  return ""
}
function Ask($name, $question, $default, [switch]$Secret) {
  $fromEnv = [Environment]::GetEnvironmentVariable("ELVIN_$name")
  if ($fromEnv) { return $fromEnv }
  if ($NonInteractive) { return $default }
  if ($Secret) {
    $s = Read-Host "  $question" -AsSecureString
    return [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
  }
  $a = Read-Host "  $question [$default]"
  if ([string]::IsNullOrWhiteSpace($a)) { return $default } else { return $a }
}
function Rnd($n) {  # cryptographically secure random secret
  $chars = [char[]]"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
  $bytes = New-Object byte[] $n
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}
function Sha256($path) { (Get-FileHash -Algorithm SHA256 $path).Hash.ToLower() }

Write-Host @"

   ELVIN  -  autonomous agentic coder
   one-click installer for Windows

"@ -ForegroundColor Magenta

# ----------------------------------------------------------------------------- Docker
Head "1/7  Docker Desktop"
function DockerReady { try { docker info *> $null; return $LASTEXITCODE -eq 0 } catch { return $false } }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { Die "winget is missing. Install 'App Installer' from the Microsoft Store, then run this again." }
  $wslReady = $false
  try { wsl --status *> $null; $wslReady = ($LASTEXITCODE -eq 0) } catch { $wslReady = $false }
  if (-not $wslReady) {
    Say "Installing WSL2 (Docker Desktop needs it) - Windows may ask for permission"
    Start-Process wsl -ArgumentList "--install --no-distribution" -Verb RunAs -Wait
    Warn "WSL2 was installed. RESTART Windows, then run Install-Elvin-Windows.bat again."
    Read-Host "Press Enter to close"; exit 0
  }
  Say "Installing Docker Desktop with winget"
  winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
  Ok "Docker Desktop installed"
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
if (-not (DockerReady)) {
  $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
  if (Test-Path $dd) { Start-Process $dd }
  Say "Waiting for Docker to start (first start can take a few minutes)..."
  for ($i = 0; $i -lt 120 -and -not (DockerReady); $i++) { Start-Sleep 3 }
}
if (-not (DockerReady)) { Die "Docker is not running. Start Docker Desktop, finish its setup, and run this again." }
Ok (docker --version)

# ----------------------------------------------------------------------------- hardware
Head "2/7  Hardware and the best local model"
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
$arch = if ([Environment]::Is64BitOperatingSystem) { if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" } } else { "x86" }
$gpus = Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name
$gpu = "none"; $gpuName = ""; $vramGB = 0; $backend = "none"; $compat = $false
$qwen = ""; $qwenAuto = ""; $localUrl = ""; $composeFile = "docker-compose.yml"; $qwenCtx = 16384
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
  $line = (nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits | Select-Object -First 1)
  if ($line) {
    $parts = $line.Split(","); $gpu = "nvidia"; $gpuName = $parts[0].Trim(); $vramGB = [math]::Floor([int]$parts[1].Trim() / 1024)
    if ($vramGB -ge 6) {
      $backend = "vllm"; $compat = $true; $localUrl = "http://vllm:8000/v1"; $composeFile = "docker-compose.yml;docker-compose.nvidia.yml"
      if ($vramGB -ge 40) { $qwen = "Qwen/Qwen2.5-Coder-32B-Instruct-AWQ"; $qwenCtx = 32768 }
      elseif ($vramGB -ge 20) { $qwen = "Qwen/Qwen2.5-Coder-32B-Instruct-AWQ"; $qwenCtx = 12288 }
      elseif ($vramGB -ge 12) { $qwen = "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ" }
      elseif ($vramGB -ge 8) { $qwen = "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ" }
      else { $qwen = "Qwen/Qwen2.5-Coder-3B-Instruct-AWQ"; $qwenCtx = 8192 }
      $qwenAuto = $qwen
    }
  }
}
if ($gpu -eq "none") {
  $amd = $gpus | Where-Object { $_ -match "AMD|Radeon" } | Select-Object -First 1
  if ($amd) {
    $gpu = "amd"; $gpuName = $amd
    $key = "HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    Get-ChildItem $key -ErrorAction SilentlyContinue | ForEach-Object {
      try {
        $p = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
        if ($p.DriverDesc -match "AMD|Radeon" -and $p."HardwareInformation.qwMemorySize") { $vramGB = [math]::Max($vramGB, [math]::Floor([int64]$p."HardwareInformation.qwMemorySize" / 1GB)) }
      } catch { }
    }
    if ($vramGB -ge 8) {
      $backend = "ollama"; $compat = $true; $localUrl = "http://host.docker.internal:11435"
      if ($vramGB -ge 24) { $qwen = "qwen2.5-coder:14b" } elseif ($vramGB -ge 12) { $qwen = "qwen2.5-coder:7b" } else { $qwen = "qwen2.5-coder:3b" }
      $qwenAuto = if ($ramGB -ge 16) { "qwen2.5-coder:1.5b-base" } else { $qwen }
    }
  }
}
# integrated / unsupported graphics: say why the CPU is used
$igpuPattern = "Intel|Iris|UHD|Radeon\(TM\) Graphics|Radeon Graphics|Vega|780M|760M|680M|660M"
if ($gpu -eq "none") {
  $found = $gpus | Where-Object { $_ } | Select-Object -First 1
  if ($found) { $gpuName = $found; $gpu = if ($found -match $igpuPattern) { "igpu" } else { "other" } }
} elseif ($gpu -eq "amd" -and -not $compat -and $gpuName -match $igpuPattern) { $gpu = "igpu" }
Ok "RAM $ramGB GB - GPU: $gpu $gpuName $(if ($vramGB) { "($vramGB GB)" })"
if ($gpu -eq "igpu") { Warn "Integrated GPU ($gpuName): it shares system memory and Docker can't use it for AI, so local models run on the CPU." }
elseif ($gpu -eq "other") { Warn "This GPU ($gpuName) isn't supported for local AI (Elvin uses NVIDIA, AMD or Apple Silicon), so local models run on the CPU." }

# Local fallbacks in Docker (CPU, one loaded at a time): sized by the memory Docker may use and the performance cores
# (same table as install/lib/local-models.sh)
$dockerMemGB = 0
try { $dockerMemGB = [math]::Floor([int64](docker info --format "{{.MemTotal}}") / 1GB) } catch { $dockerMemGB = 0 }
# CPU threads for them: physical cores (hyper-threads only slow them down), never more than Docker has
$gemmaThreads = 0
try { $gemmaThreads = [int](Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum } catch {}
try { $dockerCpus = [int](docker info --format "{{.NCPU}}"); if ($dockerCpus -gt 0 -and $gemmaThreads -gt $dockerCpus) { $gemmaThreads = $dockerCpus } } catch {}
if ($dockerMemGB -ge 16 -and $gemmaThreads -ge 8) { $localTier = "large"; $gemmaModel = "gemma3:12b"; $qwenLocal = "qwen2.5-coder:14b"; $gemmaMem = "12g" }
elseif ($dockerMemGB -ge 10) { $localTier = "medium"; $gemmaModel = "gemma3:4b"; $qwenLocal = "qwen2.5-coder:7b"; $gemmaMem = "7g" }
elseif ($dockerMemGB -ge 6) { $localTier = "small"; $gemmaModel = "gemma3:4b"; $qwenLocal = "qwen2.5-coder:3b"; $gemmaMem = "5g" }
else { $localTier = "weak"; $gemmaModel = "gemma3:1b"; $qwenLocal = "qwen2.5-coder:1.5b"; $gemmaMem = "2g" }
Ok "Local fallbacks in Docker ($localTier): $gemmaModel + $qwenLocal - Docker has $dockerMemGB GB and $gemmaThreads cores (effort is capped at medium while they answer)"
if ($compat) { Ok "Local Qwen Coder: $qwen via $backend" } else { Warn "This computer can't run a local Qwen Coder model well. Elvin will use Claude or ChatGPT instead." }

# ----------------------------------------------------------------------------- settings
Head "3/7  Settings"
$gatewayUrl = Ask "GATEWAY_URL" "Gemma 3 gateway URL, e.g. http://192.168.1.50:8000/v1 (leave empty if you don't have one)" (EnvGet "GATEWAY_URL")
if ($gatewayUrl) { $gatewayUrl = $gatewayUrl.TrimEnd('/'); if (-not $gatewayUrl.EndsWith('/v1')) { $gatewayUrl = "$gatewayUrl/v1" } }
$gatewayKey = ""
if ($gatewayUrl) {
  $gatewayKey = Ask "GATEWAY_API_KEY" "Gateway API key" (EnvGet "GATEWAY_API_KEY")
  try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Headers @{ Authorization = "Bearer $gatewayKey" } "$gatewayUrl/models" | Out-Null; Ok "Gemma gateway reachable" }
  catch { Warn "Gemma gateway not reachable right now (Elvin will retry later and use its local Gemma meanwhile)" }
} else { Ok "No gateway - Elvin uses its local Gemma in Docker (add one later on the System Status page)" }
$anthropic = EnvGet "ANTHROPIC_API_KEY"; $openai = EnvGet "OPENAI_API_KEY"
# Too weak to run Elvin on local models alone -> a cloud key is compulsory
$requireKey = ""
if (-not $compat) { $requireKey = "it has no GPU that can run Qwen Coder (integrated GPUs and the CPU only get the slower Docker models)" }
elseif ($qwen -match "(?i)(0\.5|1\.5)b") { $requireKey = "its GPU only fits a Qwen Coder smaller than 3B ($qwen)" }
if (-not $gatewayUrl -and $localTier -eq "weak") {
  $requireKey = $(if ($requireKey) { "$requireKey; also, " } else { "" }) + "there is no Gemma gateway and Docker has too little memory for a local Gemma that can read images"
}
if ($requireKey) {
  Say "A Claude (Anthropic) or ChatGPT (OpenAI) API key is REQUIRED on this computer: $requireKey."
  while (-not $anthropic -and -not $openai) {
    $anthropic = Ask "ANTHROPIC_API_KEY" "Anthropic API key (blank to skip)" "" -Secret
    if (-not $anthropic) { $openai = Ask "OPENAI_API_KEY" "OpenAI API key (blank to skip)" "" -Secret }
    if (-not $anthropic -and -not $openai) { if ($NonInteractive) { Die "No API key given - Elvin cannot run on this computer without one." }; Warn "Elvin cannot run here without one of these keys." }
  }
} else {
  if (-not $anthropic) { $anthropic = Ask "ANTHROPIC_API_KEY" "Anthropic API key for Claude fallbacks (optional, Enter to skip)" "" -Secret }
  if (-not $openai) { $openai = Ask "OPENAI_API_KEY" "OpenAI API key for ChatGPT (optional, Enter to skip)" "" -Secret }
}
$defHome = EnvGet "ELVIN_HOME_HOST"; if (-not $defHome) { $defHome = (Join-Path $env:USERPROFILE "Elvin") -replace "\\", "/" }
$elvinHome = (Ask "ELVIN_HOME_HOST" "Folder for your projects and Elvin's data" $defHome) -replace "\\", "/"
$defPort = EnvGet "ELVIN_PORT"; if (-not $defPort) { $defPort = "8765" }
$port = Ask "ELVIN_PORT" "Port" $defPort
$lan = Ask "LAN" "Allow other devices on your network to open Elvin? (y/N)" "n"
$bind = if ($lan -match "^[yY]") { "0.0.0.0" } else { "127.0.0.1" }

# ----------------------------------------------------------------------------- folders + .env
Head "4/7  Folders and configuration"
foreach ($d in "workspace", "data", "logs", "bin", "models", "run") { New-Item -ItemType Directory -Force -Path (Join-Path $elvinHome $d) | Out-Null }
Ok "Created $elvinHome\{workspace,data,logs,bin,models,run}"
$secret = EnvGet "SECRET_KEY"; if (-not $secret) { $secret = Rnd 60 }
$pg = EnvGet "POSTGRES_PASSWORD"; if (-not $pg) { $pg = Rnd 32 }
$runner = EnvGet "RUNNER_TOKEN"; if (-not $runner) { $runner = Rnd 40 }
$lanIp = ""
if ($bind -eq "0.0.0.0") { $lanIp = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notmatch "^(127|169)\." -and $_.PrefixOrigin -ne "WellKnown" } | Select-Object -First 1).IPAddress }
$envText = @"
# Written by install\install.ps1 on $(Get-Date -Format s). Re-run the installer to change it.
SECRET_KEY=$secret
POSTGRES_PASSWORD=$pg
RUNNER_TOKEN=$runner
ELVIN_HOME_HOST=$elvinHome
ELVIN_PORT=$port
ELVIN_BIND=$bind
ELVIN_UID=1000
ELVIN_GID=1000
BASE_URL=http://localhost:$port
TRUSTED_ORIGINS=$(if ($lanIp) { "http://${lanIp}:$port" })
HOST_HOME_HINT=$elvinHome
COMPOSE_FILE=$composeFile
COMPOSE_PATH_SEPARATOR=;
HOST_OS=windows
HOST_ARCH=$arch
HOST_GPU=$gpu
HOST_GPU_NAME=$gpuName
HOST_RAM_GB=$ramGB
HOST_VRAM_GB=$vramGB
HOST_COMPATIBLE=$($compat.ToString().ToLower())
GATEWAY_URL=$gatewayUrl
GATEWAY_API_KEY=$gatewayKey
GATEWAY_MODEL=gemma
LOCAL_LLM_BACKEND=$backend
LOCAL_LLM_URL=$localUrl
QWEN_MODEL=$qwen
QWEN_AUTOCOMPLETE_MODEL=$qwenAuto
QWEN_CONTEXT=$qwenCtx
LOCAL_GEMMA_MODEL=$gemmaModel
LOCAL_GEMMA_MEM=$gemmaMem
LOCAL_QWEN_MODEL=$qwenLocal
LOCAL_GEMMA_THREADS=$gemmaThreads
ANTHROPIC_API_KEY=$anthropic
OPENAI_API_KEY=$openai
CLAMAV_REQUIRED=true
LOADTEST_ALLOW_PUBLIC=false
"@
[IO.File]::WriteAllText((Join-Path $Root ".env"), $envText.Replace("`r`n", "`n"), (New-Object Text.UTF8Encoding($false)))
Ok ".env written"

# ----------------------------------------------------------------------------- local model
Head "5/7  Local AI model"
if ($backend -eq "ollama") {
  $ver = "v0.40.15"
  $bin = Join-Path $elvinHome "bin\ollama-$ver"
  $assets = @{ "ollama-windows-amd64.zip" = "9ca74a7ab66ec86870b1eecac7df160df08dc4e3a291ab5d2d59bcf9310221c6";
               "ollama-windows-amd64-rocm.zip" = "2f5e81675329a5de2477a4a25823d55d5c3ead8d59522e92eac1664ab492faeb" }
  if (-not (Test-Path (Join-Path $bin "ollama.exe"))) {
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    foreach ($name in $assets.Keys) {
      $zip = Join-Path $env:TEMP $name
      Say "Downloading $name (large file, please wait)"
      Invoke-WebRequest -UseBasicParsing "https://github.com/ollama/ollama/releases/download/$ver/$name" -OutFile $zip
      if ((Sha256 $zip) -ne $assets[$name]) { Die "Checksum mismatch for $name" }
      Expand-Archive -Force $zip -DestinationPath $bin
      Remove-Item $zip
    }
  }
  $env:OLLAMA_HOST = "127.0.0.1:11435"; $env:OLLAMA_MODELS = Join-Path $elvinHome "models"; $env:OLLAMA_CONTEXT_LENGTH = "16384"; $env:OLLAMA_KEEP_ALIVE = "30m"
  Start-Process -WindowStyle Hidden (Join-Path $bin "ollama.exe") -ArgumentList "serve"
  for ($i = 0; $i -lt 30; $i++) { try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:11435/api/version | Out-Null; break } catch { Start-Sleep 1 } }
  foreach ($m in @($qwen, $qwenAuto) | Select-Object -Unique) { Say "Pulling $m"; & (Join-Path $bin "ollama.exe") pull $m }
  Ok "Qwen ready via Elvin's Ollama (AMD GPU)"
} elseif ($backend -eq "vllm") { Ok "vLLM will download $qwen from Hugging Face on first start (several GB)." }
else { Ok "Using cloud models only." }

# ----------------------------------------------------------------------------- build + start
Head "6/7  Building Elvin's containers"
docker compose build
if ($LASTEXITCODE -ne 0) { Die "docker compose build failed" }
Head "7/7  Starting Elvin"
docker compose up -d
if ($LASTEXITCODE -ne 0) { Die "docker compose up failed" }
for ($i = 0; $i -lt 90; $i++) { try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/healthz" | Out-Null; $up = $true; break } catch { Start-Sleep 2 } }
if (-not $up) { Die "Elvin did not start - run: docker compose logs app" }
Ok "Elvin is running at http://localhost:$port"
if ($lanIp) { Ok "Other devices on your network: http://${lanIp}:$port" }
Write-Host "  The virus scanner downloads its signatures on first start (a few minutes)."
Write-Host "  Start/stop later with Start-Elvin-Windows.bat / Stop-Elvin-Windows.bat"
if (-not $NonInteractive) { Start-Process "http://localhost:$port"; Read-Host "`nPress Enter to close" }

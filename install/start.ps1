# Start Elvin on Windows (and Elvin's own Ollama when the AMD backend is used).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path ".env")) { Write-Host "Run Install-Elvin-Windows.bat first."; Read-Host; exit 1 }
$cfg = @{}; Get-Content .env | Where-Object { $_ -match "^[A-Z_]+=" } | ForEach-Object { $k, $v = $_ -split "=", 2; $cfg[$k] = $v }
$port = if ($cfg["ELVIN_PORT"]) { $cfg["ELVIN_PORT"] } else { "8765" }
try { docker info *> $null } catch {}
if ($LASTEXITCODE -ne 0) {
  $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"; if (Test-Path $dd) { Start-Process $dd }
  for ($i = 0; $i -lt 100; $i++) { docker info *> $null; if ($LASTEXITCODE -eq 0) { break }; Start-Sleep 3 }
}
if ($cfg["LOCAL_LLM_BACKEND"] -eq "ollama") {
  $exe = Get-ChildItem (Join-Path $cfg["ELVIN_HOME_HOST"] "bin") -Recurse -Filter ollama.exe -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($exe) {
    $env:OLLAMA_HOST = "127.0.0.1:11435"; $env:OLLAMA_MODELS = Join-Path $cfg["ELVIN_HOME_HOST"] "models"; $env:OLLAMA_CONTEXT_LENGTH = "16384"
    try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:11435/api/version | Out-Null } catch { Start-Process -WindowStyle Hidden $exe.FullName -ArgumentList "serve" }
  }
}
if (-not $cfg["LOCAL_GEMMA_THREADS"] -and -not $env:LOCAL_GEMMA_THREADS) {   # older installs: physical cores, capped at Docker's CPUs
  try { $t = [int](Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum; $d = [int](docker info --format "{{.NCPU}}")
        if ($d -gt 0 -and $t -gt $d) { $t = $d }; if ($t -gt 0) { $env:LOCAL_GEMMA_THREADS = "$t" } } catch {}
}
docker compose up -d
for ($i = 0; $i -lt 90; $i++) { try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/healthz" | Out-Null; break } catch { Start-Sleep 2 } }
Write-Host "Elvin is running at http://localhost:$port"
Start-Process "http://localhost:$port"

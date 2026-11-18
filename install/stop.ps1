# Stop Elvin on Windows. Your projects and data are kept.
Set-Location (Join-Path $PSScriptRoot "..")
docker compose stop
Get-Process ollama -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*\Elvin\bin\*" } | Stop-Process -Force
Write-Host "Elvin stopped."

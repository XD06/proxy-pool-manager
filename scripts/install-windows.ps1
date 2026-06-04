param(
  [string]$Version = "1.13.13",
  [string]$SingBoxZip = "",
  [switch]$SkipPythonDeps
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

New-Item -ItemType Directory -Force -Path "bin", "config", "tmp" | Out-Null

$projectConfig = Join-Path $Root "config\sing-box.json"
$runningEngine = Get-CimInstance Win32_Process -Filter "name = 'sing-box.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*$projectConfig*" }
if ($runningEngine) {
  $pids = ($runningEngine | Select-Object -ExpandProperty ProcessId) -join ", "
  throw "Project sing-box is still running (pid: $pids). Stop it first: .\scripts\stop-service.ps1"
}

if (-not $SkipPythonDeps) {
  if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
  }
  .\.venv\Scripts\python.exe -m pip install --upgrade pip
  .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}

$zipPath = $SingBoxZip
if (-not $zipPath) {
  $downloadZip = Join-Path $Root "tmp\sing-box-$Version-windows-amd64.zip"
  if (-not (Test-Path $downloadZip)) {
    $url = "https://github.com/SagerNet/sing-box/releases/download/v$Version/sing-box-$Version-windows-amd64.zip"
    Invoke-WebRequest -Uri $url -OutFile $downloadZip
  }
  $zipPath = $downloadZip
}

if (-not (Test-Path $zipPath)) {
  throw "sing-box zip not found: $zipPath"
}

$extractDir = Join-Path $Root "tmp\sing-box-$Version-windows-amd64"
if (Test-Path $extractDir) {
  Remove-Item -LiteralPath $extractDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force

$binary = Get-ChildItem -Path $extractDir -Recurse -Filter "sing-box.exe" | Select-Object -First 1
if (-not $binary) {
  throw "sing-box.exe not found in $zipPath"
}

$targetBinary = Join-Path $Root "bin\sing-box.exe"
if (Test-Path $targetBinary) {
  $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
  Copy-Item -LiteralPath $targetBinary -Destination (Join-Path $Root "bin\sing-box-backup-$stamp.exe") -Force
}
Copy-Item -LiteralPath $binary.FullName -Destination $targetBinary -Force

$appConfigPath = Join-Path $Root "config\app.json"
if (-not (Test-Path $appConfigPath)) {
  @"
{
  "host": "127.0.0.1",
  "port": 9000,
  "proxy_listen_host": "127.0.0.1",
  "proxy_public_host": "",
  "clash_api_addr": "127.0.0.1:9090",
  "domain_resolve_strategy": ""
}
"@ | Set-Content -Path $appConfigPath -Encoding UTF8
}

Write-Output "Installed Proxy Pool Manager dependencies."
& (Join-Path $Root "bin\sing-box.exe") version
Write-Output "Start with: .\scripts\start-service.ps1"

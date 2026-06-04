param(
  [string]$HostName,
  [int]$Port = 0,
  [string]$ProxyListenHost
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$ConfigPath = Join-Path $Root "config\app.json"
$Config = @{}
if (Test-Path $ConfigPath) {
  $Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
}

$EffectiveHost = if ($PSBoundParameters.ContainsKey("HostName")) { $HostName } elseif ($Config.host) { $Config.host } else { "127.0.0.1" }
$EffectivePort = if ($PSBoundParameters.ContainsKey("Port") -and $Port -gt 0) { $Port } elseif ($Config.port) { [int]$Config.port } else { 9000 }
$EffectiveProxyListenHost = if ($PSBoundParameters.ContainsKey("ProxyListenHost")) { $ProxyListenHost } elseif ($Config.proxy_listen_host) { $Config.proxy_listen_host } else { "127.0.0.1" }

$existing = Get-NetTCPConnection -LocalPort $EffectivePort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existing) {
  Write-Output "Proxy Pool Manager is already listening on $EffectiveHost`:$EffectivePort (pid $($existing.OwningProcess))."
  return
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root "tmp") | Out-Null
$env:PPM_HOST = $EffectiveHost
$env:PPM_PORT = [string]$EffectivePort
$env:PPM_PROXY_LISTEN_HOST = $EffectiveProxyListenHost
if ($Config.proxy_public_host) {
  $env:PPM_PROXY_PUBLIC_HOST = [string]$Config.proxy_public_host
}

$PythonExe = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
  $PythonExe = "python"
}

Start-Process -WindowStyle Hidden `
  -WorkingDirectory $Root `
  -FilePath $PythonExe `
  -ArgumentList @("main.py") `
  -RedirectStandardOutput (Join-Path $Root "tmp\server.out.log") `
  -RedirectStandardError (Join-Path $Root "tmp\server.err.log")

Start-Sleep -Seconds 2
$statusUrl = "http://127.0.0.1:$EffectivePort/api/status"
try {
  $status = Invoke-WebRequest -Uri $statusUrl -UseBasicParsing -TimeoutSec 10 | Select-Object -ExpandProperty Content
  Write-Output "Started Proxy Pool Manager: http://$EffectiveHost`:$EffectivePort"
  Write-Output "Proxy listen host: $EffectiveProxyListenHost"
  Write-Output $status
} catch {
  Write-Output "Service start was requested, but status check failed: $($_.Exception.Message)"
  Write-Output "Check tmp\server.err.log"
}

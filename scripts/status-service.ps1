param(
  [int]$Port = 0
)

$Root = Split-Path -Parent $PSScriptRoot
$ProjectSingBox = Join-Path $Root "bin\sing-box.exe"
$ConfigPath = Join-Path $Root "config\app.json"
$Config = @{}
if (Test-Path $ConfigPath) {
  $Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
}
$EffectivePort = if ($PSBoundParameters.ContainsKey("Port") -and $Port -gt 0) { $Port } elseif ($Config.port) { [int]$Config.port } else { 9000 }
$EffectiveHost = if ($Config.host) { [string]$Config.host } else { "127.0.0.1" }

Write-Output "Web service:"
$listener = Get-NetTCPConnection -LocalPort $EffectivePort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
  Write-Output "  listening on $EffectiveHost`:$EffectivePort, pid $($listener.OwningProcess)"
  try {
    Invoke-WebRequest -Uri "http://127.0.0.1:$EffectivePort/api/status" -UseBasicParsing -TimeoutSec 10 |
      Select-Object -ExpandProperty Content
  } catch {
    Write-Output "  API status failed: $($_.Exception.Message)"
  }
} else {
  Write-Output "  not listening"
}

Write-Output ""
Write-Output "Project sing-box:"
$processes = Get-CimInstance Win32_Process -Filter "name = 'sing-box.exe'" |
  Where-Object { $_.CommandLine -like "*$Root\config\sing-box.json*" }
if ($processes) {
  $processes | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | Format-List
} else {
  Write-Output "  not running"
}

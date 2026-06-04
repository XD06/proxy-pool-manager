param(
  [int]$Port = 0,
  [switch]$KeepEngine
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$ProjectSingBox = Join-Path $Root "bin\sing-box.exe"
$ConfigPath = Join-Path $Root "config\app.json"
$Config = @{}
if (Test-Path $ConfigPath) {
  $Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
}
$EffectivePort = if ($PSBoundParameters.ContainsKey("Port") -and $Port -gt 0) { $Port } elseif ($Config.port) { [int]$Config.port } else { 9000 }

$listener = Get-NetTCPConnection -LocalPort $EffectivePort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
  Stop-Process -Id $listener.OwningProcess -Force
  Write-Output "Stopped Web service on port $EffectivePort (pid $($listener.OwningProcess))."
} else {
  Write-Output "Web service on port $EffectivePort is not listening."
}

if (-not $KeepEngine) {
  $processes = Get-CimInstance Win32_Process -Filter "name = 'sing-box.exe'" |
    Where-Object { $_.CommandLine -like "*$Root\config\sing-box.json*" }
  foreach ($process in $processes) {
    Stop-Process -Id $process.ProcessId -Force
    Write-Output "Stopped project sing-box (pid $($process.ProcessId))."
  }
}

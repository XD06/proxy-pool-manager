$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$Ok = 0
$Warn = 0
$Fail = 0

function Pass($Message) {
  $script:Ok += 1
  Write-Output "[OK]   $Message"
}

function Warn($Message) {
  $script:Warn += 1
  Write-Output "[WARN] $Message"
}

function Fail($Message) {
  $script:Fail += 1
  Write-Output "[FAIL] $Message"
}

function Read-AppConfig($Key, $Default) {
  $Path = "config\app.json"
  if (-not (Test-Path $Path)) {
    return $Default
  }
  try {
    $Data = Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    $Value = $Data.$Key
    if ($null -eq $Value -or "$Value" -eq "") {
      return $Default
    }
    return $Value
  } catch {
    return $Default
  }
}

Write-Output "=== Proxy Pool Manager doctor ==="
Write-Output "root: $Root"

if (Get-Command python -ErrorAction SilentlyContinue) {
  Pass "python: $((Get-Command python).Source)"
} else {
  Fail "python not found"
}

if (Test-Path ".venv\Scripts\python.exe") {
  Pass "venv python: .venv\Scripts\python.exe"
} else {
  Warn "venv python not found; run scripts\install-windows.ps1"
}

if (Test-Path "requirements.txt") {
  Pass "requirements.txt exists"
} else {
  Fail "requirements.txt missing"
}

if (Test-Path "config\app.json") {
  Pass "config\app.json exists"
} else {
  Warn "config\app.json missing; run scripts\install-windows.ps1"
}

$Port = Read-AppConfig "port" 9000
$HostValue = Read-AppConfig "host" "127.0.0.1"
$ProxyListenHost = Read-AppConfig "proxy_listen_host" "127.0.0.1"
$ClashApiAddr = Read-AppConfig "clash_api_addr" "127.0.0.1:9090"
Write-Output "config: host=$HostValue port=$Port proxy_listen_host=$ProxyListenHost clash_api_addr=$ClashApiAddr"

if (Test-Path "bin\sing-box.exe") {
  Pass "sing-box executable: bin\sing-box.exe"
  try {
    & "bin\sing-box.exe" version | Select-Object -First 1
  } catch {
    Warn "sing-box version failed: $($_.Exception.Message)"
  }
} else {
  Warn "bin\sing-box.exe missing"
}

if (Test-Path "proxycheck-api\proxycheck.exe") {
  Pass "proxycheck executable: proxycheck-api\proxycheck.exe"
} else {
  Warn "proxycheck.exe missing; run go build -o proxycheck.exe .\cmd\proxycheck inside proxycheck-api"
}

if (Test-Path "tmp\server.pid") {
  $PidText = (Get-Content "tmp\server.pid" -Raw).Trim()
  if ($PidText -and (Get-Process -Id ([int]$PidText) -ErrorAction SilentlyContinue)) {
    Pass "web pid file running: $PidText"
  } else {
    Warn "tmp\server.pid exists but process is not running"
  }
} else {
  Warn "web service not running by tmp\server.pid"
}

try {
  $Status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 5
  Pass "web API reachable: http://127.0.0.1:$Port/api/status"
  $Expected = @($Status.expected_ports)
  $Listening = @($Status.listening_ports)
  $Missing = $Expected | Where-Object { $Listening -notcontains $_ }
  Write-Output "status: running=$($Status.running) ready=$($Status.ready) listening=$($Listening.Count)/$($Expected.Count)"
  if ($Missing.Count -gt 0) {
    Write-Output "missing ports: $(([array]$Missing | Select-Object -First 20) -join ',')"
  }
} catch {
  Warn "web API not reachable on 127.0.0.1:$Port"
}

$ConfigPath = (Resolve-Path "config\sing-box.json" -ErrorAction SilentlyContinue)
if ($ConfigPath) {
  $SingBoxProcess = Get-CimInstance Win32_Process -Filter "name = 'sing-box.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$($ConfigPath.Path)*" }
  if ($SingBoxProcess) {
    Pass "project sing-box process found"
  } else {
    Warn "project sing-box process not found"
  }
} else {
  Warn "config\sing-box.json missing"
}

Write-Output "=== Summary: ok=$Ok warn=$Warn fail=$Fail ==="
if ($Fail -gt 0) {
  exit 1
}

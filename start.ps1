$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PidFile = Join-Path $ProjectRoot "logs\server.pid"

if (-not (Test-Path -LiteralPath $Python)) {
    py -3.12 -m venv (Join-Path $ProjectRoot ".venv")
    & $Python -m pip install --disable-pip-version-check -e $ProjectRoot
}

New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "data"), (Join-Path $ProjectRoot "logs") | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "data\password.txt"))) {
    if (-not $env:ADMIN_INITIAL_PASSWORD) {
        throw "首次启动需要设置 ADMIN_INITIAL_PASSWORD 环境变量"
    }
    "# username:password:role`nadmin:$($env:ADMIN_INITIAL_PASSWORD):admin" | Set-Content -LiteralPath (Join-Path $ProjectRoot "data\password.txt") -Encoding UTF8
}

$env:PYTHONPATH = Join-Path $ProjectRoot "src"
$env:APP_CONFIG_PATH = Join-Path $ProjectRoot "config\app.json"
$env:CHECKS_CONFIG_PATH = Join-Path $ProjectRoot "config\checks.yaml"
$Process = Start-Process -FilePath $Python -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "31880", "--proxy-headers") -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
$Process.Id | Set-Content -LiteralPath $PidFile
Write-Output "personal-system-inspector started, PID=$($Process.Id)"

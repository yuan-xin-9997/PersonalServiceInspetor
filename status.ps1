$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $ProjectRoot "logs\server.pid"

if (Test-Path -LiteralPath $PidFile) {
    $ProcessId = [int](Get-Content -LiteralPath $PidFile -Raw)
    $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($Process) {
        Write-Output "process=running pid=$ProcessId"
    } else {
        Write-Output "process=stale-pid pid=$ProcessId"
    }
} else {
    Write-Output "process=stopped"
}

Invoke-RestMethod -Uri "http://127.0.0.1:31880/health/ready"

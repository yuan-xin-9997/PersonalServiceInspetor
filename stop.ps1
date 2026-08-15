$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $ProjectRoot "logs\server.pid"

if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Output "personal-system-inspector is not running"
    exit 0
}

$ProcessId = [int](Get-Content -LiteralPath $PidFile -Raw)
Stop-Process -Id $ProcessId -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PidFile -Force
Write-Output "personal-system-inspector stopped"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir   = Split-Path -Parent $ScriptDir
$MuMuDir   = "D:\Program Files\Netease\MuMuPlayer-12.0\nx_main"
$Python    = Join-Path $RepoDir ".venv\Scripts\python.exe"

$env:PATH = "$MuMuDir;$env:PATH"
if (-not $env:ANDROID_SERIAL) { $env:ANDROID_SERIAL = "127.0.0.1:16384" }

Push-Location $RepoDir
try {
    Write-Host "启动 CoC Autopilot 看板: http://127.0.0.1:8899" -ForegroundColor Cyan
    & $Python "coc_dashboard.py"
} finally {
    Pop-Location
}

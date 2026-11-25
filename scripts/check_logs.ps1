param(
    [int]$Last = 15,
    [int]$Tail = 0,
    [switch]$Open,
    [string]$TaskName = "CoC-Autopilot"
)

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir    = Split-Path -Parent $ScriptDir
$LogRoot    = Join-Path $RepoDir "logs"
$SummaryLog = Join-Path $LogRoot "summary.log"
$LatestLog  = Join-Path $LogRoot "latest.log"

if ($Open) {
    if (Test-Path $LogRoot) { Start-Process explorer.exe $LogRoot }
    exit 0
}

Write-Host "=== 定时任务状态 ($TaskName) ===" -ForegroundColor Cyan
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host "状态: $($task.State) | 上次运行: $($info.LastRunTime) (结果=$($info.LastTaskResult)) | 下次运行: $($info.NextRunTime)"
} else {
    Write-Host "未注册定时任务（可运行 scripts\install_task.ps1 安装）" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== 最近 $Last 次巡检汇总 ($SummaryLog) ===" -ForegroundColor Cyan
if (Test-Path $SummaryLog) {
    Get-Content $SummaryLog -Tail $Last -Encoding UTF8
} else {
    Write-Host "暂无汇总记录" -ForegroundColor Yellow
}

if ($Tail -gt 0) {
    Write-Host ""
    Write-Host "=== 最近一次详细日志尾部 $Tail 行 ($LatestLog) ===" -ForegroundColor Cyan
    if (Test-Path $LatestLog) {
        Get-Content $LatestLog -Tail $Tail -Encoding UTF8
    } else {
        Write-Host "暂无 latest.log" -ForegroundColor Yellow
    }
}

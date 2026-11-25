param(
    [string]$TaskName = "CoC-Autopilot"
)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除定时任务: $TaskName" -ForegroundColor Green
} else {
    Write-Host "未找到定时任务: $TaskName" -ForegroundColor Yellow
}

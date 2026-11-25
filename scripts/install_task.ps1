param(
    [string]$TaskName = "CoC-Autopilot",
    [string[]]$Times = @("04:00", "10:00", "16:00"),
    [ValidateSet("None", "Sleep", "Hibernate", "Shutdown")]
    [string]$PowerAction = "Sleep",
    [switch]$Dry,
    [int]$VmIndex = 0
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AutoRun   = Join-Path $ScriptDir "auto_run.ps1"

$argList = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$AutoRun`" -VmIndex $VmIndex -PowerAction $PowerAction"
if ($Dry) { $argList += " -Dry" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argList
$timeList = @($Times | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$triggers = @()
foreach ($t in $timeList) {
    $triggers += New-ScheduledTaskTrigger -Daily -At $t
}

$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$runLevel = if ($isAdmin) { "Highest" } else { "Limited" }

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel $runLevel

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description "CoC Autopilot 自动启动 MuMu 模拟器执行巡检，结束后执行 $PowerAction" `
    -Force | Out-Null

Write-Host "已注册定时任务: $TaskName" -ForegroundColor Green
Write-Host "  每日时间点 : $($timeList -join ', ')"
Write-Host "  结束后动作 : $PowerAction"
Write-Host "  运行权限   : $runLevel"
Write-Host "  唤醒定时器 : 已启用 (WakeToRun=True)"
if (-not $isAdmin) {
    Write-Host "提示：当前非管理员权限，已使用 Limited。若 S0 深度睡眠下无法唤醒，请以管理员运行本脚本提升为 Highest。" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "提示：请确认 Windows 电源选项中「睡眠 -> 允许使用唤醒定时器」为【启用】。" -ForegroundColor Yellow

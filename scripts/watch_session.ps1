param(
    [double]$Minutes = 120,
    [double]$Jitter = 10,
    [double]$ShortMaxMin = 30,
    [double]$SpeedMult = 1,
    [double]$BoostMinutes = 0,
    [switch]$ShortOnly,
    [int]$VmIndex = 0,
    [switch]$Dry,
    [switch]$KeepEmulator,
    [switch]$KeepGame,
    [ValidateSet("None", "Sleep", "Hibernate", "Shutdown")]
    [string]$PowerAction = "None",
    [int]$BootTimeoutSec = 180,
    [int]$KeepLogDays = 30,
    [int]$UserActiveWindowSec = 180,
    [int]$WakeDetectWindowSec = 120
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir   = Split-Path -Parent $ScriptDir
$MuMuDir   = "D:\Program Files\Netease\MuMuPlayer-12.0\nx_main"
$MuMuMgr   = Join-Path $MuMuDir "MuMuManager.exe"
$Python    = Join-Path $RepoDir ".venv\Scripts\python.exe"
$WatchPy   = Join-Path $ScriptDir "watch_session.py"

$LogRoot    = Join-Path $RepoDir "logs"
$RunsDir    = Join-Path $LogRoot "runs"
$LatestLog  = Join-Path $LogRoot "latest.log"
$SummaryLog = Join-Path $LogRoot "summary.log"
$LockFile   = Join-Path $LogRoot "auto_run.lock"

New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null
$RunId   = "watch_" + (Get-Date -Format "yyyyMMdd_HHmmss")
$RunLog  = Join-Path $RunsDir "$RunId.log"
$StartAt = Get-Date

$env:PATH = "$MuMuDir;$env:PATH"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Write-Log([string]$Msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Msg"
    Write-Host $line
    Add-Content -Path $RunLog -Value $line -Encoding UTF8
}

function Prune-OldLogs([int]$Days) {
    if ($Days -le 0) { return }
    $cutoff = (Get-Date).AddDays(-$Days)
    Get-ChildItem -Path $RunsDir -Filter "*.log" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

function Get-VmInfo {
    $raw = & $MuMuMgr info -v $VmIndex 2>$null | Out-String
    if (-not $raw.Trim()) { return $null }
    try { return ($raw | ConvertFrom-Json) } catch { return $null }
}

function Get-IdleSeconds {
    try {
        if (-not ("Win32IdleHelper" -as [type])) {
            Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class Win32IdleHelper {
    [StructLayout(LayoutKind.Sequential)]
    struct LASTINPUTINFO {
        public uint cbSize;
        public uint dwTime;
    }
    [DllImport("user32.dll")]
    static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    public static double GetIdleSeconds() {
        LASTINPUTINFO info = new LASTINPUTINFO();
        info.cbSize = (uint)Marshal.SizeOf(info);
        if (!GetLastInputInfo(ref info)) return 999999;
        uint diff = unchecked((uint)Environment.TickCount - info.dwTime);
        return diff / 1000.0;
    }
}
"@
        }
        return [int][math]::Floor([Win32IdleHelper]::GetIdleSeconds())
    } catch {
        return 999999
    }
}

function Test-JustWokeFromSleep([int]$WindowSec) {
    try {
        $ev = Get-WinEvent -FilterHashtable @{
            LogName      = "System"
            ProviderName = "Microsoft-Windows-Kernel-Power", "Microsoft-Windows-Power-Troubleshooter"
            Id           = 1, 107, 507
            StartTime    = (Get-Date).AddSeconds(-$WindowSec)
        } -MaxEvents 1 -ErrorAction SilentlyContinue
        return ($null -ne $ev)
    } catch {
        return $false
    }
}

# 防重入锁（与定时巡检共用锁，值守期间最多锁 4 小时防止定时巡检撞车）
if (Test-Path $LockFile) {
    $lockAge = (Get-Date) - (Get-Item $LockFile).LastWriteTime
    if ($lockAge.TotalMinutes -lt 240) {
        $owner = (Get-Content $LockFile -ErrorAction SilentlyContinue | Out-String).Trim()
        Write-Log "检测到另一轮正在运行（$owner），跳过本次值守"
        Copy-Item -Path $RunLog -Destination $LatestLog -Force -ErrorAction SilentlyContinue
        exit 0
    }
}
Set-Content -Path $LockFile -Value "pid=$PID start=$RunId" -Encoding UTF8

$rc = 1
$status = "FAIL"
$note = ""
$finalLine = ""
$pkg = "com.tencent.tmgp.supercell.clashofclans"

$idleAtStart = Get-IdleSeconds
$userWasActive = ($idleAtStart -lt $UserActiveWindowSec)
$justWoke = Test-JustWokeFromSleep -WindowSec $WakeDetectWindowSec
$pcWasAlreadyOn = (-not $justWoke) -or $userWasActive
$emuWasRunning = $false
$gameWasRunning = $false

try {
    Write-Log "=== 开始拟人值守 RunId=$RunId Minutes=$Minutes (±$Jitter) VM=$VmIndex Dry=$Dry PowerAction=$PowerAction ==="
    Write-Log "初始状态: 刚唤醒=$justWoke 启动空闲=${idleAtStart}s 电脑原已开机=$pcWasAlreadyOn"

    # 1. 启动/复用模拟器
    $info = Get-VmInfo
    if ($info -and $info.is_process_started) {
        $emuWasRunning = $true
        Write-Log "MuMu 模拟器 #$VmIndex 本来就在运行（不启动、结束也不关闭）"
    } else {
        Write-Log "启动 MuMu 模拟器 #$VmIndex ..."
        & $MuMuMgr control -v $VmIndex launch | Out-Null
    }

    # 2. 等待 Android 就绪
    $deadline = (Get-Date).AddSeconds($BootTimeoutSec)
    $serial = $null
    while ((Get-Date) -lt $deadline) {
        $info = Get-VmInfo
        if ($info -and $info.is_android_started -and $info.adb_port) {
            $hostIp = if ($info.adb_host_ip) { $info.adb_host_ip } else { "127.0.0.1" }
            $serial = "${hostIp}:$($info.adb_port)"
            break
        }
        Start-Sleep -Seconds 3
    }
    if (-not $serial) { throw "等待模拟器启动超时 (${BootTimeoutSec}s)" }
    Write-Log "Android 已就绪，ADB=$serial"

    # 3. 连接 ADB
    $env:ANDROID_SERIAL = $serial
    $adbReady = $false
    while ((Get-Date) -lt $deadline) {
        & adb connect $serial *>$null
        $boot = (& adb -s $serial shell getprop sys.boot_completed 2>$null | Out-String).Trim()
        if ($boot -eq "1") {
            $adbReady = $true
            break
        }
        Start-Sleep -Seconds 3
    }
    if (-not $adbReady) { throw "等待 ADB 就绪超时 ($serial)" }

    # 4. 接替模式：无论游戏是否已在运行，直接接管进入智能排程值守
    $gamePid = (& adb -s $serial shell pidof $pkg 2>$null | Out-String).Trim()
    if ($gamePid) {
        Write-Log "检测到游戏已在运行 (pid=$gamePid)，直接接管开始值守"
    }
    Start-Sleep -Seconds 2
    $pyArgs = @($WatchPy, "--minutes", "$Minutes", "--jitter", "$Jitter", "--short-max-min", "$ShortMaxMin", "--speed-mult", "$SpeedMult", "--boost-minutes", "$BoostMinutes")
    if ($ShortOnly) { $pyArgs += "--short-only" }
    if ($Dry) { $pyArgs += "--dry" }
    if ($KeepGame) { $pyArgs += "--keep-game" }
    Write-Log "执行: $Python $($pyArgs -join ' ')"
    Push-Location $RepoDir
    try {
        & $Python @pyArgs 2>&1 | ForEach-Object {
            $s = "$_"
            Write-Host $s
            Add-Content -Path $RunLog -Value $s -Encoding UTF8
            if ($s -match "最终 |值守完成:") { $finalLine = $s }
        }
        $rc = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($rc -eq 0) { $status = "OK" } else { $status = "FAIL"; $note = "exit=$rc" }
    Write-Log "值守结束，状态=$status 退出码=$rc"
}
catch {
    $status = "ERROR"
    $note = $_.Exception.Message
    Write-Log "执行异常: $note"
    $rc = 1
}
finally {
    if ($KeepEmulator -or $emuWasRunning -or $gameWasRunning) {
        Write-Log "保留 MuMu 模拟器 #$VmIndex 不关闭 (Keep=$KeepEmulator, 原已运行=$emuWasRunning, 游戏原已运行=$gameWasRunning)"
    } else {
        Write-Log "关闭 MuMu 模拟器 #$VmIndex ..."
        try {
            & $MuMuMgr control -v $VmIndex shutdown | Out-Null
            Start-Sleep -Seconds 5
        } catch {
            Write-Log "关闭模拟器异常: $_"
        }
    }

    $idleAtEnd = Get-IdleSeconds
    $effectivePower = $PowerAction
    if ($PowerAction -ne "None") {
        if ($gameWasRunning) {
            Write-Log "游戏原本就在运行，跳过 $PowerAction"
            $effectivePower = "$PowerAction(skipped:game-running)"
        } elseif ($pcWasAlreadyOn) {
            Write-Log "电脑启动前本来就开着（刚唤醒=$justWoke，启动空闲=${idleAtStart}s），跳过 $PowerAction"
            $effectivePower = "$PowerAction(skipped:pc-already-on)"
        } elseif ($idleAtEnd -lt $UserActiveWindowSec) {
            Write-Log "值守期间检测到用户操作电脑（结束空闲=${idleAtEnd}s < ${UserActiveWindowSec}s），跳过 $PowerAction"
            $effectivePower = "$PowerAction(skipped:user-active)"
        }
    }

    $durSec = [int][math]::Round(((Get-Date) - $StartAt).TotalSeconds)
    $modeTag = if ($Dry) { "watch-dry" } else { "watch" }
    $detail = if ($finalLine) { $finalLine } elseif ($note) { $note } else { "-" }
    $summary = "$RunId | $status | $modeTag | ${durSec}s | power=$effectivePower | $detail | log=runs/$RunId.log"
    Add-Content -Path $SummaryLog -Value $summary -Encoding UTF8
    Copy-Item -Path $RunLog -Destination $LatestLog -Force -ErrorAction SilentlyContinue
    Prune-OldLogs -Days $KeepLogDays
    Remove-Item -Path $LockFile -Force -ErrorAction SilentlyContinue

    if ($effectivePower -eq "Sleep") {
        Write-Log "15 秒后进入睡眠（内存待机，不写休眠文件）..."
        Copy-Item -Path $RunLog -Destination $LatestLog -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 15
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.Application]::SetSuspendState([System.Windows.Forms.PowerState]::Suspend, $false, $false) | Out-Null
    } elseif ($effectivePower -eq "Hibernate") {
        Write-Log "15 秒后进入休眠..."
        Copy-Item -Path $RunLog -Destination $LatestLog -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 15
        shutdown /h
    } elseif ($effectivePower -eq "Shutdown") {
        Write-Log "30 秒后关机（如需取消请运行 shutdown /a）"
        Copy-Item -Path $RunLog -Destination $LatestLog -Force -ErrorAction SilentlyContinue
        shutdown /s /t 30 /c "CoC Autopilot 值守已完成，30秒后自动关机（可运行 shutdown /a 取消）"
    }
}

exit $rc

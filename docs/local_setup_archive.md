# CoC-Autopilot Windows + MuMu 模拟器本地部署与排错归档

> 归档日期：2026-11-26  
> Fork 仓库：`https://github.com/xxh200812345/coc-autopilot`（上游：`FFatTiger/coc-autopilot`）  
> 本地路径：`C:\Users\xxh20\coc-autopilot`  
> 关联对话 Session ID：`7bcb8620-0537-429d-bd77-a2935c677505`

---

## 1. 分辨率适配调研结论

### 1.1 原项目是否支持多分辨率？
* **不支持**。虽然底层抓帧/触控（`mc.py` / `mt.py`）会动态读取设备分辨率并按比例换算，但上层业务逻辑全部按 **`2220×1080`（18.5:9 横屏）** 写死了绝对像素坐标。
* 在 `1920×1080` 下直接运行会导致：
  * 右对齐 UI（资源区 `RES_CROP=(1830,2160)`、工人框 `WORKER_BOX`、建造按钮 `BUILD_BTN=(2091,918)`、建筑列表 `LIST_BOX`、资源图标切片 `2170:2208`、右上角关闭点等）**直接越界（`x > 1920`）**；
  * 居中 UI（确认弹窗、底部建筑面板、研究弹窗等）因屏幕中心从 `1110` 变为 `960`，整体**右偏约 `150px`**。
* README 中提到的「只改 `coc_patrol2.py` 顶部 6 个常量」并不完整，实际有几十处硬编码坐标分散在函数体和内存后端文件中。

### 1.2 最终决策
* 由于使用 **MuMu 模拟器 12**，直接将模拟器物理分辨率设为 **`2220×1080`（竖屏 `1080×2220`）**，即可**零改动业务代码**直接运行 OCR 后端。
* 若未来要改造成原生支持 `1920×1080` 及任意横屏分辨率，建议以 `2220×1080` 为基准按 `scale = H / 1080` + 左/中/右锚点做坐标换算（高度同为 `1080` 时 `coc_templates/` 模板图大概率可直接复用）。

---

## 2. 本地部署过程与踩坑排错记录

| # | 现象 / 报错 | 根因 | 解决方案 |
| :--- | :--- | :--- | :--- |
| 1 | `adb` 命令找不到 | 未装全局 Android SDK，ADB 位于 MuMu 安装目录 | 运行前将 `D:\Program Files\Netease\MuMuPlayer-12.0\nx_main` 加入 `PATH`，并通过 `MuMuManager.exe info -v 0` 动态获取端口（`127.0.0.1:16384`） |
| 2 | Windows 下运行 `coc_patrol_run.py` 会报 `ModuleNotFoundError: No module named 'fcntl'` 及缺少 `signal.SIGALRM` | 原项目按 Linux 编写，使用了 `fcntl.lockf`、`/tmp/coc_patrol.lock` 和 `signal.SIGALRM` | 在 `coc_patrol_run.py` 做最小 Windows 兼容保护（`try/except ImportError` 降级文件锁、Windows 下改用 `tempfile.gettempdir()`、`hasattr(signal, "SIGALRM")` 判断），不改动 Linux 行为和游戏逻辑 |
| 3 | 导入 `rapidocr_onnxruntime` 时 Python 静默崩溃，退出码 `-1073741819`（`0xC0000005` Access Violation） | `pip` 默认安装的 `onnxruntime==1.32.0` 在当前 Windows + Python 3.12 环境下存在原生 DLL 崩溃 | 在 `.venv` 中降级固定为 **`onnxruntime==1.20.1`**，恢复正常 |
| 4 | 任务计划程序首次触发失败，日志报 `执行异常: * daemon not running; starting now at tcp:5037` | PowerShell 5.1 下 `$ErrorActionPreference = "Stop"` 会把原生命令（`adb connect`）写到 stderr 的正常启动提示当成致命异常中止脚本 | 将 `scripts/auto_run.ps1` 的 `$ErrorActionPreference` 改为 `"Continue"`，通过显式检查 `$LASTEXITCODE` 和异常抛出控制流程 |
| 5 | 定时任务下收尾报 `UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'` | 定时任务拉起 Python 时 stdout 默认使用 Windows GBK 编码，无法输出 `✓` 符号 | 在 `scripts/auto_run.ps1` 中设置 `PYTHONUTF8=1` 和 `PYTHONIOENCODING=utf-8` |
| 6 | 设置 UTF-8 后，`latest.log` 里 Python 输出出现乱码（如 `杩涙潙瀹屾垚`），且 `summary.log` 未抓到「最终」摘要行 | Python 输出 UTF-8，但 PowerShell 5.1 管道读取默认仍按 GBK 解码 | 在 `scripts/auto_run.ps1` 中补充 `[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)` 和 `$OutputEncoding` |
| 7 | `install_task.ps1` 传 `-Times "08:00","12:00"` 报无法转换为 `DateTime` | `powershell -File` 会把逗号分隔参数拼成单个字符串 `"08:00,12:00"` | 在 `install_task.ps1` 内部统一按逗号 `split(",")` 解析时间数组 |
| 8 | 每轮收尾提示 `锁屏 ✗` | 模拟器默认常亮不响应 `KEYCODE_SLEEP` | 新建 `config.json` 将 `"lock_screen_after_run": false` |
| 9 | 未安装 `minicap`，日志提示回退 `screencap` | 模拟器内未推送 `minicap` 二进制 | 自动降级走 `adb screencap`，整轮含启停模拟器约 117~185 秒，稳定可用 |
| 10 | 偶尔单轮 `收取前` 或 `收取后` 的 `elixir` 读成 `None` | 圣水满仓高亮/「已满」字样或帧抖动影响 HSV 白字提取 | 正常波动，不影响下一轮巡检和升级（升级预算取 `after` 有效值） |

---

## 3. Windows 定时唤醒 & 睡眠（S0 vs S3）专题记录

### 3.1 为什么选「睡眠」而非「休眠」？
* **休眠（S4 / `shutdown /h`）** 每次都会把整份内存写入硬盘 `hiberfil.sys`，一天多次会增加 SSD 写入损耗。
* **睡眠（内存待机）** 不写硬盘、唤醒只需 1~2 秒、所有窗口原样保留，且原生支持 Windows 任务计划程序的 `WakeToRun` 定时器唤醒。
* 脚本中通过调用 Windows 原生 API：
  ```powershell
  Add-Type -AssemblyName System.Windows.Forms
  [System.Windows.Forms.Application]::SetSuspendState([System.Windows.Forms.PowerState]::Suspend, $false, $false)
  ```
  确保进入**纯睡眠**而非误触发休眠（避开 `rundll32 powrprof.dll,SetSuspendState` 的坑）。

### 3.2 本机睡眠状态实测（`powercfg /a` 与系统事件日志）
* **支持状态**：本机仅支持 **`待机 (S0 低电量待机) 连接的网络`**、休眠、快速启动；**不支持经典 S3**（`系统固件不支持此待机状态；当支持 S0 低电量待机时，禁用此待机状态`）。
* **注意事项**：不要在 Windows 里盲目通过注册表 `PlatformAoAcOverride` 强禁 S0，固件无 S3 表时会导致系统「睡眠」选项直接消失。
* **实测表现**：
  * 本机主板对 S0 深度空闲（**DRIPS**）实现良好——彻底进入深度待机后，**风扇完全停转、显卡/机箱氛围灯全灭、按电源键秒亮且所有对话框原样保留**，体验等同于 S3 且不写硬盘。
  * 若有时看到睡眠后风扇仍在低速转，是因为 S0 处于「连接的网络」浅度待机（后台更新/刚退出的模拟器服务短暂持有唤醒锁），等后台释放锁后即掉入 DRIPS 停转。如需更快停转，可关闭 S0 睡眠时联网（Disconnected Standby）。
* **唤醒定时器**：`powercfg /q SCHEME_CURRENT SUB_SLEEP RTCWAKE` 实测台式机交流电源下已处于**启用（`0x1`）**，实测定时唤醒（`00:12:19 exiting Modern Standby` → 跑完巡检 → `00:15:41 entering sleep`）一次通过。

---

## 4. 自动化脚本体系与防打扰策略

### 4.1 目录与脚本清单（`C:\Users\xxh20\coc-autopilot\scripts\`）
* `scripts/auto_run.ps1`：全流程主控脚本（防重入锁 → 初始状态检测 → 启停 MuMu → ADB 等待 → 游戏运行检查 → 跑巡检 → 写日志/清理旧日志 → 按条件睡眠）
* `scripts/install_task.ps1`：注册 Windows 定时任务 `CoC-Autopilot`（当前正式配置：每日 **`04:00, 10:00, 16:00`**，`WakeToRun=True`，`-PowerAction Sleep`）
* `scripts/uninstall_task.ps1`：卸载定时任务
* `scripts/check_logs.ps1`：一键查看定时任务状态、最近 15 次汇总、以及最新详细日志
* `scripts/dashboard.ps1`：一键启动 Web 看板（`http://127.0.0.1:8899`）

### 4.2 三层「原样进、原样出」防打扰逻辑
1. **电脑层面**：通过系统事件日志检查最近 120 秒内是否有睡眠唤醒事件（Event ID `1 / 107 / 507`）+ Win32 `GetLastInputInfo` 检测启动/结束时用户键鼠空闲时间（阈值 180 秒）。只要电脑本来就开着或中途你动了键鼠，**绝不触发睡眠**。
2. **模拟器层面**：启动前检查 `MuMuManager.exe info -v 0`，若 MuMu 本来就在运行，**不重复启动，跑完也不关闭**。
3. **游戏层面**：连上 ADB 后执行 `pidof com.tencent.tmgp.supercell.clashofclans`，若游戏本来就在运行，**直接跳过本轮（`SKIP`），不启动、不关闭、不操作、不关模拟器、不睡眠**。

### 4.3 日志管理结构（`logs/`，已加入 `.gitignore`）
* `logs/summary.log`：每次运行追加 1 行摘要（时间、状态、模式、耗时、电源动作、资源/工人/星星、详细日志路径）
* `logs/latest.log`：始终保存最近一次完整日志
* `logs/runs/yyyyMMdd_HHmmss.log`：按次归档，默认自动清理 30 天前旧日志
* `logs/auto_run.lock`：防重入锁（超过 30 分钟残留自动失效）

---

## 5. 常用运维命令

```powershell
cd C:\Users\xxh20\coc-autopilot

# 查看定时任务状态 + 最近 15 次汇总 + 最近一次最后 40 行日志
powershell -ExecutionPolicy Bypass -File .\scripts\check_logs.ps1 -Tail 40

# 打开日志文件夹
powershell -ExecutionPolicy Bypass -File .\scripts\check_logs.ps1 -Open

# 手动跑一轮（不睡眠）
powershell -ExecutionPolicy Bypass -File .\scripts\auto_run.ps1

# 启动 Web 看板（http://127.0.0.1:8899）
powershell -ExecutionPolicy Bypass -File .\scripts\dashboard.ps1

# 修改定时时间点
powershell -ExecutionPolicy Bypass -File .\scripts\install_task.ps1 -Times "04:00,10:00,16:00" -PowerAction Sleep

# 卸载定时任务
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_task.ps1
```

---

## 6. 重启后回到 Antigravity 对话 Session
* **桌面端 / IDE**：打开软件后在左侧边栏历史对话列表点击本对话（可 Pin 置顶或 Star 标星）。
* **CLI (`agy`)**：运行 `agy` 后输入 `/resume`（或 `agy --resume`），选择 ID `7bcb8620-0537-429d-bd77-a2935c677505`。

# 一键启动数字人全链路：静态页(8000) + WS 桥(9000) + harness(5557) + 浏览器
#
#   .\tools\start_all.ps1                  # 相机模式，固定种子
#   .\tools\start_all.ps1 -NoCam           # 无相机（只看拟人空闲动作）
#   .\tools\start_all.ps1 -Seed 0          # 每次随机
#   .\tools\start_all.ps1 -DryRun          # 只打印将要做什么，不启动
#   .\tools\start_all.ps1 -Stop            # 停掉这三个服务
#
# 说明：每个服务各占一个 PowerShell 窗口，方便单独看日志 / 单独关。
#      harness 窗口里输出同时写到 run_console.log。

param(
    [switch]$NoCam,
    [int]$Seed = 12345,
    [int]$Port = 8000,
    [string]$Backend = 'auto',   # auto | msmf | dshow | any
    [string]$Fourcc  = 'MJPG',   # MJPG | YUY2 | NONE
    [int]$FpsCap    = 30,         # 0 = 不封顶
    [int]$UiEvery   = 2,          # 预览每 N 帧刷一次
    [switch]$NoBrowser,
    [switch]$Stop,
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
# 解释器：优先 $env:GAZE_PYTHON，否则用 PATH 里的 python
$Py = if ($env:GAZE_PYTHON) { $env:GAZE_PYTHON } else { "python" }

# 本脚本在 tools/ 下
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$web  = Join-Path $root "3d"
$log  = Join-Path $root "run_console.log"
$env:PYTHONIOENCODING = "utf-8"

function Test-Port([int]$p) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $ok = $c.ConnectAsync("127.0.0.1", $p).Wait(300)
        $c.Close()
        return $ok
    } catch { return $false }
}

function Get-PidsOnPort([int]$p) {
    $lines = netstat -ano | Select-String ":$p\s+.*LISTENING"
    $lines | ForEach-Object { ($_.Line.Trim() -split '\s+')[-1] } | Sort-Object -Unique
}

# ---------------- 停止 ----------------
if ($Stop) {
    Write-Host "== 停止数字人链路 =="
    foreach ($p in @(5557, 9000, $Port)) {
        $pids = Get-PidsOnPort $p
        if (-not $pids) { Write-Host ("  端口 {0,-5} 本来就空着" -f $p); continue }
        foreach ($procId in $pids) {
            try {
                Stop-Process -Id $procId -Force -ErrorAction Stop
                Write-Host ("  端口 {0,-5} 已停止 (PID {1})" -f $p, $procId)
            } catch {
                Write-Host ("  端口 {0,-5} 停止失败 (PID {1}): {2}" -f $p, $procId, $_.Exception.Message)
            }
        }
    }
    exit 0
}

# ---------------- 启动 ----------------
Write-Host "== 数字人启动器 =="
Write-Host ("  Python : {0}" -f $Py)
Write-Host ("  根目录 : {0}" -f $root)
Write-Host ("  页面   : http://localhost:{0}/index.html" -f $Port)
Write-Host ("  种子   : {0}" -f $(if ($Seed -eq 0) { "随机" } else { $Seed }))
if ($DryRun) { Write-Host "  [DryRun] 只打印，不启动`n" } else { Write-Host "" }

function Start-Window([string]$title, [string]$cmd) {
    Write-Host ("  -> {0}" -f $title)
    Write-Host ("     {0}" -f $cmd)
    if ($DryRun) { return }
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList @("-NoExit", "-NoProfile", "-Command", "`$Host.UI.RawUI.WindowTitle='$title'; $cmd")
}

# 1) 静态服务器（页面 + VRM）
if (Test-Port $Port) {
    Write-Host ("  [{0}] 已在运行，跳过" -f $Port)
} else {
    Start-Window "digit-human · static:$Port" `
        "Set-Location '$web'; & '$Py' -m http.server $Port"
}

# 2) WS 桥（ZMQ 5557 -> WS 9000）
if (Test-Port 9000) {
    Write-Host "  [9000] 已在运行，跳过"
} else {
    Start-Window "digit-human · ws-bridge:9000" `
        "Set-Location '$root'; & '$Py' -u -m tools.ws_bridge"
}

# 3) harness（发布 5557）
$camArg = if ($NoCam) { " --no-cam" } else { "" }
if (Test-Port 5557) {
    Write-Host "  [5557] 已在运行（可能已有 harness），跳过"
} else {
    if ($Seed -ne 0) { $seedCmd = "`$env:GAZE_SEED='$Seed'; " } else { $seedCmd = "Remove-Item Env:GAZE_SEED -ErrorAction SilentlyContinue; " }
    $camCmd = "`$env:GAZE_CAM_BACKEND='$Backend'; `$env:GAZE_CAM_FOURCC='$Fourcc'; `$env:GAZE_FPS_CAP='$FpsCap'; `$env:GAZE_UI_EVERY='$UiEvery'; "
    Start-Window "digit-human · harness" `
        "Set-Location '$root'; ${seedCmd}${camCmd}& '$Py' -u test_harness.py$camArg 2>&1 | Tee-Object -FilePath '$log'"
}

# 4) 等桥起来再开浏览器
if (-not $DryRun) {
    $t0 = Get-Date
    while (-not (Test-Port 9000) -and ((Get-Date) - $t0).TotalSeconds -lt 6) { Start-Sleep -Milliseconds 200 }
    if (-not (Test-Port 9000)) { Write-Host "  [警告] 9000 还没起来，页面可能显示未连接" }
}

# 5) 打开页面（带时间戳绕过缓存）
$url = "http://localhost:$Port/index.html?v=$(Get-Date -Format HHmmss)"
if ($NoBrowser) {
    Write-Host ("  手动打开: {0}" -f $url)
} else {
    Write-Host ("  打开页面: {0}" -f $url)
    if (-not $DryRun) { Start-Process $url }
}

# 6) 健康检查：等 harness 起来，回读 [cam] 行
if (-not $DryRun) {
    $t0 = Get-Date
    while (-not (Test-Port 5557) -and ((Get-Date) - $t0).TotalSeconds -lt 10) { Start-Sleep -Milliseconds 200 }
    if (Test-Port 5557) {
        Start-Sleep -Milliseconds 800
        Write-Host ""
        Write-Host "  链路状态："
        if (Test-Path $log) {
            $cam = Select-String -Path $log -Pattern "^\[cam\]" | Select-Object -Last 5
            if ($cam) { $cam | ForEach-Object { Write-Host ("    " + $_.Line.Trim()) } }
            else { Write-Host "    [warn] 日志里还没有 [cam] 行，相机可能没起来" }
        }
        $ok9000 = Test-Port 9000
        Write-Host ("    WS 桥 9000 : {0}" -f $(if ($ok9000) { "OK" } else { "未监听！" }))
        Write-Host ("    静态页 {0} : {1}" -f $Port, $(if (Test-Port $Port) { "OK" } else { "未监听！" }))
    } else {
        Write-Host "`n  [warn] 5557 未监听，harness 可能启动失败（看它的窗口）"
    }
}
Write-Host ""
Write-Host "提示："
Write-Host "  · harness 窗口按 Q 退出；输出同时写入 run_console.log"
Write-Host "  · 停止全部：.\tools\start_all.ps1 -Stop"
Write-Host "  · 页面不刷新时加 ?v=新数字，或 Ctrl+Shift+R"

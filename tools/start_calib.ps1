# 一键启动标定环境（方案 A：只跑标定桥）
#
#   .\tools\start_calib.ps1              # 默认 sim 后端，自动清端口 + 起桥 + 开页面
#   .\tools\start_calib.ps1 -Backend real
#   .\tools\start_calib.ps1 -DryRun      # 只看会做什么，不动进程
#
# 它会占用 HTTP 9100（页面）和 WS 9000（数字人数据 + 标定通道），
# 因此不需要再跑 ws_bridge，也不需要另起静态服务器。
param(
    [ValidateSet("both", "solo")][string]$Mode = "both",
    [ValidateSet("sim", "real")][string]$Backend = "sim",
    [int]$HttpPort = 9100,
    [int]$WsPort = 9000,
    [switch]$NoOpen,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
# 解释器：优先 $env:GAZE_PYTHON，否则用 PATH 里的 python
$Py = if ($env:GAZE_PYTHON) { $env:GAZE_PYTHON } else { "python" }

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"

function Get-ListenPids([int]$port) {
    $found = @()
    try {
        $found = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop |
                   Select-Object -ExpandProperty OwningProcess |
                   Where-Object { $_ -and "$_" -ne "0" })
    } catch { }
    if ($found.Count -eq 0) {
        # 兜底：Get-NetTCPConnection 在受限环境可能返回空对象
        $found = @(netstat -ano |
                   Select-String ":$port\s+.*LISTENING" |
                   ForEach-Object { ($_.Line.Trim() -split '\s+')[-1] } |
                   Where-Object { $_ -and "$_" -ne "0" })
    }
    return $found | Sort-Object -Unique
}


function Clear-Port([int]$port) {
    foreach ($procId in Get-ListenPids $port) {
        $name = "?"
        try { $name = (Get-Process -Id $procId -ErrorAction Stop).ProcessName } catch { }
        if ($DryRun) {
            Write-Host "  [dry] 会结束 PID=$procId ($name) 占用 $port"
            continue
        }
        Write-Host "  结束 PID=$procId ($name) 占用 $port"
        $ok = $false
        try { Stop-Process -Id $procId -Force -ErrorAction Stop; $ok = $true } catch { }
        if (-not $ok) {
            & taskkill /PID $procId /F 2>&1 | Out-Null
            $ok = ($LASTEXITCODE -eq 0)
        }
        if (-not $ok) { Write-Host "  ⚠ 结束失败（权限不足）：请手动 taskkill /PID $procId /F" }
    }
}

if ($Mode -eq "both") {
    # 方案 B：9000 留给 ws_bridge（真人实时），标定桥用 9101 叠加
    $WsPort = 9101
    Write-Host "== 1/3 清理端口 $WsPort（保留 9000 给 ws_bridge）=="
    Clear-Port $WsPort
} else {
    Write-Host "== 1/3 清理端口 $WsPort / $HttpPort（solo：标定桥独占）=="
    Clear-Port $WsPort
    Clear-Port $HttpPort
}
if (-not $DryRun) { Start-Sleep -Milliseconds 500 }

$left = @(Get-ListenPids $WsPort) + @(Get-ListenPids $HttpPort)
if ($left.Count -gt 0 -and -not $DryRun) {
    Write-Host "⚠ 端口仍被占用，桥会自动后移，页面可能连错服务。"
    Write-Host "  请手动结束：taskkill /PID <pid> /F"
}

Write-Host "== 2/3 启动标定桥（$Backend / $Mode）=="
Write-Host "  标定页 http://127.0.0.1:$HttpPort/calib.html"
Write-Host "  数字人 http://127.0.0.1:$HttpPort/index.html"
if ($Mode -eq "both") {
    Write-Host "  9000 仍归 ws_bridge（数字人跟真人）；标定命令走 $WsPort 叠加"
} else {
    Write-Host "  solo：数字人只由标定驱动，需要 ws_bridge 时改用 -Mode both"
}
Write-Host "  按 Ctrl+C 停止"
Write-Host ""

if ($DryRun) {
    Write-Host "[dry] 之后会执行: $Py -m tools.calib_bridge --backend $Backend --http $HttpPort --ws $WsPort"
    exit 0
}

if (-not $NoOpen) {
    # 等桥起来再开页面
    Start-Job -ScriptBlock {
        param($u1, $u2)
        Start-Sleep -Seconds 3
        try { Start-Process $u1 } catch {}
        try { Start-Process $u2 } catch {}
    } -ArgumentList "http://127.0.0.1:$HttpPort/index.html",
                   "http://127.0.0.1:$HttpPort/calib.html" | Out-Null
}

Write-Host "== 3/3 运行中 =="
& $Py -m tools.calib_bridge --backend $Backend --http $HttpPort --ws $WsPort

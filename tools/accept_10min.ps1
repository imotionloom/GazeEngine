# 10 分钟连跑（soak test）
#   .\tools\accept_10min.ps1              # NoCam 自动跑（无人场景）
#   .\tools\accept_10min.ps1 -WithCam     # 相机模式（需人配合）
#   .\tools\accept_10min.ps1 -Minutes 5   # 缩短时长调试
#
# 流程：停旧链路 → 起链路 → 等 N 分钟 → 停 → 分析 CSV → PASS/FAIL

param(
    [switch]$WithCam,
    [int]$Minutes = 10,
    [int]$Seed = 12345
)

$ErrorActionPreference = "Continue"
# 解释器：优先 $env:GAZE_PYTHON，否则用 PATH 里的 python
$Py = if ($env:GAZE_PYTHON) { $env:GAZE_PYTHON } else { "python" }

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

Write-Host "== 10 分钟验收 =="
Write-Host "  时长: ${Minutes}min  种子: $Seed  相机: $(if ($WithCam) {'ON'} else {'OFF'})"
Write-Host ""

# 1. 停旧链路
Write-Host "[1/5] 停止旧链路..."
& "$PSScriptRoot\start_all.ps1" -Stop | Out-Null
Start-Sleep -Seconds 2

# 2. 备份旧 CSV
$csv = Join-Path $root "runtime_log.csv"
if (Test-Path $csv) {
    $bak = "$csv.bak"
    Move-Item $csv $bak -Force
    Write-Host "  旧 CSV 备份到 runtime_log.csv.bak"
}

# 3. 起链路（不自动开浏览器）
Write-Host "[2/5] 启动链路..."
$camArg = if ($WithCam) { @() } else { @("-NoCam") }
& "$PSScriptRoot\start_all.ps1" -Seed $Seed -NoBrowser @camArg | Out-Null

# 4. 等待 harness 就绪
Write-Host "[3/5] 等待 harness 就绪..."
$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $c = New-Object Net.Sockets.TcpClient
        if ($c.ConnectAsync("127.0.0.1", 5557).Wait(300)) { $c.Close(); $ready = $true; break }
        $c.Close()
    } catch {}
}
if (-not $ready) {
    Write-Host "FAIL: harness 5557 未在 20s 内就绪"
    exit 1
}
Write-Host "  harness 就绪"

# 5. 计时
$totalSec = $Minutes * 60
Write-Host "[4/5] 连跑 ${Minutes} 分钟..."
$t0 = Get-Date
while (((Get-Date) - $t0).TotalSeconds -lt $totalSec) {
    $elapsed = ((Get-Date) - $t0).TotalSeconds
    $remain = $totalSec - $elapsed
    Write-Host ("`r  剩余 {0:N0}s   " -f $remain) -NoNewline
    Start-Sleep -Seconds 5
}
Write-Host ""

# 6. 停链路
Write-Host "[5/5] 停止链路..."
& "$PSScriptRoot\start_all.ps1" -Stop | Out-Null
Start-Sleep -Seconds 2

# 7. 分析
Write-Host ""
& $Py "$PSScriptRoot\accept_analyze.py" --csv $csv
exit $LASTEXITCODE

# 随机审计一键脚本（Windows / PowerShell）
#
#   .\run.ps1                                  # 10x60s 跨启动 + 2 次同 seed + 600s 长测
#   .\run.ps1 -Cross 6 -LongDur 300
#   .\run.ps1 -Baseline                        # 只跑理想随机基线
#   .\run.ps1 -Config .\my_th.json             # 自定义阈值
#
# 关键：每次采集都是**新进程**。同一进程内模块级 RNG 是连续的，
# 那样测到的不是"跨启动"，M13 会假阴性。
param(
    [int]$Cross = 10,
    [double]$CrossDur = 60,
    [double]$LongDur = 600,
    [int]$SeedA = 12345,
    [string]$Config = "",
    [switch]$Baseline,
    [switch]$NoLong
)

$ErrorActionPreference = "Stop"
# 解释器：优先 $env:GAZE_PYTHON，否则用 PATH 里的 python
$Py = if ($env:GAZE_PYTHON) { $env:GAZE_PYTHON } else { "python" }

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"

$out = Join-Path $root "random_audit\out"
New-Item -ItemType Directory -Force -Path $out | Out-Null
Get-ChildItem $out -Filter *.jsonl -ErrorAction SilentlyContinue | Remove-Item -Force

$cfgArgs = @()
if ($Config) { $cfgArgs = @("--config", (Resolve-Path $Config).Path) }

function Run-Collect([string[]]$extra) {
    & $Py -m random_audit.collector @extra
    if ($LASTEXITCODE -ne 0) { throw "collector 失败: $($extra -join ' ')" }
}

if ($Baseline) {
    Write-Host "== 理想随机基线 =="
    & $Py -m random_audit.baseline --runs 6 --duration $CrossDur
    exit 0
}

Write-Host "== 1/4 同 seed 两次（M13 应 ≈1.0）=="
Run-Collect @("--duration", "$CrossDur", "--scenario", "idle", "--seed", "$SeedA", "--out", "$out\same_a.jsonl")
Run-Collect @("--duration", "$CrossDur", "--scenario", "idle", "--seed", "$SeedA", "--out", "$out\same_b.jsonl")

Write-Host "== 2/4 跨启动 $Cross 次（不指定 seed = OS 熵）=="
for ($i = 1; $i -le $Cross; $i++) {
    Run-Collect @("--duration", "$CrossDur", "--scenario", "idle", "--out", "$out\cross_$i.jsonl")
}

if (-not $NoLong) {
    Write-Host "== 3/4 长测 $LongDur s（混合场景，覆盖社交族）=="
    Run-Collect @("--duration", "$LongDur", "--scenario", "mixed", "--out", "$out\long.jsonl")
}

Write-Host "== 分析 =="
$files = @(Get-ChildItem $out -Filter "cross_*.jsonl" | ForEach-Object { $_.FullName })
& $Py -m random_audit.report @files @cfgArgs "--pair" "$out\same_a.jsonl" "$out\same_b.jsonl"
$code = $LASTEXITCODE

if (-not $NoLong) {
    & $Py -m random_audit.report "$out\long.jsonl" @cfgArgs
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }
}

exit $code

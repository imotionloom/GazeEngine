# 随机审计入口（与 tools/ 里其它脚本同风格）
#
#   .\tools\run_audit.ps1                       # 全量：10x60s + 同seed + 600s
#   .\tools\run_audit.ps1 -Cross 3 -CrossDur 30 -NoLong
#   .\tools\run_audit.ps1 -Baseline             # 只出理想随机基线
#   .\tools\run_audit.ps1 -Config .\my_th.json  # 自定义阈值
#
# 退出码：0 = 全部指标通过；1 = 有指标未通过（可直接当 CI 门禁）。
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
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$runner = Join-Path $root "random_audit\run.ps1"

if (-not (Test-Path $runner)) {
    Write-Host "找不到 $runner" -ForegroundColor Red
    exit 2
}

$argv = @("-Cross", "$Cross", "-CrossDur", "$CrossDur", "-LongDur", "$LongDur", "-SeedA", "$SeedA")
if ($Baseline) { $argv += "-Baseline" }
if ($NoLong) { $argv += "-NoLong" }

if ($Config) { $argv += @("-Config", (Resolve-Path $Config).Path) }

& powershell -NoProfile -ExecutionPolicy Bypass -File $runner @argv
exit $LASTEXITCODE

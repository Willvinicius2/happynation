# Sobe apenas o bot de trade (src.main) e o dashboard (src.dashboard)
# em background, sem o stack do Aether.
#
# Uso:
#   .\scripts\run_happynation_bg.ps1
#   .\scripts\run_happynation_bg.ps1 -Restart          # mata processos antigos antes
#   .\scripts\run_happynation_bg.ps1 -AggressivePaper  # coleta amostras com travas relaxadas

param(
    [switch]$Restart,
    [switch]$AggressivePaper
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logsDir = Join-Path $root "logs"
$pidFile = Join-Path $logsDir "happynation.pids"

if (-not (Test-Path $python)) {
    Write-Host "python.exe nao encontrado em $python" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }

$env:TRADE_DASHBOARD_ALLOW_PORT_FALLBACK = if ($env:TRADE_DASHBOARD_ALLOW_PORT_FALLBACK) { $env:TRADE_DASHBOARD_ALLOW_PORT_FALLBACK } else { "1" }

if ($AggressivePaper) {
    # Modo paper agressivo: usado somente quando queremos gerar amostras rapido.
    # Mantem o bot em paper, mas relaxa travas que normalmente impedem reentrada.
    $env:RISK_GUARD_ENABLED = "0"
    $env:MAX_DAILY_LOSS_PCT = "100"
    $env:MAX_ASSET_DRAWDOWN_PCT = "100"
    $env:MAX_GLOBAL_DRAWDOWN_PCT = "100"
    $env:MAX_OPEN_POSITIONS = "12"
    $env:MAX_CONSECUTIVE_LOSSES = "999"
    $env:PAPER_AUTO_JUDGE_ENABLED = "0"
    $env:PAPER_JUDGE_MAX_LOSS_STREAK = "999"
    $env:PAPER_JUDGE_MIN_WINRATE_PCT = "0"
    $env:PAPER_JUDGE_MAX_DRAWDOWN_USD = "9999"
    $env:AETHER_SKIP_DEAD_STRATEGIES = "0"
    $env:AETHER_ONLY_MIN_BUY_VOTES = "1"
    $env:AETHER_ONLY_MIN_BUY_WEIGHT = "1"
    $env:AETHER_ONLY_MIN_SELL_VOTES = "1"
    $env:AETHER_ONLY_MIN_SELL_WEIGHT = "1"
    $env:PAPER_MAX_POSITION_USD = "100"
    $env:PAPER_MAX_POSITION_PCT = "10"
    $env:PAPER_MAX_TOTAL_ALLOCATED_PCT = "100"
    $env:PAPER_MIN_CASH_RESERVE_USD = "0"
} else {
    # Modo padrao: paper protegido. O historico de entradas mostrou que voto
    # isolado + judge desligado deixava estrategias perdedoras reentrarem.
    $env:RISK_GUARD_ENABLED = "1"
    $env:MAX_DAILY_LOSS_PCT = "3.0"
    $env:MAX_ASSET_DRAWDOWN_PCT = "4.0"
    $env:MAX_GLOBAL_DRAWDOWN_PCT = "5.0"
    $env:MAX_OPEN_POSITIONS = "4"
    $env:MAX_CONSECUTIVE_LOSSES = "2"
    $env:PAPER_AUTO_JUDGE_ENABLED = "1"
    $env:PAPER_JUDGE_MIN_TRADES = "2"
    $env:PAPER_JUDGE_MAX_LOSS_STREAK = "2"
    $env:PAPER_JUDGE_MIN_WINRATE_PCT = "35"
    $env:PAPER_JUDGE_MAX_DRAWDOWN_USD = "8"
    $env:AETHER_SKIP_DEAD_STRATEGIES = "1"
    $env:AETHER_ONLY_MIN_BUY_VOTES = "2"
    $env:AETHER_ONLY_MIN_BUY_WEIGHT = "2"
    $env:AETHER_ONLY_MIN_SELL_VOTES = "1"
    $env:AETHER_ONLY_MIN_SELL_WEIGHT = "1"
    $env:PAPER_MAX_POSITION_USD = "100"
    $env:PAPER_MAX_POSITION_PCT = "10"
    $env:PAPER_MAX_TOTAL_ALLOCATED_PCT = "60"
    $env:PAPER_MIN_CASH_RESERVE_USD = "100"
}

$modules = @(
    @{ name = "bot";       module = "src.main" },
    @{ name = "dashboard"; module = "src.dashboard" }
)

function Normalize-StartProcessEnvironment {
    $processPath = [System.Environment]::GetEnvironmentVariable("Path", "Process")
    $processPATH = [System.Environment]::GetEnvironmentVariable("PATH", "Process")
    $pathValue = if ($processPath) { $processPath } else { $processPATH }
    [System.Environment]::SetEnvironmentVariable("PATH", $null, "Process")
    if ($pathValue) {
        [System.Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")
    }
}

function Get-ExistingModuleProcesses {
    param([array]$Entries)
    $pythonProcs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue)
    $found = @()
    foreach ($entry in $Entries) {
        $pattern = "-m\s+$([regex]::Escape($entry.module))(\s|$)"
        foreach ($p in $pythonProcs) {
            $cmd = $p.CommandLine
            if ($cmd -and $cmd -match $pattern) {
                $found += [pscustomobject]@{
                    Pid = [int]$p.ProcessId
                    Name = $entry.name
                    Module = $entry.module
                }
            }
        }
    }
    return $found | Sort-Object Pid -Unique
}

$existing = @(Get-ExistingModuleProcesses -Entries $modules)
if ($existing.Count -gt 0) {
    if (-not $Restart) {
        Write-Host "Ja existe bot/dashboard rodando:" -ForegroundColor Yellow
        $existing | Select-Object Pid,Name,Module | Format-Table -AutoSize
        Write-Host "Use -Restart para reiniciar." -ForegroundColor DarkGray
        exit 1
    }
    foreach ($p in $existing) {
        try {
            Stop-Process -Id $p.Pid -Force -ErrorAction Stop
            Write-Host ("[restart] stop pid={0} ({1})" -f $p.Pid, $p.Name) -ForegroundColor Yellow
        } catch {}
    }
    Start-Sleep -Seconds 1
}

if (Test-Path $pidFile) { Remove-Item $pidFile -Force }

$started = @()
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
foreach ($entry in $modules) {
    $name = $entry.name
    $module = $entry.module
    $logPath = Join-Path $logsDir ("{0}-{1}.log" -f $name, $stamp)
    $errPath = Join-Path $logsDir ("{0}-{1}.err.log" -f $name, $stamp)
    Set-Content -Path $logPath -Value ("[start] {0} {1}" -f (Get-Date -Format o), $module) -Encoding utf8
    Set-Content -Path $errPath -Value "" -Encoding utf8

    Normalize-StartProcessEnvironment
    $proc = Start-Process -FilePath $python `
        -ArgumentList @("-u", "-m", $module) `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $logPath `
        -RedirectStandardError $errPath `
        -PassThru
    Add-Content -Path $pidFile -Value ("{0}`t{1}`t{2}" -f $proc.Id, $name, $module)
    $started += [pscustomobject]@{ Name=$name; Pid=$proc.Id; Log=$logPath }
    Write-Host ("[OK] {0,-10} pid={1,-6} log={2}" -f $name, $proc.Id, $logPath) -ForegroundColor Green
}

Write-Host ""
Write-Host "Bot + dashboard rodando. Trade dashboard: http://127.0.0.1:8000" -ForegroundColor Cyan
Write-Host "Parar tudo:  .\scripts\stop_happynation.ps1" -ForegroundColor DarkGray
Write-Host ""
$started | Format-Table -AutoSize

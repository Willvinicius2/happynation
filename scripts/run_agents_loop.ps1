# Spawn hidden do loop dos agentes Tier 3 (DriftAgent, MarketRegimeAgent, etc).
# Executa src.agent_runner a cada AETHER_AGENTS_INTERVAL_SEC (default 300s)
# e grava src/logs/agents/latest_report.json a cada tick.
#
# Uso:
#   .\scripts\run_agents_loop.ps1              # 5 min, hidden
#   .\scripts\run_agents_loop.ps1 -Visible     # janela visivel pra debug
#   .\scripts\run_agents_loop.ps1 -Restart     # encerra loop existente e sobe outro
#
# Stop:
#   Stop-Process -Id (Get-Content src\logs\agents\loop.pid)

param(
    [switch]$Visible,
    [switch]$Restart,
    [int]$IntervalSec = 0
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$pidFile = Join-Path $root "src\logs\agents\loop.pid"
New-Item -ItemType Directory -Force -Path (Split-Path $pidFile) | Out-Null
if (-not (Test-Path $python)) { $python = "python" }

if ($IntervalSec -gt 0) { $env:AETHER_AGENTS_INTERVAL_SEC = "$IntervalSec" }

function Get-AgentsLoopProcesses {
    $found = @()
    if (Test-Path $pidFile) {
        try {
            $pidFromFile = [int](Get-Content $pidFile -ErrorAction Stop | Select-Object -First 1)
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$pidFromFile" -ErrorAction SilentlyContinue
            if ($proc -and $proc.CommandLine -match "src\.agent_runner_loop") {
                $found += $proc
            }
        } catch {}
    }

    $pythonProcs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue)
    foreach ($p in $pythonProcs) {
        $cmd = $p.CommandLine
        if ($cmd -and $cmd -match "-m\s+src\.agent_runner_loop(\s|$)") {
            $found += $p
        }
    }
    return $found | Sort-Object ProcessId -Unique
}

$existing = @(Get-AgentsLoopProcesses)
if ($existing.Count -gt 0) {
    if (-not $Restart) {
        Write-Host "Ja existe loop dos agentes rodando:" -ForegroundColor Yellow
        $existing | Select-Object @{Name="Pid";Expression={$_.ProcessId}},CommandLine | Format-Table -AutoSize
        Write-Host "Use .\scripts\run_agents_loop.ps1 -Restart para reiniciar." -ForegroundColor DarkGray
        exit 1
    }
    foreach ($p in $existing) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Write-Host ("[restart] stop agents loop pid={0}" -f $p.ProcessId) -ForegroundColor Yellow
        } catch {}
    }
    Start-Sleep -Seconds 1
}

$style = if ($Visible) { "Normal" } else { "Hidden" }
$proc = Start-Process $python `
    -WindowStyle $style `
    -ArgumentList "-u", "-m", "src.agent_runner_loop" `
    -WorkingDirectory $root `
    -PassThru
$proc.Id | Out-File -FilePath $pidFile -Encoding ascii
$intervalShown = if ($env:AETHER_AGENTS_INTERVAL_SEC) { $env:AETHER_AGENTS_INTERVAL_SEC } else { '300' }
Write-Host "[OK] agents loop -> PID $($proc.Id) (style=$style, interval=${intervalShown}s)" -ForegroundColor Green
Write-Host "Log:  src\logs\agents\runner_loop.log" -ForegroundColor DarkGray
Write-Host "Stop: Stop-Process -Id $($proc.Id)" -ForegroundColor DarkGray

# Esteira industrial Aether: sobe SO o throttle watchdog (nao toca nos
# processos Aether ja rodando). Para ativar throughput maior, use as
# env vars no shell antes OU deixe o watchdog ajustar dinamicamente.
#
# Uso:
#   .\scripts\start_aether_industrial.ps1
#
# Knobs (env vars opcionais antes de subir o stack Aether):
#   AETHER_TASKS_PER_WORKER=4         (default 2; watchdog ajusta entre 1..AETHER_WD_TASKS_MAX)
#   AETHER_TARGET_PROBES_PER_WORKER=10 (default 6)
#   AETHER_LIMIT_NODES_CAP=50         (default 25)
#   AETHER_LLM_MAX_NEW=5              (default 3, cap interno 5)
#   AETHER_VALIDATOR_POLL_SECONDS=30  (default 60)
#   AETHER_CRITIC_BT_WORKERS=6        (default 2)
#   AETHER_WD_TASKS_MAX=6             (teto que watchdog respeita)
#   AETHER_WD_DRY_RUN=1               (so loga, nao mexe)
#
# IMPORTANTE: nao mata nenhum processo. Para aplicar env vars novas
# nos procs Aether existentes, eh preciso parar+resubir manualmente
# com stop_aether_llm.ps1 / start_aether_llm.ps1 (mas o watchdog ja
# faz throttle dinamico mesmo sem reiniciar).

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logsDir = Join-Path $root "src\logs\aether"
$pidFile = Join-Path $root "logs\aether_industrial.pids"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path $pidFile) -Force | Out-Null

# Verifica se watchdog ja esta rodando
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'aether_throttle_watchdog'
}
if ($existing) {
    Write-Host "[OK] watchdog ja esta rodando (pid=$($existing.ProcessId)); nada a fazer."
    exit 0
}

$args = @("-u", (Join-Path $root "scripts\aether_throttle_watchdog.py"))
$p = Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput "$logsDir\throttle_watchdog.out.log" `
    -RedirectStandardError  "$logsDir\throttle_watchdog.err.log"
"$($p.Id)`twatchdog" | Set-Content $pidFile -Encoding UTF8
Write-Host "[watchdog] pid=$($p.Id) | log: $logsDir\throttle_watchdog.log"
Write-Host ""
Write-Host "Para parar: Stop-Process -Id $($p.Id)"
Write-Host "Para ver acoes: Get-Content '$logsDir\throttle_watchdog.log' -Wait -Tail 20"

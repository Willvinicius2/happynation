# Roda o stack do Aether SEM abrir janelas/CMDs externos.
# Cada modulo vira um processo Python hidden, com log em logs/<nome>.log.
# Os PIDs ficam em logs/aether.pids para o stop_aether.ps1 conseguir encerrar.
#
# Uso (no terminal integrado do VSCode):
#   .\scripts\run_aether_bg.ps1            # minimo (server + validator)
#   .\scripts\run_aether_bg.ps1 -Full      # stack completo (critic + hyper + deploy)
#   .\scripts\run_aether_bg.ps1 -Stack     # idem run_aether_stack.ps1 (sem economy)
#   .\scripts\run_aether_bg.ps1 -Bot       # adiciona o bot de trade (src.main)
#   .\scripts\run_aether_bg.ps1 -Dashboard # adiciona o trade monitor (src.dashboard)
#   .\scripts\run_aether_bg.ps1 -Restart   # encerra modulos iguais antes de subir
#
# Para parar:  .\scripts\stop_aether.ps1
# Para acompanhar logs:  .\scripts\tail_aether.ps1 server
#                        .\scripts\tail_aether.ps1 -All

param(
    [switch]$Full,
    [switch]$Stack,
    [switch]$Bot,
    [switch]$Dashboard,
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logsDir = Join-Path $root "logs"
$pidFile = Join-Path $logsDir "aether.pids"

if (-not (Test-Path $python)) {
    Write-Host "python.exe nao encontrado em $python" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir | Out-Null
}

function Import-DotEnvIfPresent {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    foreach ($line in Get-Content -Path $Path -ErrorAction SilentlyContinue) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) { continue }
        $parts = $trimmed.Split("=", 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        if (-not $key) { continue }
        if (-not [Environment]::GetEnvironmentVariable($key, "Process")) {
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
}

# Carrega .env local + credenciais globais do Hermes sem sobrescrever variaveis ja
# herdadas do start_aether.bat/terminal. Isso evita o Aether subir com LLM
# "desconfigurado" quando as chaves existem apenas no perfil Hermes.
Import-DotEnvIfPresent (Join-Path $root ".env")
Import-DotEnvIfPresent (Join-Path $env:LOCALAPPDATA "hermes\.env")

# Variaveis herdadas
if (-not $env:AETHER_ECONOMY_MODE)                         { $env:AETHER_ECONOMY_MODE = if ($Stack) { "0" } else { "1" } }
if (-not $env:AETHER_LLM_MAX_TOKENS)                       { $env:AETHER_LLM_MAX_TOKENS = if ($Stack) { "700" } else { "400" } }
if (-not $env:AETHER_LLM_MAX_NEW)                          { $env:AETHER_LLM_MAX_NEW = if ($Stack) { "6" } else { "3" } }
# Tier A: defaults sobem pra 4000 mas guards respeitam env ja seteado pelo stack wrapper.
if (-not $env:AETHER_HYPOTHESIS_MAX_TOKENS)                { $env:AETHER_HYPOTHESIS_MAX_TOKENS = if ($Stack) { "4000" } else { "1100" } }
if (-not $env:AETHER_ROLE_HYPOTHESIS_GENERATOR_MAX_TOKENS) { $env:AETHER_ROLE_HYPOTHESIS_GENERATOR_MAX_TOKENS = if ($Stack) { "4000" } else { "1100" } }
$env:AETHER_ADAPTIVE_TASK_BATCH = "1"
$env:AETHER_TASKS_PER_WORKER = "2"
$env:AETHER_TARGET_PROBES_PER_WORKER = "6"
$env:AETHER_SCOUT_CANDIDATE_SCAN_LIMIT = "120"
if (-not $env:AETHER_UI_STATE_MAX_NODES) { $env:AETHER_UI_STATE_MAX_NODES = "850" }
if (-not $env:AETHER_STATE_COMPACT_METRICS) { $env:AETHER_STATE_COMPACT_METRICS = "1" }
if (-not $env:AETHER_PRIMARY_LLM_PROVIDER) { $env:AETHER_PRIMARY_LLM_PROVIDER = "openai" }
if (-not $env:AETHER_OPENAI_TRANSPORT) { $env:AETHER_OPENAI_TRANSPORT = "codex_cli" }
if (-not $env:AETHER_OPENAI_MODEL) { $env:AETHER_OPENAI_MODEL = "gpt-5.4-mini" }
if (-not $env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_MODEL) { $env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_MODEL = "gpt-5.5" }
if (-not $env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_EFFORT) { $env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_EFFORT = "high" }
if (-not $env:AETHER_CODEX_CLI_TIMEOUT) { $env:AETHER_CODEX_CLI_TIMEOUT = "300" }
if (-not $env:AETHER_CONTEXT_BROKER) { $env:AETHER_CONTEXT_BROKER = "0" }
$env:AETHER_DEPLOY_CRITIC_GATE = if ($env:AETHER_DEPLOY_CRITIC_GATE) { $env:AETHER_DEPLOY_CRITIC_GATE } else { "approved" }
$env:AETHER_DEPLOY_MIN_TRADES = if ($env:AETHER_DEPLOY_MIN_TRADES) { $env:AETHER_DEPLOY_MIN_TRADES } else { "12" }
$env:AETHER_DEPLOY_REQUIRE_COMBO_FOR_MULTI = if ($env:AETHER_DEPLOY_REQUIRE_COMBO_FOR_MULTI) { $env:AETHER_DEPLOY_REQUIRE_COMBO_FOR_MULTI } else { "1" }
# Caps de diversidade afrouxados 2026-05-12 (pedido do usuario: mais profundidade
# de ensemble). Antes: family=30 asset=12 asset_family_tf=5 hyp_asset=2 top_n=80.
$env:AETHER_DEPLOY_MAX_PER_FAMILY = if ($env:AETHER_DEPLOY_MAX_PER_FAMILY) { $env:AETHER_DEPLOY_MAX_PER_FAMILY } else { "40" }
$env:AETHER_DEPLOY_MAX_PER_ASSET = if ($env:AETHER_DEPLOY_MAX_PER_ASSET) { $env:AETHER_DEPLOY_MAX_PER_ASSET } else { "18" }
$env:AETHER_DEPLOY_MAX_PER_ASSET_FAMILY_TF = if ($env:AETHER_DEPLOY_MAX_PER_ASSET_FAMILY_TF) { $env:AETHER_DEPLOY_MAX_PER_ASSET_FAMILY_TF } else { "8" }
$env:AETHER_DEPLOY_MAX_PER_HYPOTHESIS_ASSET = if ($env:AETHER_DEPLOY_MAX_PER_HYPOTHESIS_ASSET) { $env:AETHER_DEPLOY_MAX_PER_HYPOTHESIS_ASSET } else { "0" }
$env:AETHER_DEPLOY_ALLOWED_ASSETS = if ($env:AETHER_DEPLOY_ALLOWED_ASSETS) { $env:AETHER_DEPLOY_ALLOWED_ASSETS } else { "BTCUSDT,ETHUSDT,SOLUSDT,ADAUSDT,XRPUSDT,LINKUSDT,DOGEUSDT,AVAXUSDT,BNBUSDT" }
$env:AETHER_DEPLOY_ALLOWED_TIMEFRAMES = if ($env:AETHER_DEPLOY_ALLOWED_TIMEFRAMES) { $env:AETHER_DEPLOY_ALLOWED_TIMEFRAMES } else { "15m,1h,4h,1d" }
$env:AETHER_DEPLOY_TOP_N = if ($env:AETHER_DEPLOY_TOP_N) { $env:AETHER_DEPLOY_TOP_N } else { "250" }
$env:AETHER_STRATEGIES_ONLY = if ($env:AETHER_STRATEGIES_ONLY) { $env:AETHER_STRATEGIES_ONLY } else { "1" }
$env:AETHER_PAPER_READY_REQUIRE_CRITIC = if ($env:AETHER_PAPER_READY_REQUIRE_CRITIC) { $env:AETHER_PAPER_READY_REQUIRE_CRITIC } else { "1" }
$env:AETHER_PAPER_READY_MIN_TRADES = if ($env:AETHER_PAPER_READY_MIN_TRADES) { $env:AETHER_PAPER_READY_MIN_TRADES } else { "30" }
$env:AETHER_PAPER_READY_REQUIRE_COMBO_FOR_MULTI = if ($env:AETHER_PAPER_READY_REQUIRE_COMBO_FOR_MULTI) { $env:AETHER_PAPER_READY_REQUIRE_COMBO_FOR_MULTI } else { "1" }
$env:AETHER_VALIDATOR_MIN_TRADES = if ($env:AETHER_VALIDATOR_MIN_TRADES) { $env:AETHER_VALIDATOR_MIN_TRADES } else { "30" }
$env:AETHER_VALIDATOR_MAX_MDD_PCT = if ($env:AETHER_VALIDATOR_MAX_MDD_PCT) { $env:AETHER_VALIDATOR_MAX_MDD_PCT } else { "35" }
$env:AETHER_CRITIC_CROSS_POOL = if ($env:AETHER_CRITIC_CROSS_POOL) { $env:AETHER_CRITIC_CROSS_POOL } else { "3" }
$env:AETHER_CRITIC_CROSS_MIN_PASS = if ($env:AETHER_CRITIC_CROSS_MIN_PASS) { $env:AETHER_CRITIC_CROSS_MIN_PASS } else { "1" }
$env:AETHER_CRITIC_CROSS_MIN_PNL = if ($env:AETHER_CRITIC_CROSS_MIN_PNL) { $env:AETHER_CRITIC_CROSS_MIN_PNL } else { "-3.0" }
$env:AETHER_CRITIC_GRID_MIN_P25_PNL = if ($env:AETHER_CRITIC_GRID_MIN_P25_PNL) { $env:AETHER_CRITIC_GRID_MIN_P25_PNL } else { "-7.0" }
$env:AETHER_CRITIC_MTF_DISABLE = if ($env:AETHER_CRITIC_MTF_DISABLE) { $env:AETHER_CRITIC_MTF_DISABLE } else { "1" }
$env:AETHER_CRITIC_BOOTSTRAP_MIN_SHARPE_P5 = if ($env:AETHER_CRITIC_BOOTSTRAP_MIN_SHARPE_P5) { $env:AETHER_CRITIC_BOOTSTRAP_MIN_SHARPE_P5 } else { "-0.3" }
if (-not $env:AETHER_LLM_HEARTBEAT_EVERY) { $env:AETHER_LLM_HEARTBEAT_EVERY = if ($Stack) { "2" } else { "3" } }
if (-not $env:AETHER_FORCE_EXPLORATION_EVERY) { $env:AETHER_FORCE_EXPLORATION_EVERY = if ($Stack) { "2" } else { "4" } }
if (-not $env:AETHER_WEB_RESEARCH_HEARTBEAT_EVERY) { $env:AETHER_WEB_RESEARCH_HEARTBEAT_EVERY = "1" }
if (-not $env:AETHER_WEB_INFLIGHT_TOLERANCE) { $env:AETHER_WEB_INFLIGHT_TOLERANCE = "24" }
if (-not $env:AETHER_CONTINUOUS_RESEARCH_INTERVAL_SECONDS) { $env:AETHER_CONTINUOUS_RESEARCH_INTERVAL_SECONDS = if ($Stack) { "20" } else { "25" } }
if (-not $env:AETHER_LLM_INFLIGHT_TOLERANCE) { $env:AETHER_LLM_INFLIGHT_TOLERANCE = "32" }
if (-not $Stack) {
    $env:AETHER_OPENAI_ROLE_VALIDATION_AGENT_MODEL = if ($env:AETHER_OPENAI_ROLE_VALIDATION_AGENT_MODEL) { $env:AETHER_OPENAI_ROLE_VALIDATION_AGENT_MODEL } else { $env:AETHER_OPENAI_MODEL }
    $env:AETHER_OPENAI_ROLE_PORTFOLIO_AGENT_MODEL = if ($env:AETHER_OPENAI_ROLE_PORTFOLIO_AGENT_MODEL) { $env:AETHER_OPENAI_ROLE_PORTFOLIO_AGENT_MODEL } else { $env:AETHER_OPENAI_MODEL }
}

$modules = @(
    @{ name = "server";    module = "src.research.aether.server" },
    @{ name = "validator"; module = "src.research.aether.auto_validator" }
)
if ($Full -or $Stack) {
    $modules += @(
        @{ name = "critic"; module = "src.research.aether.critic_agent" },
        @{ name = "hyper";  module = "src.research.aether.hyper_agent" },
        @{ name = "deploy"; module = "src.research.aether.auto_deploy" }
    )
}
if ($Bot) {
    $modules += @{ name = "bot"; module = "src.main" }
}
if ($Dashboard) {
    $env:TRADE_DASHBOARD_ALLOW_PORT_FALLBACK = if ($env:TRADE_DASHBOARD_ALLOW_PORT_FALLBACK) { $env:TRADE_DASHBOARD_ALLOW_PORT_FALLBACK } else { "1" }
    $modules += @{ name = "dashboard"; module = "src.dashboard" }
}

$moduleNames = ($modules | ForEach-Object { $_.name }) -join ", "
$codexOnPath = [bool](Get-Command codex -ErrorAction SilentlyContinue)
$codexExplicit = [bool]($env:AETHER_CODEX_CLI_PATH -and (Test-Path -LiteralPath $env:AETHER_CODEX_CLI_PATH))
$codexInstalled = $codexOnPath -or $codexExplicit
$codexStatus = if ($codexExplicit) { "explicit" } elseif ($codexOnPath) { "path" } else { "missing" }
$hasOpenAiKey = [bool]($env:OPENAI_API_KEY -or $env:AETHER_OPENAI_API_KEY)
if ($env:AETHER_PRIMARY_LLM_PROVIDER -eq "openai" -and $env:AETHER_OPENAI_TRANSPORT -in @("codex_cli", "cli") -and -not $codexInstalled -and $hasOpenAiKey) {
    $env:AETHER_OPENAI_TRANSPORT = "api"
    Write-Host "OpenAI: Codex CLI ausente; usando API key carregada como transporte api." -ForegroundColor Yellow
}
$modeLabel = if ($Full -or $Stack) { "FULL/STACK" } elseif ($Bot -or $Dashboard) { "CUSTOM" } else { "MINIMUM" }
Write-Host ""
Write-Host ("Aether launcher mode: {0}" -f $modeLabel) -ForegroundColor Cyan
Write-Host ("Sidecars selecionados: {0}" -f $moduleNames) -ForegroundColor Cyan
foreach ($entry in $modules) {
    Write-Host ("  - {0} => {1}" -f $entry.name, $entry.module) -ForegroundColor DarkGray
}
Write-Host ("OpenAI: provider={0} transport={1} model={2} codex={3} api_key={4}" -f $env:AETHER_PRIMARY_LLM_PROVIDER, $env:AETHER_OPENAI_TRANSPORT, $env:AETHER_OPENAI_MODEL, $codexStatus, ($(if ($hasOpenAiKey) { "set" } else { "unset" }))) -ForegroundColor Cyan
if ($env:AETHER_PRIMARY_LLM_PROVIDER -eq "openai" -and -not ($codexInstalled -or $hasOpenAiKey)) {
    Write-Host "Aviso: OpenAI foi selecionado, mas Codex CLI e API key nao parecem configurados." -ForegroundColor Yellow
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
                    CommandLine = $cmd
                }
            }
        }
    }
    return $found | Sort-Object Pid -Unique
}

$existing = @(Get-ExistingModuleProcesses -Entries $modules)
if ($existing.Count -gt 0) {
    if (-not $Restart) {
        Write-Host "Ja existe processo Aether/bot para esta stack:" -ForegroundColor Yellow
        $existing | Select-Object Pid,Name,Module | Format-Table -AutoSize
        Write-Host "Use .\scripts\stop_aether.ps1 ou rode este script com -Restart." -ForegroundColor DarkGray
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

# Limpa pid file antigo
if (Test-Path $pidFile) { Remove-Item $pidFile -Force }

$started = @()
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
foreach ($entry in $modules) {
    $name = $entry.name
    $module = $entry.module
    $logPath = Join-Path $logsDir ("{0}-{1}.log" -f $name, $stamp)
    $errPath = Join-Path $logsDir ("{0}-{1}.err.log" -f $name, $stamp)
    # Logs com timestamp evitam erro quando um processo antigo ainda segura
    # o handle do arquivo por alguns segundos.
    Set-Content -Path $logPath -Value ("[start] {0} {1}" -f (Get-Date -Format o), $module) -Encoding utf8
    Set-Content -Path $errPath -Value "" -Encoding utf8

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
Write-Host "Stack rodando em background. Dashboard: http://127.0.0.1:8765" -ForegroundColor Cyan
Write-Host "Acompanhar log:  .\scripts\tail_aether.ps1 server" -ForegroundColor DarkGray
Write-Host "Parar tudo:      .\scripts\stop_aether.ps1" -ForegroundColor DarkGray
Write-Host ""
$started | Format-Table -AutoSize

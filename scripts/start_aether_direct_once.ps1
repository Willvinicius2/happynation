param(
    [switch]$Bot,
    [switch]$Dashboard
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logsDir = Join-Path $root "logs"
$pidFile = Join-Path $logsDir "aether.pids"
if (-not (Test-Path $python)) { throw "python.exe nao encontrado em $python" }
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }

function Import-DotEnvIfPresent {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    foreach ($line in Get-Content -Path $Path -ErrorAction SilentlyContinue) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) { continue }
        $parts = $trimmed.Split("=", 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        if ($key -and -not [Environment]::GetEnvironmentVariable($key, "Process")) {
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
}

Import-DotEnvIfPresent (Join-Path $root ".env")
Import-DotEnvIfPresent (Join-Path $env:LOCALAPPDATA "hermes\.env")

$env:AETHER_PRIMARY_LLM_PROVIDER = if ($env:AETHER_PRIMARY_LLM_PROVIDER) { $env:AETHER_PRIMARY_LLM_PROVIDER } else { "openai" }
$codexShim = Join-Path $env:APPDATA "npm\codex.cmd"
if (Test-Path $codexShim) {
    $env:AETHER_CODEX_CLI_PATH = $codexShim
    $env:AETHER_OPENAI_TRANSPORT = "codex_cli"
    $env:AETHER_PREFER_CODEX_CLI = "1"
} else {
    $env:AETHER_OPENAI_TRANSPORT = if ($env:AETHER_OPENAI_TRANSPORT) { $env:AETHER_OPENAI_TRANSPORT } else { "codex_cli" }
}
$env:AETHER_OPENAI_MODEL = if ($env:AETHER_OPENAI_MODEL) { $env:AETHER_OPENAI_MODEL } else { "gpt-5.4-mini" }
$env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_MODEL = if ($env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_MODEL) { $env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_MODEL } else { "gpt-5.4-mini" }
$env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_EFFORT = if ($env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_EFFORT) { $env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_EFFORT } else { "high" }
$env:AETHER_CODEX_CLI_TIMEOUT = if ($env:AETHER_CODEX_CLI_TIMEOUT) { $env:AETHER_CODEX_CLI_TIMEOUT } else { "300" }
$env:AETHER_CYCLE_AUTO_START = "0"
$env:AETHER_ECONOMY_MODE = if ($env:AETHER_ECONOMY_MODE) { $env:AETHER_ECONOMY_MODE } else { "0" }
$env:AETHER_DEPLOY_ALLOWED_ASSETS = if ($env:AETHER_DEPLOY_ALLOWED_ASSETS) { $env:AETHER_DEPLOY_ALLOWED_ASSETS } else { "BTCUSDT,ETHUSDT,SOLUSDT,ADAUSDT,XRPUSDT,LINKUSDT,DOGEUSDT,AVAXUSDT,BNBUSDT" }
$env:AETHER_DEPLOY_ALLOWED_TIMEFRAMES = if ($env:AETHER_DEPLOY_ALLOWED_TIMEFRAMES) { $env:AETHER_DEPLOY_ALLOWED_TIMEFRAMES } else { "15m,1h,4h,1d" }

$modules = @(
    @{ name = "server";    module = "src.research.aether.server" },
    @{ name = "validator"; module = "src.research.aether.auto_validator" },
    @{ name = "critic";    module = "src.research.aether.critic_agent" },
    @{ name = "hyper";     module = "src.research.aether.hyper_agent" },
    @{ name = "deploy";    module = "src.research.aether.auto_deploy" }
)
if ($Bot) { $modules += @{ name = "bot"; module = "src.main" } }
if ($Dashboard) {
    $env:TRADE_DASHBOARD_ALLOW_PORT_FALLBACK = if ($env:TRADE_DASHBOARD_ALLOW_PORT_FALLBACK) { $env:TRADE_DASHBOARD_ALLOW_PORT_FALLBACK } else { "1" }
    $modules += @{ name = "dashboard"; module = "src.dashboard" }
}

if (Test-Path $pidFile) { Remove-Item $pidFile -Force }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
foreach ($entry in $modules) {
    $name = $entry.name
    $module = $entry.module
    $logPath = Join-Path $logsDir ("{0}-{1}.log" -f $name, $stamp)
    $errPath = Join-Path $logsDir ("{0}-{1}.err.log" -f $name, $stamp)
    Set-Content -Path $logPath -Value ("[start] {0} {1}" -f (Get-Date -Format o), $module) -Encoding utf8
    Set-Content -Path $errPath -Value "" -Encoding utf8
    $proc = Start-Process -FilePath $python -ArgumentList @("-u", "-m", $module) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $logPath -RedirectStandardError $errPath -PassThru
    Add-Content -Path $pidFile -Value ("{0}`t{1}`t{2}" -f $proc.Id, $name, $module)
    Write-Host ("[OK] {0} pid={1}" -f $name, $proc.Id)
}
Write-Host "started"

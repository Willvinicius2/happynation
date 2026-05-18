# Sobe APENAS os 3 modulos do Aether que dependem de OpenAI/LLM:
#   server, auto_validator, hyper_agent
# Os modulos sem LLM (critic_agent, promote_loop) ja rodam 24/7
# junto com o bot principal.
#
# Uso:
#   .\scripts\start_aether_llm.ps1
#
# Para parar so esses 3:
#   .\scripts\stop_aether_llm.ps1

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logsDir = Join-Path $root "src\logs\aether"
$pidFile = Join-Path $root "logs\aether_llm.pids"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path $pidFile) -Force | Out-Null
"" | Set-Content $pidFile -Encoding UTF8

if (-not $env:AETHER_PRIMARY_LLM_PROVIDER) { $env:AETHER_PRIMARY_LLM_PROVIDER = "openai" }
if (-not $env:AETHER_OPENAI_TRANSPORT) { $env:AETHER_OPENAI_TRANSPORT = "codex_cli" }
if (-not $env:AETHER_OPENAI_MODEL) { $env:AETHER_OPENAI_MODEL = "gpt-5.4-mini" }
if (-not $env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_MODEL) { $env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_MODEL = "gpt-5.5" }
if (-not $env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_EFFORT) { $env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_EFFORT = "high" }
if (-not $env:AETHER_WEB_RESEARCH_HEARTBEAT_EVERY) { $env:AETHER_WEB_RESEARCH_HEARTBEAT_EVERY = "1" }
if (-not $env:AETHER_WEB_INFLIGHT_TOLERANCE) { $env:AETHER_WEB_INFLIGHT_TOLERANCE = "24" }
if (-not $env:AETHER_CODEX_CLI_TIMEOUT) { $env:AETHER_CODEX_CLI_TIMEOUT = "300" }
if (-not $env:AETHER_CONTEXT_BROKER) { $env:AETHER_CONTEXT_BROKER = "0" }

# Verifica que ja nao esta rodando
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'src\.research\.aether\.(server|auto_validator|hyper_agent)'
}
if ($running) {
    Write-Host "Ja existem $($running.Count) processos LLM rodando. Pare antes com stop_aether_llm.ps1"
    exit 1
}

function Spawn-Detached($name, [string[]]$argList, $log) {
    $p = Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput "$logsDir\$log.out.log" -RedirectStandardError "$logsDir\$log.err.log"
    "$($p.Id)`t$name" | Add-Content $pidFile -Encoding UTF8
    Write-Host "  [$name] pid=$($p.Id)"
}

$modulesToStart = @(
    @{ name = "server"; module = "src.research.aether.server" },
    @{ name = "auto_validator"; module = "src.research.aether.auto_validator" },
    @{ name = "hyper_agent"; module = "src.research.aether.hyper_agent" }
)
$codexInstalled = [bool](Get-Command codex -ErrorAction SilentlyContinue)
$hasOpenAiKey = [bool]($env:OPENAI_API_KEY -or $env:AETHER_OPENAI_API_KEY)

Write-Host "Subindo Aether LLM stack:" -ForegroundColor Cyan
foreach ($entry in $modulesToStart) {
    Write-Host ("  - {0} => {1}" -f $entry.name, $entry.module) -ForegroundColor DarkGray
}
Write-Host ("OpenAI: provider={0} transport={1} model={2} codex={3} api_key={4}" -f $env:AETHER_PRIMARY_LLM_PROVIDER, $env:AETHER_OPENAI_TRANSPORT, $env:AETHER_OPENAI_MODEL, ($(if ($codexInstalled) { "ok" } else { "missing" })), ($(if ($hasOpenAiKey) { "set" } else { "unset" }))) -ForegroundColor Cyan
if ($env:AETHER_PRIMARY_LLM_PROVIDER -eq "openai" -and -not ($codexInstalled -or $hasOpenAiKey)) {
    Write-Host "Aviso: OpenAI foi selecionado, mas Codex CLI e API key nao parecem configurados." -ForegroundColor Yellow
}

Spawn-Detached "server"         @("-u","-m","src.research.aether.server")          "server"
Spawn-Detached "auto_validator" @("-u","-m","src.research.aether.auto_validator")  "auto_validator"
Spawn-Detached "hyper_agent"    @("-u","-m","src.research.aether.hyper_agent")     "hyper_agent"

Write-Host "`nPIDs em $pidFile"
Write-Host "Logs em $logsDir"

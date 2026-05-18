# Versao economica do stack do Aether.
# Reduz consumo de tokens e numero de chats abertos por ciclo.
#
# Difs em relacao ao run_aether_stack.ps1:
#   - hypothesis/validation/portfolio rodam no modelo OpenAI configurado
#   - max_tokens por resposta cai 700 -> 400
#   - heartbeat LLM espacado: 2 -> 5 ciclos
#   - exploracao forcada espacada: 4 -> 8 ciclos
#   - drop sidecars opcionais (critic + hyper + deploy) p/ rodar so server + auto_validator
#     se quiser tudo, use -Full
#
# Uso:
#   .\scripts\run_aether_economy.ps1            # minimo (server + validator)
#   .\scripts\run_aether_economy.ps1 -Full      # stack completa, ainda em modo economia

param(
    [switch]$Full
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "python.exe nao encontrado em $python" -ForegroundColor Red
    exit 1
}

# Variaveis de economia herdadas pelos filhos
$envBlock = @'
$env:AETHER_ECONOMY_MODE = "1"
$env:AETHER_LLM_MAX_TOKENS = "400"
$env:AETHER_PRIMARY_LLM_PROVIDER = "openai"
$env:AETHER_OPENAI_TRANSPORT = if ($env:AETHER_OPENAI_TRANSPORT) { $env:AETHER_OPENAI_TRANSPORT } else { "codex_cli" }
$env:AETHER_OPENAI_MODEL = if ($env:AETHER_OPENAI_MODEL) { $env:AETHER_OPENAI_MODEL } else { "gpt-5.4-mini" }
$env:AETHER_CODEX_CLI_TIMEOUT = if ($env:AETHER_CODEX_CLI_TIMEOUT) { $env:AETHER_CODEX_CLI_TIMEOUT } else { "300" }
$env:AETHER_CONTEXT_BROKER = if ($env:AETHER_CONTEXT_BROKER) { $env:AETHER_CONTEXT_BROKER } else { "0" }
$env:AETHER_LLM_HEARTBEAT_EVERY = "5"
$env:AETHER_FORCE_EXPLORATION_EVERY = "8"
$env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_MODEL = if ($env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_MODEL) { $env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_MODEL } else { "gpt-5.5" }
$env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_EFFORT = if ($env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_EFFORT) { $env:AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_EFFORT } else { "high" }
$env:AETHER_OPENAI_ROLE_VALIDATION_AGENT_MODEL = $env:AETHER_OPENAI_MODEL
$env:AETHER_OPENAI_ROLE_PORTFOLIO_AGENT_MODEL  = $env:AETHER_OPENAI_MODEL
'@

$modules = @(
    @{ name = "aether-server";    module = "src.research.aether.server" },
    @{ name = "aether-validator"; module = "src.research.aether.auto_validator" }
)

if ($Full) {
    $modules += @(
        @{ name = "aether-critic"; module = "src.research.aether.critic_agent" },
        @{ name = "aether-hyper";  module = "src.research.aether.hyper_agent" },
        @{ name = "aether-deploy"; module = "src.research.aether.auto_deploy" }
    )
}

foreach ($entry in $modules) {
    $title = $entry.name
    $module = $entry.module
    $cmd = "$envBlock; `$Host.UI.RawUI.WindowTitle = '$title'; & '$python' -u -m $module"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd -WorkingDirectory $root | Out-Null
    Write-Host "[OK] $title -> $module" -ForegroundColor Green
}

Write-Host ""
Write-Host "Stack economica iniciada. Dashboard em http://127.0.0.1:8765" -ForegroundColor Cyan
Write-Host "Modo: $((@{$true='COMPLETO';$false='MINIMO (server + validator)'}[$Full.IsPresent]))" -ForegroundColor Yellow

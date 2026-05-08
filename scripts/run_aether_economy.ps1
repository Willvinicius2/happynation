# Versao economica do stack do Aether.
# Reduz consumo de tokens e numero de chats abertos por ciclo.
#
# Difs em relacao ao run_aether_stack.ps1:
#   - hypothesis/validation/portfolio rodam em Haiku (nao Sonnet)  ~5x mais barato
#   - reuso de sessao Claude Code aumentado de 20 -> 50 chamadas/sessao (menos chats novos)
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
$env:AETHER_CLAUDE_SESSION_REUSE = "1"
$env:AETHER_CLAUDE_SESSION_MAX_CALLS = "50"
$env:AETHER_LLM_HEARTBEAT_EVERY = "5"
$env:AETHER_FORCE_EXPLORATION_EVERY = "8"
$env:AETHER_HYPOTHESIS_MODEL = "claude-haiku-4-5-20251001"
$env:AETHER_VALIDATION_MODEL = "claude-haiku-4-5-20251001"
$env:AETHER_PORTFOLIO_MODEL  = "claude-haiku-4-5-20251001"
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

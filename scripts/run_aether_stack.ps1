# Wrapper de compatibilidade para o stack completo do Aether.
#
# O script antigo abria processos em PowerShell separados e podia duplicar
# server/validator/critic/hyper/deploy. Este wrapper preserva o tuning do
# stack completo, mas delega para run_aether_bg.ps1, que tem PID file e trava
# contra processos duplicados.
#
# Uso:
#   .\scripts\run_aether_stack.ps1
#   .\scripts\run_aether_stack.ps1 -Restart

param(
    [switch]$Restart,
    [switch]$Bot,
    [switch]$Dashboard
)

$ErrorActionPreference = "Stop"

# Tuning de geracao (Tier A: prompt 3KB + effort high + 4000 tokens; Tier B: engines estruturais + two-stage opcional).
if (-not $env:AETHER_LLM_MAX_NEW)                          { $env:AETHER_LLM_MAX_NEW = "6" }
if (-not $env:AETHER_LLM_MAX_TOKENS)                       { $env:AETHER_LLM_MAX_TOKENS = "2000" }
if (-not $env:AETHER_HYPOTHESIS_MAX_TOKENS)                { $env:AETHER_HYPOTHESIS_MAX_TOKENS = "4000" }
if (-not $env:AETHER_ROLE_HYPOTHESIS_GENERATOR_MAX_TOKENS) { $env:AETHER_ROLE_HYPOTHESIS_GENERATOR_MAX_TOKENS = "4000" }
if (-not $env:AETHER_HYPOTHESIS_EFFORT)                    { $env:AETHER_HYPOTHESIS_EFFORT = "high" }
if (-not $env:AETHER_HYPOTHESIS_TWO_STAGE)                 { $env:AETHER_HYPOTHESIS_TWO_STAGE = "0" }
if (-not $env:AETHER_SEMANTIC_K)                           { $env:AETHER_SEMANTIC_K = "4" }
if (-not $env:AETHER_DUPLICATE_THRESHOLD)                  { $env:AETHER_DUPLICATE_THRESHOLD = "0.92" }
if (-not $env:AETHER_LLM_PROMPT_CACHE_DISABLE)             { $env:AETHER_LLM_PROMPT_CACHE_DISABLE = "0" }
if (-not $env:AETHER_ECONOMY_MODE)                         { $env:AETHER_ECONOMY_MODE = "1" }

# Readiness LLM: quando o stack e iniciado via PowerShell, o PATH do processo
# nem sempre enxerga o shim npm do Codex, mesmo com o CLI autenticado em
# C:\Users\<usuario>\.codex. Expor o caminho explicito evita que
# llm_status() marque o provider como configured=false.
if (-not $env:AETHER_CODEX_CLI_PATH) {
    $codexShim = Join-Path $env:APPDATA "npm\codex.cmd"
    # Caminho esperado em Windows: AppData\Roaming\npm\codex.cmd
    if (Test-Path $codexShim) { $env:AETHER_CODEX_CLI_PATH = $codexShim }
}

# Critic v5 (regime gate + v4 gates).
if (-not $env:AETHER_CRITIC_MTF_DISABLE)                   { $env:AETHER_CRITIC_MTF_DISABLE = "1" }
if (-not $env:AETHER_CRITIC_BOOTSTRAP_MIN_SHARPE_P5)       { $env:AETHER_CRITIC_BOOTSTRAP_MIN_SHARPE_P5 = "-0.3" }
if (-not $env:AETHER_CRITIC_GRID_MIN_P25_PNL)              { $env:AETHER_CRITIC_GRID_MIN_P25_PNL = "-7.0" }
if (-not $env:AETHER_CRITIC_REGIME_DISABLE)                { $env:AETHER_CRITIC_REGIME_DISABLE = "0" }
if (-not $env:AETHER_CRITIC_REGIME_MIN_PASS)               { $env:AETHER_CRITIC_REGIME_MIN_PASS = "2" }
if (-not $env:AETHER_CRITIC_REGIME_MIN_PNL)                { $env:AETHER_CRITIC_REGIME_MIN_PNL = "-5.0" }
if (-not $env:AETHER_CRITIC_REGIME_MIN_VALID)              { $env:AETHER_CRITIC_REGIME_MIN_VALID = "2" }

# Manifest operacional do bot: alinhar com src/config/portfolio.yaml.
# Não restringir o deploy a uma lista antiga de 4 ativos; o bot/dashboard agora
# operam o portfólio configurado completo.
if (-not $env:AETHER_DEPLOY_ALLOWED_ASSETS)                { $env:AETHER_DEPLOY_ALLOWED_ASSETS = "BTCUSDT,ETHUSDT,SOLUSDT,ADAUSDT,XRPUSDT,LINKUSDT,DOGEUSDT,AVAXUSDT,BNBUSDT" }
if (-not $env:AETHER_DEPLOY_ALLOWED_TIMEFRAMES)            { $env:AETHER_DEPLOY_ALLOWED_TIMEFRAMES = "15m,1h,4h" }
if (-not $env:AETHER_DEPLOY_MIN_TRADES)                    { $env:AETHER_DEPLOY_MIN_TRADES = "60" }
if (-not $env:AETHER_DEPLOY_MAX_MDD_PCT)                   { $env:AETHER_DEPLOY_MAX_MDD_PCT = "0" }
if (-not $env:AETHER_DEPLOY_TRADE_COUNT_WEIGHT)            { $env:AETHER_DEPLOY_TRADE_COUNT_WEIGHT = "0.35" }
if (-not $env:AETHER_DEPLOY_PNL_WEIGHT)                    { $env:AETHER_DEPLOY_PNL_WEIGHT = "0.08" }
if (-not $env:AETHER_DEPLOY_SHARPE_WEIGHT)                 { $env:AETHER_DEPLOY_SHARPE_WEIGHT = "0.08" }
if (-not $env:AETHER_DEPLOY_MIN_HIT_RATE)                  { $env:AETHER_DEPLOY_MIN_HIT_RATE = "0.63" }
if (-not $env:AETHER_DEPLOY_HIT_RATE_WEIGHT)               { $env:AETHER_DEPLOY_HIT_RATE_WEIGHT = "0.45" }
if (-not $env:AETHER_DEPLOY_MAX_EST_TRADES_PER_DAY)        { $env:AETHER_DEPLOY_MAX_EST_TRADES_PER_DAY = "5.0" }
if (-not $env:AETHER_DEPLOY_MDD_PENALTY)                   { $env:AETHER_DEPLOY_MDD_PENALTY = "0.10" }
if (-not $env:AETHER_DEPLOY_FAST_TF_BONUS)                 { $env:AETHER_DEPLOY_FAST_TF_BONUS = "0.08" }

if (-not $env:AETHER_DEPLOY_MAX_PER_FAMILY)                { $env:AETHER_DEPLOY_MAX_PER_FAMILY = "30" }
if (-not $env:AETHER_DEPLOY_MAX_PER_ASSET)                 { $env:AETHER_DEPLOY_MAX_PER_ASSET = "10" }
if (-not $env:AETHER_DEPLOY_MAX_PER_ASSET_FAMILY_TF)       { $env:AETHER_DEPLOY_MAX_PER_ASSET_FAMILY_TF = "7" }
if (-not $env:AETHER_DEPLOY_MAX_PER_HYPOTHESIS_ASSET)      { $env:AETHER_DEPLOY_MAX_PER_HYPOTHESIS_ASSET = "5" }
if (-not $env:AETHER_DEPLOY_TOP_N)                         { $env:AETHER_DEPLOY_TOP_N = "80" }
if (-not $env:AETHER_DEPLOY_MANAGED_SOURCES)               { $env:AETHER_DEPLOY_MANAGED_SOURCES = "aether_auto_deploy,aether_happynation,aether_manual_ranking_deploy" }
if (-not $env:AETHER_DEPLOY_RESPECT_RANKING_RESET)         { $env:AETHER_DEPLOY_RESPECT_RANKING_RESET = "1" }

$paramsForBg = @{ Stack = $true }
if ($Restart) { $paramsForBg.Restart = $true }
if ($Bot) { $paramsForBg.Bot = $true }
if ($Dashboard) { $paramsForBg.Dashboard = $true }

& (Join-Path $PSScriptRoot "run_aether_bg.ps1") @paramsForBg
exit $LASTEXITCODE

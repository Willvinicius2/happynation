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

# Critic v5 (regime gate + v4 gates).
if (-not $env:AETHER_CRITIC_MTF_DISABLE)                   { $env:AETHER_CRITIC_MTF_DISABLE = "1" }
if (-not $env:AETHER_CRITIC_BOOTSTRAP_MIN_SHARPE_P5)       { $env:AETHER_CRITIC_BOOTSTRAP_MIN_SHARPE_P5 = "-0.3" }
if (-not $env:AETHER_CRITIC_GRID_MIN_P25_PNL)              { $env:AETHER_CRITIC_GRID_MIN_P25_PNL = "-7.0" }
if (-not $env:AETHER_CRITIC_REGIME_DISABLE)                { $env:AETHER_CRITIC_REGIME_DISABLE = "0" }
if (-not $env:AETHER_CRITIC_REGIME_MIN_PASS)               { $env:AETHER_CRITIC_REGIME_MIN_PASS = "2" }
if (-not $env:AETHER_CRITIC_REGIME_MIN_PNL)                { $env:AETHER_CRITIC_REGIME_MIN_PNL = "-5.0" }
if (-not $env:AETHER_CRITIC_REGIME_MIN_VALID)              { $env:AETHER_CRITIC_REGIME_MIN_VALID = "2" }

# Manifest operacional do bot: apenas ativos operados e candle 1h.
if (-not $env:AETHER_DEPLOY_ALLOWED_ASSETS)                { $env:AETHER_DEPLOY_ALLOWED_ASSETS = "BTCUSDT,ETHUSDT,SOLUSDT,ADAUSDT" }
if (-not $env:AETHER_DEPLOY_ALLOWED_TIMEFRAMES)            { $env:AETHER_DEPLOY_ALLOWED_TIMEFRAMES = "15m,1h,4h,1d" }
if (-not $env:AETHER_DEPLOY_MIN_TRADES)                    { $env:AETHER_DEPLOY_MIN_TRADES = "12" }
if (-not $env:AETHER_DEPLOY_MAX_PER_FAMILY)                { $env:AETHER_DEPLOY_MAX_PER_FAMILY = "30" }
if (-not $env:AETHER_DEPLOY_MAX_PER_ASSET)                 { $env:AETHER_DEPLOY_MAX_PER_ASSET = "12" }
if (-not $env:AETHER_DEPLOY_MAX_PER_ASSET_FAMILY_TF)       { $env:AETHER_DEPLOY_MAX_PER_ASSET_FAMILY_TF = "5" }
if (-not $env:AETHER_DEPLOY_TOP_N)                         { $env:AETHER_DEPLOY_TOP_N = "80" }

$paramsForBg = @{ Stack = $true }
if ($Restart) { $paramsForBg.Restart = $true }
if ($Bot) { $paramsForBg.Bot = $true }
if ($Dashboard) { $paramsForBg.Dashboard = $true }

& (Join-Path $PSScriptRoot "run_aether_bg.ps1") @paramsForBg
exit $LASTEXITCODE

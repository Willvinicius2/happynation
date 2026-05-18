# Restart only auto_deploy with the new MIN_SCORE.
# Mimics run_aether_bg.ps1's Start-Process pattern.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logsDir = Join-Path $root "logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }

# Env do start_aether.bat - replicado
$env:AETHER_DEPLOY_MIN_SCORE = "0.45"
$env:AETHER_DEPLOY_TOP_N = "250"
$env:AETHER_DEPLOY_ALLOWED_TIMEFRAMES = "*"
$env:AETHER_DEPLOY_ALLOWED_ASSETS = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,MATICUSDT"
$env:AETHER_DEPLOY_MAX_PER_FAMILY = "0"
$env:AETHER_DEPLOY_MAX_PER_ASSET = "0"
$env:AETHER_DEPLOY_MAX_PER_ASSET_FAMILY_TF = "0"
$env:AETHER_DEPLOY_MAX_PER_HYPOTHESIS_ASSET = "0"
$env:AETHER_DEPLOY_CRITIC_GATE = "approved"
$env:AETHER_DEPLOY_MIN_TRADES = "12"
$env:AETHER_DEPLOY_REQUIRE_COMBO_FOR_MULTI = "1"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $logsDir ("deploy-" + $stamp + ".log")
$errPath = Join-Path $logsDir ("deploy-" + $stamp + ".err.log")
Set-Content -Path $logPath -Value "[start] $(Get-Date -Format o) src.research.aether.auto_deploy" -Encoding utf8
Set-Content -Path $errPath -Value "" -Encoding utf8

$proc = Start-Process -FilePath $python `
    -ArgumentList @("-u","-m","src.research.aether.auto_deploy") `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $logPath `
    -RedirectStandardError $errPath `
    -PassThru
Write-Output ("started pid=" + $proc.Id + " log=" + $logPath)

@echo off
REM Sobe o stack de pesquisa do Aether em background:
REM   server, validator, critic, hyper, deploy + agents loop (Tier 3)
REM
REM Caps de deploy estao desligados (variantes lucrativas entram livremente).
REM Allowlist de ativos: BTC, ETH, SOL, ADA, XRP, LINK
REM
REM Aether dashboard: http://127.0.0.1:8765

cd /d "%~dp0"

REM Caps de variantes TODOS DESLIGADOS - so dedupe estrutural permanece
REM (engine+params+asset+risk identicos colapsam, mantem o de maior score).
REM Razao: variantes com parametros diferentes sao estrategias DIFERENTES e
REM devem coexistir se foram aprovadas pelo Critic v4.
set "AETHER_DEPLOY_MAX_PER_ASSET_FAMILY_TF=0"
set "AETHER_DEPLOY_MAX_PER_ASSET=0"
set "AETHER_DEPLOY_MAX_PER_FAMILY=0"
set "AETHER_DEPLOY_TOP_N=150"
REM Sem cap de timeframe - 1H, 4H, 1D, todas entram (era so "1H" por default)
set "AETHER_DEPLOY_ALLOWED_TIMEFRAMES=*"
REM Allowlist alinhada com os 10 ativos do hub multi-TF (portfolio.yaml).
REM Sem todos aqui, descobertas multi-TF nos novos ativos nao deployam.
set "AETHER_DEPLOY_ALLOWED_ASSETS=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,MATICUSDT"

REM === Auto_deploy DESTRAVADO 2026-05-08 ===
REM MIN_SCORE 99 (freio da auditoria 2026-05-07) baixado para 0.55: o Critic v5
REM com regime gate ja eh o filtro real. Tier 3 do StrategyTiers faz a quarentena
REM em paper antes de qualquer estrategia receber posicao maior.
set "AETHER_DEPLOY_MIN_SCORE=0.55"

REM === Critic v4 INTERMEDIARIO ===
REM Calibracao 2026-05-07: nivel intermediario entre o default original
REM (frageis aprovavam por sorte estatistica em janelas curtas) e o
REM rigoroso de antes (12 folds + cross -1% rejeitou 100% das 250).
REM Aperta robustez sem inviabilizar aprovacao.
set "AETHER_CRITIC_WF_FOLDS=8"
set "AETHER_CRITIC_WF_MIN_PASS=6"
set "AETHER_CRITIC_WF_MIN_PNL=-2.0"
set "AETHER_CRITIC_CROSS_MIN_PNL=-2.0"
set "AETHER_CRITIC_BOOTSTRAP_MIN_SHARPE_P5=-0.15"
set "AETHER_CRITIC_MTF_DISABLE=1"
set "AETHER_CRITIC_GRID_MIN_POSITIVE_FRAC=0.55"
set "AETHER_CRITIC_GRID_MIN_P25_PNL=-5.0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_aether_bg.ps1" -Full -Restart
if errorlevel 1 goto :end

REM Agents loop (PolicyAgent + Tier 3) DESATIVADO por padrao - era ele que
REM escrevia kill switch global por "estrategia nao aprovada" / "duplicidade",
REM derrubando trades antes do SL/trail. Para reativar, rode start_agents_loop.bat.

echo.
echo Aether iniciado. Abrindo dashboard...
start "" "http://127.0.0.1:8765"
echo.
echo Para parar: scripts\stop_aether.ps1
:end
pause

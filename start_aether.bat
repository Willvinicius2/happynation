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
set "AETHER_DEPLOY_TOP_N=250"
set "AETHER_DEPLOY_MAX_PER_HYPOTHESIS_ASSET=0"
REM Sem cap de timeframe - 1H, 4H, 1D, todas entram (era so "1H" por default)
set "AETHER_DEPLOY_ALLOWED_TIMEFRAMES=*"
REM Allowlist alinhada com os 10 ativos do hub multi-TF (portfolio.yaml).
REM Sem todos aqui, descobertas multi-TF nos novos ativos nao deployam.
set "AETHER_DEPLOY_ALLOWED_ASSETS=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,MATICUSDT"

REM === Auto_deploy DESTRAVADO 2026-05-08 ===
REM MIN_SCORE baixado de 0.55 -> 0.45: o Critic v5 (8 folds walk-forward,
REM cross-symbol, regime gate, bootstrap) ja eh o filtro real de robustez.
REM Score composto entre 0.42-0.55 representa estrategias que o Critic aprovou
REM mas que ficaram em 2o plano no scoring weighted. Manter 0.55 estava
REM cortando 49/71 validadas (69%) e gerando portfolio concentrado.
REM Tier 3 do StrategyTiers faz a quarentena em paper.
set "AETHER_DEPLOY_MIN_SCORE=0.45"

REM === Critic v4 INTERMEDIARIO ===
REM Calibracao 2026-05-07: nivel intermediario entre o default original
REM (frageis aprovavam por sorte estatistica em janelas curtas) e o
REM rigoroso de antes (12 folds + cross -1% rejeitou 100% das 250).
REM Aperta robustez sem inviabilizar aprovacao.
REM === Critic v4 RELAXADO (Tier A 2026-05-09): cross-symbol estava cortando 100% pos-boost ===
REM Antes: cross_min_pnl=-2.0 wf_min_pass=6/8 grid_min_p25=-5.0
REM Agora: cross_min_pnl=-4.0 wf_min_pass=5/8 grid_min_p25=-7.0 cross_pool=4
REM Razao: o gate cross-symbol estava rejeitando estrategias com pnl=161% e 48%.
REM Pool maior (4 alts) reduz variancia. Mais tolerante mas wf+regime+bootstrap
REM continuam sendo as redes de seguranca de robustez.
set "AETHER_CRITIC_WF_FOLDS=8"
set "AETHER_CRITIC_WF_MIN_PASS=5"
set "AETHER_CRITIC_WF_MIN_PNL=-2.0"
set "AETHER_CRITIC_CROSS_MIN_PNL=-4.0"
set "AETHER_CRITIC_CROSS_POOL=4"
set "AETHER_CRITIC_BOOTSTRAP_MIN_SHARPE_P5=-0.15"
set "AETHER_CRITIC_MTF_DISABLE=1"
set "AETHER_CRITIC_GRID_MIN_POSITIVE_FRAC=0.55"
set "AETHER_CRITIC_GRID_MIN_P25_PNL=-7.0"

REM === Modo industrial 2026-05-09 (autorizado: "mais robusto e produtivo mesmo que custe mais token") ===
REM Tier C: maximizar throughput LLM sem quebrar concorrencia.
REM   AETHER_LLM_MAX_NEW=12 (era 8): cada chamada LLM cria ate 12 hipoteses
REM   AETHER_LLM_MAX_NEW_HARD_CAP=20 (era 12): destrava o cap interno
REM   AETHER_LLM_HEARTBEAT_BATCH=8: batch maior em heartbeat
REM   AETHER_LLM_HEARTBEAT_EVERY=1: dispara LLM a cada cycle quando ocioso
REM   AETHER_FORCE_EXPLORATION_EVERY=2: forced rounds 2x mais frequentes
REM   AETHER_LLM_INFLIGHT_TOLERANCE=16: permite ate 16 batches em voo concorrentes
REM   AETHER_FAMILY_NODE_CAP=20: 2.5x mais nos por familia (combo destravado)
REM   AETHER_INFLIGHT_WINDOW_MINUTES=15: janela inflight menor
set "AETHER_LLM_MAX_NEW=12"
set "AETHER_LLM_MAX_NEW_HARD_CAP=20"
set "AETHER_LLM_HEARTBEAT_BATCH=8"
set "AETHER_LLM_HEARTBEAT_EVERY=1"
set "AETHER_FORCE_EXPLORATION_EVERY=2"
set "AETHER_LLM_INFLIGHT_TOLERANCE=16"
set "AETHER_FAMILY_NODE_CAP=20"
set "AETHER_INFLIGHT_WINDOW_MINUTES=15"
REM Qualidade de pesquisa: apenas o Hypothesis Generator usa GPT-5.5.
REM Os demais papeis continuam no modelo economico configurado no launcher.
set "AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_MODEL=gpt-5.5"
set "AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_EFFORT=high"
REM Pesquisa web continua: roda como heartbeat mesmo quando ja ha fila de testes.
set "AETHER_WEB_RESEARCH_HEARTBEAT_EVERY=1"
set "AETHER_WEB_INFLIGHT_TOLERANCE=24"
set "AETHER_CONTINUOUS_RESEARCH_INTERVAL_SECONDS=20"
REM Cooldown LLM agressivo: 45min default era conservador demais ("verde sem
REM produzir"). 10min mantem espacamento saudavel sem matar throughput.
set "AETHER_LLM_HYPOTHESIS_COOLDOWN_MINUTES=10"
REM Mais probes por worker: batches scout terminam mais rapido = loop principal
REM libera mais cedo para nova chamada LLM.
set "AETHER_TARGET_PROBES_PER_WORKER=10"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_aether_bg.ps1" -Full -Restart
if errorlevel 1 goto :end

REM Auto-iniciar o cycle de pesquisa (continuous). Sem isso o orchestrator no
REM server nao roda _run_research_phase, e nenhuma hipotese nova eh proposta —
REM o stack fica vivo mas nao produz. Espera o server subir e dispara.
REM workers=12 (escolha do usuario 2026-05-11). AVISO: nesta maquina pode dar
REM DLL load failed / MemoryError em alguns workers quando o paging file estoura
REM (tasks viram 'failed', cycle continua). Pra eliminar: aumentar memoria virtual
REM do Windows (Painel de Controle -> Sistema -> Avancado -> Memoria Virtual).
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 5; try { $r = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/cycle/start?workers=12&limit_nodes=24&probes_per_node=60&max_tasks=24&continuous=true' -TimeoutSec 10; Write-Host '[cycle] started=' $r.started 'limit_nodes=' $r.effective_limit_nodes } catch { Write-Host '[cycle] FALHOU iniciar:' $_.Exception.Message }"

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

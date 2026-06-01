# Auditoria de organizacao e faltantes

Data: 2026-05-21
Escopo: `D:/openclaude/HAPPY`

## Resumo rapido

O projeto esta funcional e testado, mas ainda tem alguns pontos de organizacao que dificultam manutencao:

- `src/dashboard.py` estava misturando API Python, leitura de estado e um HTML/CSS/JS muito grande no mesmo arquivo.
- `scripts/` tem muitos scripts de pesquisa/debug/oneshot misturados; funciona, mas esta dificil saber o que e operacional, auditoria ou experimento antigo.
- `src/logs/research/active_strategies.json` aparece modificado no Git; e estado/runtime, entao deve ser tratado com cuidado para nao sobrescrever dados vivos.
- A suite passa, mas ainda ha warnings de dependencia e warnings futuros do pandas nos engines swing.

## O que foi organizado agora

Primeira fatia segura de refatoracao:

- Extraido o template grande do Trade Dashboard para:
  - `src/static/trade_dashboard.html`
- Separados os assets visuais do dashboard para:
  - `src/static/trade_dashboard.css`
  - `src/static/trade_dashboard.js`
- `src/dashboard.py` ficou responsavel por API/rotas/leitura de payload e agora carrega o HTML externo via `DASHBOARD_HTML_PATH`, servindo os assets por `/static`.
- Adicionado teste para garantir que o dashboard continua servindo o template externo e os assets CSS/JS.

Segunda fatia segura de refatoracao:

- Extraido o helper puro de arredondamento de ordens para:
  - `src/modules/order_math.py`
- `BinanceTraderBot.adjust_to_step(...)` foi mantido como wrapper de compatibilidade, sem mudar a API publica usada pelas ordens.
- Adicionado `tests/test_order_math.py` cobrindo floor por step, retorno string com precisao fixa, rejeicao de step invalido e contrato legado do metodo do trader.

Terceira fatia segura de refatoracao:

- Extraido o helper puro de sizing/cap de posicao real para:
  - `src/modules/order_math.py::validate_position_size`
- `BinanceTraderBot._validate_real_position_size(...)` foi mantido como wrapper de compatibilidade, ainda lendo `MAX_POSITION_USD` e `MIN_POSITION_USD` do `PaperBalanceLogger`.
- `tests/test_order_math.py` agora cobre cap de notional, reducao proporcional de quantidade, bloqueio quando fica abaixo do minimo e contrato legado do metodo do trader.

Impacto:

- `src/dashboard.py` caiu de ~1721 linhas para ~729 linhas.
- `src/static/trade_dashboard.html` agora ficou enxuto (~130 linhas), com CSS/JS separados.
- `src/modules/BinanceTraderBot.py` caiu de ~2172 para ~2148 linhas; as primeiras funcoes puras sairam para `src/modules/order_math.py` (~54 linhas).
- A UI fica separada do backend, facilitando proximas melhorias visuais sem mexer em rota/API.
- O arredondamento e sizing/cap de ordens agora podem ser testados isoladamente, sem instanciar cliente Binance/TinyDB.
- Risco baixo: nenhuma regra de trading, deploy ou paper foi alterada.

## Mapa atual de tamanho

Levantamento local:

- Python total: 267 arquivos, ~58.089 linhas.
- Maiores areas:
  - `src/research`: 41 arquivos, ~17.287 linhas.
  - `scripts`: 78 arquivos, ~11.987 linhas.
  - `src/strategies`: 67 arquivos, ~8.915 linhas.
  - `src/modules`: 17 arquivos, ~6.169 linhas.
  - `tests`: 16 arquivos, ~4.439 linhas.

Maiores arquivos antes/depois da fatia:

- `src/modules/BinanceTraderBot.py`: antes ~2172, agora ~2148 linhas.
- `src/modules/order_math.py`: ~54 linhas extraidas do trader.
- `src/research/aether/orchestrator.py`: ~1951 linhas.
- `src/dashboard.py`: antes ~1721, agora ~729.
- `src/static/trade_dashboard.html`: ~130 linhas; CSS/JS em arquivos proprios.
- `src/research/aether/compute_signals.py`: ~1275 linhas.
- `src/modules/PaperBalanceLogger.py`: ~1102 linhas.

## O que esta faltando / backlog priorizado

### P1 - organizacao que impacta manutencao direta

1. Separar `src/modules/BinanceTraderBot.py`
   - Hoje concentra execucao, paper/live, risco, trailing, integracao e estado.
   - Extracoes concluidas: `src/modules/order_math.py` com `adjust_to_step` e `validate_position_size`, ambos puros e testados.
   - Proxima fatia segura: extrair mais funcoes puras de trailing/stop ou helpers de dados stale com wrapper de compatibilidade.

2. Organizar `scripts/`
   - Separar em subpastas:
     - `scripts/ops/` para start/stop/status/watchdog.
     - `scripts/audit/` para relatorios e checks.
     - `scripts/research/` para sweeps/validacoes Aether.
     - `scripts/oneshots/` para experimentos antigos.
   - Fazer isso em etapas, porque alguns comandos/README podem depender dos caminhos atuais.

3. Criar um comando unico de saude
   - Algo como `python scripts/status.py --json` ou `python scripts/aether_quick_status.py` consolidado.
   - Deve responder: bot vivo, Aether vivo, dashboard vivo, manifest, paper equity, trades recentes, LLM pronto/nao pronto.

### P2 - qualidade/robustez

1. Corrigir warnings futuros do pandas nos engines swing
   - Arquivos citados pela suite:
     - `src/strategies/swing/donchian_breakout.py`
     - `src/strategies/swing/ema_trend.py`
     - `src/strategies/swing/bollinger_meanrev.py`
   - Nao quebra hoje, mas pode quebrar em pandas 3.0.

2. Externalizar mais UI do dashboard
   - Concluido para a fatia principal: HTML, CSS e JS ja estao separados.
   - Proximo passo visual agora deve ser melhoria de UX/cockpit, nao mais separacao basica de arquivos.

3. Padronizar docs de operacao
   - `README.md`, `docs/PROJECT.md`, `docs/OPERATIONS.md` e `docs/STRATEGIES.md` existem.
   - Falta um indice curto de scripts recomendados vs scripts legados/experimento.

### P3 - limpeza futura

1. Reduzir duplicacoes em estrategias extras.
2. Marcar scripts antigos como legado antes de mover/deletar.
3. Adicionar lint/format automatico em CI local, se o projeto for virar rotina de PR.

## Cuidados importantes

- Nao mexer em `.env` nem imprimir segredos.
- Nao sobrescrever `src/logs/research/active_strategies.json` sem backup/decisao explicita.
- Nao ligar cron/monitoramento automatico; runtime deve ser iniciado apenas quando solicitado.
- Nao refatorar bot/trading em lote grande: risco de quebrar execucao paper/live.

## Verificacao feita

Comandos executados:

```bash
python -m py_compile src/dashboard.py
python -m pytest tests/test_trade_dashboard.py -q
python -m pytest tests/test_order_math.py tests/test_smoke.py tests/test_safety_guards.py -q
python -m pytest -q
```

Resultado:

- `tests/test_trade_dashboard.py`: 5 passed.
- `tests/test_order_math.py` + smoke/safety selecionados apos a terceira fatia: 39 passed.
- Suite completa apos a terceira fatia: 197 passed.

Warnings restantes sao conhecidos e nao bloqueiam a fatia atual:

- Deprecation warnings de dependencias `binance`, `websockets`, `fastapi`.
- FutureWarning do pandas nos engines swing.

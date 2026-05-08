# Estrategias e Pesquisa

## Estrategias principais

| Arquivo | Ideia |
|---|---|
| `src/strategies/vortex_strategy.py` | tendencia via Vortex |
| `src/strategies/rsi_strategy.py` | reversao/forca relativa |
| `src/strategies/moving_average.py` | cruzamento/media movel |
| `src/strategies/ma_rsi_volume_strategy.py` | media + RSI + volume |
| `src/strategies/high_win_rate_scalp.py` | scalp de alta frequencia |
| `src/strategies/champions_strategy.py` | estrategia campea consolidada |

`src/strategies/extras` e `src/indicators/extras` guardam candidatos e blocos tecnicos extras. Eles sao uteis para pesquisa, mas nao devem ser lidos primeiro quando a duvida for sobre o bot principal.

## Backtests

Scripts principais:

```powershell
python src/backtests.py
python src/ranking_backtest.py
python src/backtest_champions_strategy.py
python scripts/generate_backtest_report.py
```

Resultados grandes sao gerados em `src/logs/ranking` ou `src/logs/research`. Eles foram removidos da organizacao inicial porque pesavam muito e atrapalhavam leitura, mas podem ser recriados pelos scripts.

## Pesquisa automatizada

Arquivos importantes:

| Arquivo | Papel |
|---|---|
| `src/strategy_research.py` | loop de pesquisa de estrategias |
| `src/research/catalog.py` | catalogo persistente |
| `src/research/validator.py` | criterios de validacao |
| `src/research/active.py` | manifest de estrategias ativas |
| `src/research/aether/*` | geracao, scoring, ranking e runtime Aether |

Estado padrao:

```text
src/logs/research/strategy_catalog.json
src/logs/research/active_strategies.json
```

## Aether

Aether e o subsistema de pesquisa com hipoteses e campanhas. Use quando quiser gerar ideias novas, ranquear candidatos ou manter um ciclo de descoberta mais amplo.

Arquivos de leitura inicial:

- `src/research/aether/orchestrator.py`
- `src/research/aether/campaigns.py`
- `src/research/aether/ranking.py`
- `src/research/aether/server.py`

UI:

```text
src/research/aether/static/index.html
```

## Criterios praticos antes de promover estrategia

- Passar em mais de um ativo/timeframe.
- Ter drawdown aceitavel.
- Nao depender de um unico periodo de mercado.
- Rodar em paper antes de producao.
- Ser validada por agente ou teste independente.

## Testes

```powershell
pytest
pytest tests/test_strategies.py -v
pytest tests/test_agents.py -v
```

Ao adicionar estrategia nova, teste no minimo:

- retorno `True`, `False` ou `None`;
- comportamento com dados curtos;
- parametros customizados;
- caso de tendencia e caso lateralizado.

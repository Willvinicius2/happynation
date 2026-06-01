# Projeto

Este arquivo e o mapa para humanos, Codex e Claude lerem o projeto sem varrer tudo.

## Pastas

```text
src/
  main.py                 entrada principal do bot
  dashboard.py            servidor/painel do dashboard
  research_server.py      servidor do dashboard de pesquisa
  modules/                cliente Binance, trader, logger, Telegram, dashboard terminal
  strategies/             estrategias ativas e extras
  indicators/             indicadores ativos e extras
  agents/                 agentes locais de risco, performance, saude, pesquisa e politica
  research/               catalogo, validacao, descoberta e Aether
  backtesting/            helper compartilhado de backtest
  database/               configs JSON pequenas
  static/                 dashboards HTML estaticos (ex.: trade_dashboard.html)
  logs/                   estado/runtime gerado; nao tratar como codigo
scripts/                  tarefas manuais e automacoes de pesquisa
tests/                    suite pytest
examples/                 perfis de .env
docs/                     documentacao consolidada
```

## Entrypoints

| Objetivo | Comando |
|---|---|
| Rodar bot | `python -m src.main` |
| Dashboard de pesquisa | `python -m src.research_server` |
| Backtest rapido | `python src/backtests.py` |
| Relatorio de backtest | `python scripts/generate_backtest_report.py` |
| Validar estrategias ativas | `python scripts/validate_active_strategies.py` |
| Rodar testes | `pytest` |

## Arquivos de estado

O codigo ainda usa `src/logs` como local padrao de runtime:

- `src/logs/trading_bot.log`: log principal.
- `src/logs/thread_debug.log`: debug de threads.
- `src/logs/paper_balance/state.json`: estado do paper trading.
- `src/logs/research/strategy_catalog.json`: catalogo de estrategias descobertas.
- `src/logs/research/active_strategies.json`: manifest de estrategias ativas, quando existir.

Esses arquivos podem ser recriados pelo bot ou pelos scripts. Para versionamento, prefira guardar apenas exemplos pequenos e deixar resultados grandes fora do Git.

## Agentes

Agentes implementados vivem em `src/agents`:

| Agente | Responsabilidade |
|---|---|
| `RiskAgent` | risco operacional do paper |
| `PerformanceAgent` | performance por ativo |
| `StrategyValidatorAgent` | auditoria de estrategias aprovadas |
| `StrategyResearcherAgent` | auditoria do catalogo |
| `AetherResearcherAgent` | hipoteses de estrategia via Aether/LLM |
| `PortfolioAgent` | exposicao e pesos |
| `PolicyAgent` | decisao consolidada por ativo |
| `ConfigAgent`, `SecurityAgent`, `HealthAgent` | protecao e sanidade operacional |

Principio do projeto: agentes devem comecar em leitura, e qualquer acao automatica precisa de flag explicita no `.env`.

## Convencoes

- Use `src/backtesting` para helpers de backtest compartilhados.
- Use `scripts` para tarefas executadas manualmente.
- Use `tests` para testes automatizados.
- Evite colocar resultados grandes em `src/logs`; eles atrapalham leitura e contexto dos agentes.
- Mantenha novas docs dentro destes tres arquivos sempre que possivel.

# HAPPYNATIONBOT

Bot de trading para Binance com paper trading, estrategias tecnicas, backtests, agentes locais e dashboards simples.

> Aviso: trading real envolve risco financeiro. Rode em `PAPER_TRADING=true`, use chaves restritas e comece pequeno.

## Leitura rapida

| Arquivo | Uso |
|---|---|
| [docs/PROJECT.md](docs/PROJECT.md) | Mapa das pastas, entrypoints e arquitetura |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Instalar, configurar, rodar, deploy e troubleshooting |
| [docs/STRATEGIES.md](docs/STRATEGIES.md) | Estrategias, backtests, Aether e pesquisa |
| [docs/REFACTOR_AUDIT.md](docs/REFACTOR_AUDIT.md) | Auditoria de organizacao, faltantes e backlog de refatoracao |

## Comecar

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy examples\config_conservative.env .env
python -m src.main
```

Edite `.env` antes de rodar com suas credenciais. Para Linux/macOS, use `source .venv/bin/activate` e `cp examples/config_conservative.env .env`.

## Estrutura curta

```text
src/        codigo do bot, estrategias, agentes, pesquisa e dashboards
scripts/    automacoes manuais: sweeps, validacoes, relatorios e instalacao
tests/      testes pytest
docs/       tres guias curtos para humanos e agentes
examples/   perfis de .env prontos para copiar
```

## Comandos uteis

```powershell
python -m src.main
pytest
python scripts\generate_backtest_report.py
python scripts\validate_active_strategies.py
python -m src.research_server
```

Dashboards:

- `src/static/dashboard.html`: painel estatico do bot.
- `src/static/research_dashboard.html`: painel de pesquisa.
- `src/research/aether/static/index.html`: UI do Aether.

## Estado atual da organizacao

O projeto foi reduzido para poucas pastas principais. Caches Python, caches pytest e quase todos os logs/backtests gerados foram removidos. Foram preservados:

- `.env`, por ser configuracao local.
- `src/logs/research/strategy_catalog.json`, por ser estado util da pesquisa.
- `src/logs/paper_balance/state.json`, por ser estado util do paper.
- `src/logs/terminal.log`, porque esta aberto por outro processo no Windows.

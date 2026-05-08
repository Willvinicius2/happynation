# Operacao

## Instalacao local

Windows:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy examples\config_conservative.env .env
```

Linux/macOS:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp examples/config_conservative.env .env
```

Depois edite `.env` com suas chaves Binance e Telegram.

## Perfis de exemplo

| Arquivo | Perfil |
|---|---|
| `examples/config_conservative.env` | comeco mais cauteloso, menos trades |
| `examples/config_aggressive.env` | maior frequencia e risco |
| `examples/config_scalping.env` | scalping, exige monitoramento maior |

Recomendacao pratica: comece com `config_conservative.env` e rode em paper por pelo menos alguns dias antes de mexer em risco.

## Variaveis importantes

| Variavel | Observacao |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_SECRET_KEY` | use chave restrita e sem saque |
| `PAPER_TRADING` | mantenha `true` ate validar bem |
| `REAL_TRADING_CONFIRM` | deve ser explicito para dinheiro real |
| `MOEDAS` | ativos monitorados |
| `CANDLE_PERIOD` | timeframe principal |
| `QUANTIDADE_POR_TRADE` | tamanho por ordem |
| `STOP_LOSS_PERCENTAGE` | limite de perda por trade |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | alertas |

## Rodar

```powershell
python -m src.main
```

Testes:

```powershell
pytest
```

Pesquisa e validacao:

```powershell
python scripts\validate_active_strategies.py
python scripts\sweep_strategies.py
python scripts\optimize_champions_3000.py
```

## Aether economico

O pesquisador Aether roda em modo economico por padrao (`AETHER_ECONOMY_MODE=1`). Nesse modo ele:

- usa modelos/effort menores por papel;
- limita saida LLM para 700 tokens por chamada;
- gera no maximo 1 hipotese LLM por rodada;
- aplica cooldown de 45 minutos entre geracoes LLM;
- usa mutacoes parametricas antes de chamar LLM;
- reaproveita sessoes Claude Code por papel/system/modelo para nao abrir chat novo a cada chamada;
- guarda traces menores no SQLite.

Variaveis uteis para controlar custo:

| Variavel | Padrao | Efeito |
|---|---:|---|
| `AETHER_ECONOMY_MODE` | `1` | liga/desliga modo economico |
| `AETHER_LLM_ENABLED` | `1` | `0` desliga todas chamadas LLM |
| `AETHER_LLM_MAX_TOKENS` | `700` | teto geral de resposta LLM |
| `AETHER_HYPOTHESIS_MAX_TOKENS` | `700` | teto do gerador de hipoteses |
| `AETHER_LLM_MAX_NEW` | `1` | maximo de hipoteses por chamada |
| `AETHER_LLM_HYPOTHESIS_COOLDOWN_MINUTES` | `45` | intervalo minimo entre geracoes LLM |
| `AETHER_LLM_CONTEXT_ROWS` | `120` | maximo de nos lidos para resumir contexto |
| `AETHER_LLM_CONTEXT_PRUNED` | `6` | podadas recentes enviadas no prompt |
| `AETHER_LLM_CONTEXT_VALIDATED` | `4` | validadas recentes enviadas no prompt |
| `AETHER_LLM_MUTATIONS_ENABLED` | `0` | habilita LLM para mutacoes quando parametro esgotar |
| `AETHER_LLM_VALIDATOR_MAX_PER_CYCLE` | `1` | revisoes LLM por ciclo |
| `AETHER_LLM_VALIDATOR_POLL_SECONDS` | `900` | intervalo do validator LLM |
| `AETHER_CLAUDE_SESSION_REUSE` | `1` | usa `--resume` no Claude Code quando possivel |
| `AETHER_CLAUDE_SESSION_MAX_CALLS` | `20` | rotaciona a sessao apos N chamadas |

O cache de sessoes fica no runtime do Aether em `llm_sessions.json`. Ele separa sessoes por papel, modelo, effort, workdir e system prompt; assim o pesquisador reaproveita contexto sem misturar funcoes diferentes.

Para rodar quase sem custo de token:

```powershell
$env:AETHER_LLM_ENABLED="0"
python -m src.research.aether.server
```

Para uma rodada pesada pontual:

```powershell
$env:AETHER_ECONOMY_MODE="0"
$env:AETHER_LLM_MAX_NEW="3"
$env:AETHER_LLM_HYPOTHESIS_COOLDOWN_MINUTES="0"
python -m src.research.aether.server
```

## Deploy simples

Em VPS, prefira:

1. Criar `.venv`.
2. Copiar `.env` com `PAPER_TRADING=true`.
3. Rodar `python -m src.main` em `systemd`, `tmux`, `screen` ou PM2.
4. Montar/persistir `src/logs` se estiver em Docker.
5. Monitorar `src/logs/trading_bot.log` e Telegram.

## Troubleshooting

| Sintoma | Checar |
|---|---|
| Bot nao inicia | `.env`, dependencias e Python correto |
| Erro Binance | chave, secret, permissao spot, IP allowlist e relogio |
| Sem Telegram | token, chat id e rede |
| Sem trades | estrategia, timeframe, filtros, saldo e `PAPER_TRADING` |
| Logs gigantes | pare o bot e limpe `src/logs/*.log` |
| JSON corrompido | validar arquivos em `src/logs/paper_balance` e `src/logs/research` |

## Limpeza segura

Pode apagar sem medo na maioria dos casos:

```powershell
Remove-Item .pytest_cache -Recurse -Force
Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Remove-Item src\logs\*.log -Force
```

Antes de apagar `src/logs/research/strategy_catalog.json` ou `src/logs/paper_balance/state.json`, confirme que voce nao precisa do historico atual.

# Plano de agentes do HAPPYNATIONBOT

Este projeto evolui para uma arquitetura com agentes locais. Cada agente tem **um único assunto**, **um nome próprio** (pra log/Telegram) e **regras claras de o que pode e não pode fazer**.

Princípios:

- Cada agente começa em modo somente leitura.
- Toda ação automática precisa de flag explícita no `.env`.
- Nenhum agente aprova a si mesmo.
- Findings têm severidade, motivo, ativo (quando existir), ação recomendada e metadados.
- Findings têm `first_seen_at` e `last_seen_at` (diff entre execuções).

## Fluxo central

```text
Ideia -> Caçador -> Catálogo -> Juiz -> Promotor -> Paper -> Produção
                                          ^
                                          |
                            Farejador / Meteorologista / Sentinela
```

## Lista oficial de agentes

### Já implementados

| Agente | Nome | Assunto |
|---|---|---|
| `RiskAgent` | **Sentinela** | Risco operacional do paper |
| `PerformanceAgent` | **Analista** | Performance por ativo |
| `ResearchAgent` | (será absorvido pelo Caçador) | Manifest ativo |
| `StrategyValidatorAgent` | **Juiz** | Auditoria de aprovadas e manifest ativo |
| `StrategyResearcherAgent` | **Caçador** | Auditoria do catálogo e saturação de templates |
| `AetherResearcherAgent` | **Caçador IA** | Gera hipóteses de estratégia via Claude/Aether |
| `PortfolioAgent` | **Gerente** | Exposição, concentração e pesos no paper |
| `PolicyAgent` | **Comandante** | Decisão consolidada por ativo |

### Tier 1 — Proteção (implementar primeiro)

| Agente | Nome | Assunto |
|---|---|---|
| `ConfigAgent` | **Vigia** | `.env`, modo real, limites de risco |
| `SecurityAgent` | **Cofre** | `.gitignore`, chaves, confirmação dupla |
| `HealthAgent` | **Médico** | JSON corrompido, log gigante, estado inválido |

### Tier 2 — Estratégias

| Agente | Nome | Assunto |
|---|---|---|
| `StrategyResearcherAgent` | **Caçador** | Gera candidatas, audita catálogo |
| `AetherResearcherAgent` | **Caçador IA** | Gera hipóteses por IA; grava só com flag |
| `StrategyValidatorAgent` | **Juiz** | Aprova/reprova com critérios duros |
| `StrategyDeploymentAgent` | **Promotor** | Promoção validated → paper → produção |

### Tier 3 — Runtime e qualidade

| Agente | Nome | Assunto |
|---|---|---|
| `ExecutionAgent` | **Maquinista** | Threads, loop, ordens travadas |
| `DataQualityAgent` | **Inspetor** | Gaps, candles repetidos, downtime |
| `DriftAgent` | **Farejador** | Desvio live vs backtest |
| `MarketRegimeAgent` | **Meteorologista** | Mudança de regime de mercado |

### Tier 4 — Consolidação

| Agente | Nome | Assunto |
|---|---|---|
| `PortfolioAgent` | **Gerente** | Exposição, concentração, pesos |
| `PolicyAgent` | **Comandante** | Decisão final por ativo |

### Não vão ser implementados

- `StrategyAgent` (lint de estratégia) — vai pra CI/teste, não runtime.
- `DeployAgent` (checklist único) — vira script, não agente.

---

## Detalhamento por agente

### Vigia — `ConfigAgent`

**Função:** validar configuração antes de qualquer operação.

**Responsabilidades:**
- Validar `.env` (variáveis obrigatórias).
- Bloquear modo real sem `REAL_TRADING_CONFIRM=true`.
- Verificar limites de risco (`MAX_DAILY_LOSS_PCT`, `MAX_DRAWDOWN_PCT`).
- Alertar se `src/main.py` tiver configuração hardcoded perigosa.

**Findings:**
- `critical`: `PAPER_TRADING=false` sem confirmação.
- `critical`: limite de risco ausente em modo real.
- `warning`: variável esperada faltando.

**Não faz:** alterar `.env`, parar o bot.

---

### Cofre — `SecurityAgent`

**Função:** proteção contra exposição de segredo e erro humano.

**Responsabilidades:**
- Verificar `.env` no `.gitignore`.
- Detectar chave real versionada acidentalmente.
- Exigir confirmação dupla pra dinheiro real.
- Alertar se API key parece ter permissão de trade real.

**Findings:**
- `critical`: chave em arquivo versionado.
- `critical`: `.env` não está no `.gitignore`.
- `warning`: API key sem restrição de IP.

**Não faz:** revogar chave, alterar permissão Binance.

---

### Médico — `HealthAgent`

**Função:** saúde operacional do processo e dos arquivos de estado.

**Responsabilidades:**
- Validar JSON de `state.json`, `trailing_db.json`, `active_strategies.json`.
- Monitorar tamanho de logs em `src/logs/`.
- Alertar se algum log tem erro repetido.
- Recomendar rotação de logs.

**Findings:**
- `critical`: `trailing_db.json` ou `state.json` corrompidos.
- `warning`: log >50 MB.
- `warning`: erro repetido em `thread_debug.log`.

**Não faz:** apagar log, recuperar JSON corrompido sozinho.

---

### Caçador — `StrategyResearcherAgent`

**Função:** gerar e catalogar candidatas. Absorve o `ResearchAgent` atual.

**Responsabilidades:**
- Ler templates em `src/research/templates.py`.
- Gerar candidatas via grid, random, mutation, crossover.
- Marcar origem da candidata.
- Evitar duplicar combinação já testada.
- Detectar catálogo vazio, velho ou saturado de rejeições.
- Sugerir templates promissores baseado em aprovadas.

**Entradas:**
- `src/research/templates.py`
- `src/logs/research/strategy_catalog.json`
- `src/logs/research/strategy_research_results.json`
- `src/logs/research/approved_strategies.csv`

**Findings:**
- `info`: catálogo OK, com pesquisa suficiente.
- `warning`: catálogo sem aprovadas.
- `warning`: template saturado de rejeições.
- `info`: sugerir templates promissores.

**Não faz:** aprovar estratégia, alterar `active_strategies.json`, deploy.

---

### Juiz — `StrategyValidatorAgent`

**Função:** decidir se uma candidata pode ir pra paper.

**Critérios duros (todos obrigatórios):**
- `total_trades >= 20`
- `profit_factor >= 1.15`
- `max_drawdown_pct <= 25`
- `max_loss_streak <= 4`
- `consistency_score >= 55`
- `windows_passed / windows_total >= 0.6` (walk-forward)

**Critério estatístico extra:**
- Rejeita se intervalo de confiança 95% do PF cruzar 1.0.

**Estados emitidos:**
- `validated` — passou tudo, pronta pra paper.
- `rejected` — falhou ≥1 critério, com motivo.
- `inconclusive` — sem amostra suficiente.

**WR isolado NÃO é critério.** Só é considerado em conjunto com PF.

**Findings:**
- `critical`: estratégia ativa não aprovada no catálogo.
- `warning`: aprovada sem métricas suficientes.
- `warning`: aprovada que viola critério.
- `info`: tudo passa.

**Não faz:** mandar pra dinheiro real, remover ativa, usar ROI sozinho.

---

### Promotor — `StrategyDeploymentAgent`

**Função:** promoção controlada **validated → paper_approved → production_ready**.

**Responsabilidades:**
- Promover `validated` → `paper_approved` quando paper rodou ≥N trades com PF ≥X% do backtest.
- Rebaixar `paper_approved` se Farejador detectar drift.
- Manter níveis: `candidate` → `validated` → `paper_approved` → `production_ready`.
- Bloquear promoção pra `production_ready` sem `REAL_TRADING_CONFIRM=true`.
- Registrar histórico em `src/logs/deployment/promotions.json`.

**Findings:**
- `info`: candidata pronta pra promoção.
- `warning`: estratégia em paper há muito tempo sem promoção.
- `critical`: estratégia em produção com drift detectado.

**Não faz:** aprovar candidata (é do Juiz), rodar backtest (é do Caçador).

---

### Maquinista — `ExecutionAgent`

**Função:** integridade do loop principal e das ordens.

**Responsabilidades:**
- Verificar threads vivas por ativo.
- Detectar travamento (sem ciclo novo por >2 ciclos).
- Detectar exceções repetidas em `thread_debug.log`.
- Detectar divergência entre posição esperada e saldo real.
- Alertar quando ordem fica aberta tempo demais.

**Findings:**
- `warning`: ativo sem atualização há >2 ciclos.
- `critical`: exceção repetida em thread.
- `critical`: divergência entre posição e saldo.

**Não faz:** matar thread, cancelar ordem real sozinho.

---

### Inspetor — `DataQualityAgent`

**Função:** qualidade dos dados de mercado.

**Responsabilidades:**
- Detectar gaps em candles.
- Detectar candles repetidos.
- Detectar janelas de downtime de exchange.
- Validar indicadores (NaN, Inf).

**Findings:**
- `warning`: gap de candles em ativo X.
- `warning`: indicador retornou NaN.
- `critical`: dado corrompido em uso por estratégia ativa.

**Não faz:** corrigir dado, pausar estratégia sozinho.

---

### Farejador — `DriftAgent`

**Função:** detectar desvio entre performance ao vivo e expectativa do backtest.

**Responsabilidades:**
- Comparar PF live vs backtest por ativo.
- Comparar DD live vs backtest.
- Aplicar teste estatístico (KS test ou control chart).
- Alertar antes do prejuízo grande.

**Findings:**
- `warning`: drift moderado em ativo X (>10%).
- `critical`: drift severo em ativo X (>25%).
- `info`: estratégias dentro da expectativa.

**Não faz:** rebaixar estratégia (é do Promotor), pausar trading.

---

### Meteorologista — `MarketRegimeAgent`

**Função:** detectar regime de mercado e avisar quando mudou.

**Responsabilidades:**
- Classificar regime atual: `bull` / `bear` / `lateral` / `vol_alta` / `vol_baixa`.
- Comparar regime atual com regime em que cada estratégia foi validada.
- Recomendar revalidação quando há mismatch.

**Findings:**
- `warning`: regime mudou, N estratégias precisam de revalidação.
- `info`: regime estável.

**Não faz:** revalidar sozinho (é do Juiz).

---

### Gerente — `PortfolioAgent`

**Função:** composição dos ativos e exposição total.

**Responsabilidades:**
- Calcular exposição por ativo em USDT.
- Avaliar concentração.
- Recomendar redução de ativos ruins.
- Recomendar pesos por performance e risco.

**Findings:**
- `warning`: ativo ruim com peso alto.
- `warning`: concentração >X% em um único ativo.
- `info`: alocação balanceada.

**Não faz:** ajustar peso sozinho, abrir/fechar posição.

---

### Comandante — `PolicyAgent`

**Função:** consolidar findings e emitir decisão única por ativo.

**Responsabilidades:**
- Ler relatório completo dos outros agentes.
- Aplicar matriz de decisão por ativo:
  - Sentinela + Farejador vermelhos → `pause`
  - Maquinista crítico → `pause`
  - Múltiplos warnings persistentes → `reduce`
  - Todos verdes → `continue`
- Registrar decisão em `src/logs/agents/policy_decisions.json`.

**Findings:**
- `critical`: decisão `kill` em ativo X.
- `warning`: decisão `pause` ou `reduce` em ativo X.
- `info`: decisão `continue`.

**Não faz:** executar a decisão (apenas recomenda na primeira versão).

---

## Infra antes de começar

1. **Diff entre relatórios:** concluído. `AgentFinding` agora tem `first_seen_at`, `last_seen_at`, `status` e `fingerprint`; o runner compara com o relatório anterior e gera `diff_counts`.
2. **Telegram:** pendente. Alerta automático para findings `warning` e `critical` novos (sem isso ninguém lê).
3. **Identidade no log:** pendente. Cada finding deve mostrar o nome próprio do agente (`[Vigia]`, `[Juiz]`, etc.) além do `agent_id`.

## Ordem de implementação

1. **Vigia** (`ConfigAgent`)
2. **Cofre** (`SecurityAgent`)
3. **Médico** (`HealthAgent`)
4. **Promotor** (`StrategyDeploymentAgent`)
5. **Maquinista** (`ExecutionAgent`)
6. **Inspetor** (`DataQualityAgent`)
7. **Farejador** (`DriftAgent`)
8. **Meteorologista** (`MarketRegimeAgent`)
9. Telegram pra `warning` / `critical`

Concluído nesta etapa:

- **Gerente** (`PortfolioAgent`): primeira versão criada em `src/agents/portfolio_agent.py`.
- **Comandante** (`PolicyAgent`): primeira versão criada em `src/agents/policy_agent.py`.
- `AgentOrchestrator` agora suporta `run_with_context(findings)` para agentes consolidadores.

Concluído nesta etapa:

- **Juiz** (`StrategyValidatorAgent`): primeira versão de auditoria criada em `src/agents/strategy_validator_agent.py`.
- **Caçador** (`StrategyResearcherAgent`): primeira versão de auditoria criada em `src/agents/strategy_researcher_agent.py`.
- **Caçador IA** (`AetherResearcherAgent`): primeira versão criada em `src/agents/aether_researcher_agent.py`; por padrão apenas mostra preview e só escreve no catálogo com `AETHER_RESEARCHER_WRITE_ENABLED=1`.

## Como criar um novo agente

1. Criar arquivo em `src/agents/<nome>_agent.py`.
2. Implementar classe com:
   - `name` (id técnico, ex: `config_agent`)
   - `display_name` (nome próprio, ex: `Vigia`)
   - `run() -> list[AgentFinding]`
3. Registrar em `build_default_orchestrator()` em `src/agents/runner.py`.
4. Adicionar testes em `tests/test_agents.py`.
5. Rodar:

```bash
python -m pytest tests/test_agents.py -q
python -m src.agent_runner
```

## Regras de segurança

- Agente novo começa em modo somente leitura.
- Toda ação automática precisa de flag explícita no `.env`.
- `critical` pode recomendar pausa, mas não opera sozinho na primeira versão.
- Toda recomendação tem motivo, ativo e metadados.
- Relatórios são gravados em `src/logs/agents/`.

## Exemplo de saída no Telegram / log

```text
[Vigia] critical: PAPER_TRADING=false sem REAL_TRADING_CONFIRM
[Cofre] warning: .env não está no .gitignore
[Médico] critical: state.json corrompido
[Caçador] info: 14 candidatas novas no catálogo
[Juiz] warning: BTCUSDT aprovada mas DD=28% > limite 25%
[Promotor] info: ETHUSDT pronta pra promoção paper → real
[Sentinela] critical: drawdown diário 5.2% acima do limite
[Analista] warning: SOLUSDT com 30 trades e PF 0.92
[Farejador] warning: SOLUSDT performance ao vivo divergiu do backtest em 18%
[Meteorologista] warning: regime mudou de bull pra lateral, revalidar 3 estratégias
[Maquinista] critical: thread ADAUSDT sem ciclo há 4 minutos
[Inspetor] warning: gap de 3 candles em DOTUSDT
[Gerente] warning: BTCUSDT com 62% da exposição
[Comandante] decisão: ADAUSDT → pause (Sentinela+Farejador+Analista vermelhos)
```

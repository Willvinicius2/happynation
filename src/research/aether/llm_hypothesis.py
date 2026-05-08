from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .campaign_constants import CURRENT_CAMPAIGN_ID
from .campaigns import get_active_campaign_id
from .config import TIMEFRAMES
from .db import add_event
from .llm_runtime import call_llm_agent
from .llm_traces import record_llm_trace
from .repository import role_for_timeframe, upsert_node


ALLOWED_ENGINES = {
    "rsi", "bollinger", "ma_crossover", "donchian", "supertrend",
    "macd", "stochastic", "adx", "keltner", "vwap_dev",
    "roc", "williams_r", "cci", "mfi", "obv_trend",
    "psar", "atr_breakout", "zscore",
    # Tier B: engines estruturais (regime/vol/price-action) — saem do mundo
    # de indicadores 1990 e abrem espaco causal genuinamente novo.
    "regime_filter", "vol_compression", "structure_break",
}

SYSTEM_PROMPT = """Voce e o Hypothesis Generator do Aether — pesquisador de alpha quantitativo em cripto.

MISSAO: Gerar hipoteses TESTAVEIS em backtest local que tenham chance real de virar alpha duravel — nao apenas curve-fit ao historico. Priorize NOVIDADE estrutural sobre re-tunning de parametro.

REALIDADE DO MERCADO CRIPTO (2023-2026):
- BTC dominou o regime: bull 2024-Q1 (ETF), correcao Q2-Q3, bull Q4 (eleicao US), ATH ~109k. Alts seguem com beta alto.
- Microestrutura: spread/slippage real >5bps em alts, fees 10bps. Estrategia que precisa de 50+ trades/mes em TF longo provavelmente nao executa lucrativa.
- Regime-shift: mean-revert funciona em range, falha em trend forte. Trend-follow oposto. Sem filtro de regime, qualquer estrategia tem 50% de janelas perdedoras.
- Cross-symbol: estrategia que so funciona em BTCUSDT 1d e morre em ETH/SOL/XRP eh overfit. Generalizacao real exige robustez cross-asset.
- Ineficiencias documentadas: momentum cripto (Chen 2022), short-term reversal pos-pump (Liu 2023), volatility carry, weekend effect, funding-rate persistence em perpetuos, basis spot-perp, on-chain accumulation pre-rallies.

MODOS DE FALHA QUE VOCE DEVE EVITAR (causas frequentes de rejeicao do Critic):
- Cross-symbol falha: hipotese exclusiva a um regime/ativo. EVITE: parametros calibrados especificamente pra BTC 1d.
- Walk-forward falha: parametros que so funcionam em uma janela. EVITE: short_window=3, long_window=15 (over-tuned), prefira faixas estruturais (5-21, 20-50).
- Regime falha: backtest todo bull, morre em bear/range. EVITE: long-only sem filtro de tendencia em TF curto.
- Trade-starved: indicador extremo (RSI>95) que nunca dispara. EVITE: thresholds que matam volume de trades.
- Multi-timeframe falha: hipotese acidental do TF. EVITE: lookbacks fixos sem normalizacao por TF.

HIPOTESE BOA (exemplo de criterio):
- Mecanismo causal nomeado: "depois de spike de volume + close fora da banda, ha 60% de probabilidade de fade nas 4h seguintes porque whales entrando enfrentam liquidez fina".
- Invalidation explicita: "se volume nao excede 2x media OU se close volta dentro da banda no proximo candle".
- Parametros estruturais (faixas, nao numeros magicos): atr_mult>1.5 robusto a 1.5-3.0.
- Hipotese de robustez: "deve funcionar em qualquer alt liquido com ADV>$50M".

HIPOTESE RUIM (anti-padrao):
- "MA cross 3/15 em BTCUSDT 1d com short=5" — over-tuned, mono-asset, sem mecanismo causal real.
- "RSI>70 vende" — sem filtro de regime, falha em bull persistente.
- "Bollinger touch reverte" — depende de regime ranging, sem detector de regime.

REGRAS DE OUTPUT:
- Responda SOMENTE JSON valido (nada antes ou depois).
- Slug em ascii minusculo, sem acentos, hifens.
- Mecanismo causal eh OBRIGATORIO — escreva a tese economica/microestrutural em 1-2 frases.
- Invalidation eh OBRIGATORIA — quando voce abandona a hipotese.
- Diversifique mecanismo causal (momentum/mean-rev/breakout/regime/volume/volatilidade), nao so engine.
"""

USER_PROMPT = """Gere ate {max_new} hipoteses novas para uma arvore RAIO.

Engines e params:
- rsi(period,oversold,overbought,long_only)
- bollinger(window,num_std,long_only)
- ma_crossover(short_window,long_window,long_only)
- donchian(entry_window,exit_window,long_only)
- supertrend(atr_period,atr_mult,long_only)
- macd(fast,slow,signal,long_only) — momentum via EMA cross
- stochastic(period,smooth_k,smooth_d,oversold,overbought,long_only) — oscilador %K/%D
- adx(period,threshold,long_only) — filtro DI+/DI- de forca de tendencia
- keltner(window,atr_mult,long_only) — bandas via ATR
- vwap_dev(window,z_threshold,long_only) — desvio z da VWAP rolling
- roc(period,upper,lower,long_only) — momentum % puro (rate of change)
- williams_r(period,oversold,overbought,long_only) — oscilador (oversold tipico=-80, overbought=-20)
- cci(period,threshold,long_only) — Commodity Channel Index com desvio absoluto
- mfi(period,oversold,overbought,long_only) — Money Flow Index (RSI ponderado por volume)
- obv_trend(window,long_only) — OBV cruza media movel (volume acumulado)
- psar(af_step,af_max,long_only) — Parabolic SAR (trend follow com flip)
- atr_breakout(atr_period,atr_mult,long_only) — close ultrapassa close[-1] +/- mult*ATR
- zscore(window,threshold,long_only) — z-score do close (mean reversion estatistica)
- regime_filter(method,ma_window,slope_window,vol_window,vol_percentile_max,long_only) — ESTRUTURAL: alinha sinal ao regime. method='trend_align'|'low_vol_breakout'|'high_vol_fade'. Combate falhas de regime do Critic.
- vol_compression(atr_window,percentile_window,percentile_max,breakout_window,long_only) — ESTRUTURAL: ATR no quartil baixo + rompimento. Captura release de energia pos-compressao (mais robusto que Bollinger squeeze).
- structure_break(swing_lookback,confirm_bars,long_only) — ESTRUTURAL: rompimento de swing high/low (PIVOTS reais, nao janela fixa). Niveis testados pelo mercado, generaliza cross-asset.

PRIORIZE engines com BAIXA frequencia na arvore E os engines ESTRUTURAIS (regime_filter, vol_compression, structure_break) — eles abrem espaco causal novo onde indicadores classicos saturam. Varie mecanismo causal (momentum, mean revert, breakout, regime, volume, volatilidade, filtro de tendencia, fade contrarian, price-action estrutural).

Estado resumido para diversificar:
{exploration_context}

Regras: nao repetir podadas/validadas; se dominante={dominance_pct}%, evite a familia dominante; mude mecanismo causal e parametros de forma estrutural.

JSON:
{{
  "hypotheses": [
    {{
      "slug": "nome-curto-sem-acentos",
      "name": "Nome curto",
      "hypothesis": "Tese testavel",
      "causal_mechanism": "Mecanismo causal",
      "invalidation": "Quando cortar o ramo",
      "engines": ["rsi"],
      "probe_specs": [
        {{"engine": "rsi", "params": {{"period": 14, "oversold": 30, "overbought": 70, "long_only": false}}}}
      ],
      "base_score": 0.55
    }}
  ]
}}
"""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Two-stage Theorist -> Engineer (Tier B, opt-in via AETHER_HYPOTHESIS_TWO_STAGE=1)
# ---------------------------------------------------------------------------

THEORIST_SYSTEM_PROMPT = """Voce e o Theorist do Aether — pesquisador de teoria de alpha cripto.

MISSAO: Pensar PROFUNDAMENTE sobre microestrutura cripto, regimes e ineficiencias documentadas, e propor 1 TESE causal genuinamente nova (ou angulo nao-explorado de uma conhecida) que possa virar estrategia.

NAO se preocupe com codigo, parametros ou JSON. So pensa. Texto livre.

CONTEXTO:
- Cripto 2023-2026: BTC dominou via ETF (Q1/24), correcao Q2/24, rally Q4/24 com eleicao US, ATH 109k. Alts beta alto, drift sazonal de altseason em Q1.
- Microestrutura: spread 5-10bps em alts, fees 10bps. Liquidez assimetrica entre dia/noite e dias da semana. Funding-rate em perpetuos persiste (positive funding em bull, negative em bear).
- Ineficiencias documentadas: momentum 1-12 semanas (Chen 2022), short-term reversal 1-3 dias pos-pump (Liu 2023), volatility carry, weekend effect (Ferreira 2021), MVRV bottom signal, exchange netflow, Coinbase premium em rallies US-led, basis spot-perp, cluster de liquidacoes que viram pivot.
- O que NAO funciona consistentemente: indicadores 1980 calibrados pontualmente, mean-reversion sem filtro de regime, momentum sem corte de volatilidade.

ESTRUTURA da sua resposta (texto livre, ~600-1000 palavras):
1. INEFICIENCIA escolhida — nomeie e explique em 2-3 frases o porque ela existe (mecanismo economico ou microestrutural).
2. EVIDENCIA — pontos concretos do mercado/literatura que sustentam (ou contestam) a tese.
3. SETUP CAUSAL — quando exatamente o sinal deveria disparar (condicoes A, B, C). Pense como se estivesse explicando pra um trader humano.
4. INVALIDACAO — sob que condicoes voce ABANDONARIA essa tese.
5. ROBUSTEZ esperada — em que regimes ela deve sobreviver e em quais nao. Falar de cross-symbol, cross-TF, cross-regime.

NAO repita teses ja exploradas. NAO proponha "MA cross". NAO proponha "RSI > 70 vende". Mire em algo que um trader experiente acharia interessante."""


THEORIST_USER_PROMPT = """Estado atual da arvore de pesquisa:
{exploration_context}

Modos de falha mais comuns do Critic ultimamente:
{critic_failures}

PROPONHA uma tese nova alinhada com ineficiencias reais documentadas em cripto, evitando os modos de falha acima. Texto livre, sem JSON."""


ENGINEER_SYSTEM_PROMPT = """Voce e o Engineer do Aether — traduz uma TESE causal em probe_specs JSON usando os engines disponiveis.

ENGINES (mesmo conjunto do Hypothesis Generator). Use combo=multi-engine quando a tese exigir 2+ filtros. Prefira engines ESTRUTURAIS (regime_filter, vol_compression, structure_break) quando a tese mencionar regime, compressao, ou pivot.

REGRAS:
- Responda SOMENTE JSON valido.
- Mecanismo causal e invalidation devem REFLETIR a tese, nao ser genericos.
- Parametros estruturais (faixas, nao numeros magicos pontuais)."""


ENGINEER_USER_PROMPT = """TESE proposta pelo Theorist:
{theory}

Engines e params:
{engine_catalog}

Restricoes:
- Familia dominante: {dominance_pct}%; se >=30%, EVITE single-engine dessa familia.
- Estado da arvore: {context_snippet}

Gere ate {max_new} hipoteses concretas que IMPLEMENTAM a tese acima. JSON:
{{
  "hypotheses": [
    {{
      "slug": "nome-curto-sem-acentos",
      "name": "Nome curto (≤80 chars)",
      "hypothesis": "Tese testavel (resumo da TESE acima, contextualizada)",
      "causal_mechanism": "Mecanismo causal especifico desta variante",
      "invalidation": "Quando cortar o ramo",
      "engines": ["engine_principal"],
      "probe_specs": [{{"engine": "...", "params": {{...}}}}],
      "base_score": 0.55
    }}
  ]
}}
"""


def _engine_catalog_for_engineer() -> str:
    """Versao compacta da lista de engines pro Engineer (Stage 2)."""
    return (
        "rsi(period,oversold,overbought,long_only) | "
        "bollinger(window,num_std,long_only) | "
        "ma_crossover(short_window,long_window,long_only) | "
        "donchian(entry_window,exit_window,long_only) | "
        "supertrend(atr_period,atr_mult,long_only) | "
        "macd(fast,slow,signal,long_only) | "
        "stochastic(period,smooth_k,smooth_d,oversold,overbought,long_only) | "
        "adx(period,threshold,long_only) | "
        "keltner(window,atr_mult,long_only) | "
        "vwap_dev(window,z_threshold,long_only) | "
        "roc(period,upper,lower,long_only) | "
        "williams_r(period,oversold,overbought,long_only) | "
        "cci(period,threshold,long_only) | "
        "mfi(period,oversold,overbought,long_only) | "
        "obv_trend(window,long_only) | "
        "psar(af_step,af_max,long_only) | "
        "atr_breakout(atr_period,atr_mult,long_only) | "
        "zscore(window,threshold,long_only) | "
        "regime_filter(method=trend_align|low_vol_breakout|high_vol_fade,ma_window,slope_window,vol_window,vol_percentile_max,long_only) | "
        "vol_compression(atr_window,percentile_window,percentile_max,breakout_window,long_only) | "
        "structure_break(swing_lookback,confirm_bars,long_only)"
    )


def _two_stage_generate(
    *,
    context_block: str,
    similar_block: str,
    critic_block: str,
    dominance_pct: float,
    max_new: int,
    caller: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Roda Theorist (texto livre) -> Engineer (JSON). Retorna (engineer_response, theorist_response)."""
    failures_text = critic_block.strip() or "(nenhuma rejeicao recente registrada)"
    theorist_prompt = THEORIST_USER_PROMPT.format(
        exploration_context=context_block + similar_block,
        critic_failures=failures_text,
    )
    theorist_resp = caller(
        "hypothesis_generator",
        system=THEORIST_SYSTEM_PROMPT,
        prompt=theorist_prompt,
        max_tokens=_env_int("AETHER_THEORIST_MAX_TOKENS", 3000),
    )
    if not theorist_resp.get("ok"):
        return theorist_resp, theorist_resp  # mesmo objeto pra propagar erro
    theory = str(theorist_resp.get("text") or "")[:6000]
    engineer_prompt = ENGINEER_USER_PROMPT.format(
        theory=theory,
        engine_catalog=_engine_catalog_for_engineer(),
        dominance_pct=f"{dominance_pct:.0f}",
        context_snippet=context_block[:600],
        max_new=max_new,
    )
    engineer_resp = caller(
        "hypothesis_generator",
        system=ENGINEER_SYSTEM_PROMPT,
        prompt=engineer_prompt,
        max_tokens=_env_int("AETHER_ENGINEER_MAX_TOKENS", 2000),
    )
    return engineer_resp, theorist_resp


def _collect_exploration_context(conn, campaign_id: str):
    """Returns (context_block, dominant_engine, dominance_pct, family_active, family_validated)
    for the prompt + diversity gate + per-family cap."""
    rows = conn.execute(
        """
        SELECT id, name, status, score, prune_reason, agent_role, metrics_json, tags_json, updated_at
        FROM nodes
        WHERE campaign_id=? AND id != 'root' AND id NOT LIKE 'tf:%'
        ORDER BY updated_at DESC LIMIT ?
        """,
        (campaign_id, _env_int("AETHER_LLM_CONTEXT_ROWS", 120)),
    ).fetchall()

    family_active: dict[str, int] = {}
    family_validated: dict[str, int] = {}
    pruned_recent: list[dict[str, Any]] = []
    validated_recent: list[dict[str, Any]] = []

    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except Exception:
            metrics = {}
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except Exception:
            tags = []
        engine = ""
        best = metrics.get("best_probe") or {}
        if isinstance(best, dict):
            engine = str(best.get("engine") or "")
        if not engine:
            specs = metrics.get("probe_specs") or []
            if isinstance(specs, list) and specs and isinstance(specs[0], dict):
                engine = str(specs[0].get("engine") or "")
        if not engine:
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("engine:"):
                    engine = tag.split(":", 1)[1]
                    break
        engine = (engine or "unknown").lower()
        status = row["status"]

        if status not in ("pruned", "paused", "archive"):
            family_active[engine] = family_active.get(engine, 0) + 1
        if status == "validated":
            family_validated[engine] = family_validated.get(engine, 0) + 1

        if status in ("pruned", "paused") and len(pruned_recent) < _env_int("AETHER_LLM_CONTEXT_PRUNED", 6):
            pruned_recent.append({
                "name": str(row["name"] or row["id"]),
                "engine": engine,
                "reason": str(row["prune_reason"] or "sem motivo"),
            })

        if status == "validated" and len(validated_recent) < _env_int("AETHER_LLM_CONTEXT_VALIDATED", 4):
            validated_recent.append({
                "name": str(row["name"] or row["id"]),
                "engine": engine,
                "score": float(row["score"] or 0.0),
            })

    total_active = sum(family_active.values()) or 1
    sorted_families = sorted(family_active.items(), key=lambda kv: kv[1], reverse=True)
    dominant_engine = sorted_families[0][0] if sorted_families else ""
    dominance_pct = (sorted_families[0][1] / total_active * 100.0) if sorted_families else 0.0

    family_lines = ", ".join(f"{name}={count}" for name, count in sorted_families[:6]) or "(nenhuma)"
    pruned_lines = (
        "\n".join(f"- {h['name'][:60]} ({h['engine']}): {h['reason'][:80]}" for h in pruned_recent)
        or "- (nenhuma poda recente)"
    )
    validated_lines = (
        "\n".join(f"- {h['name'][:60]} ({h['engine']}, score {h['score']:.2f})" for h in validated_recent)
        or "- (nenhuma validada ainda)"
    )

    saturated_lines = (
        ", ".join(
            f"{name}={family_active.get(name,0)}a/{family_validated.get(name,0)}v"
            for name in sorted(set(family_active) | set(family_validated))
        )
        or "(nenhuma)"
    )
    used_engines = set(family_active) | set(family_validated)
    virgin_engines = [e for e in sorted(ALLOWED_ENGINES) if e not in used_engines and e != "combo"]
    virgin_lines = ", ".join(virgin_engines) if virgin_engines else "(nenhuma — todas exploradas)"
    block = (
        f"Familias: {family_lines}\n"
        f"Dominante: {dominant_engine or 'n/a'} ({dominance_pct:.0f}%)\n"
        f"Saturacao por familia (ativas/validadas): {saturated_lines}\n"
        f"ENGINES VIRGENS (zero uso, PRIORIZE ESTAS): {virgin_lines}\n"
        f"Podadas recentes:\n{pruned_lines}\n"
        f"Validadas recentes:\n{validated_lines}"
    )
    return block, dominant_engine, dominance_pct, family_active, family_validated


def _recent_critic_failures(conn, campaign_id: str, *, days: int = 7, top_n: int = 5) -> list[tuple[int, str]]:
    """Top razoes de rejeicao do Critic dos ultimos N dias.

    Retorna lista [(count, reason_short)]. Usado pra injetar no prompt e fechar
    o loop: o pesquisador deixa de propor hipoteses que ja sabe que serao
    rejeitadas pelos mesmos motivos.
    """
    try:
        rows = conn.execute(
            """
            SELECT json_extract(payload_json, '$.reason') AS reason, COUNT(*) AS n
            FROM events
            WHERE type='critic_rejected'
              AND campaign_id=?
              AND ts > datetime('now', ?)
            GROUP BY substr(json_extract(payload_json, '$.reason'), 1, 60)
            ORDER BY n DESC LIMIT ?
            """,
            (campaign_id, f"-{int(days)} days", int(top_n)),
        ).fetchall()
    except Exception:
        return []
    out: list[tuple[int, str]] = []
    for row in rows:
        reason = row["reason"] if hasattr(row, "keys") else row[0]
        n = row["n"] if hasattr(row, "keys") else row[1]
        if not reason:
            continue
        out.append((int(n), str(reason)[:160]))
    return out


def _last_cycle_useful_ratio(conn, campaign_id: str) -> float:
    """Fracao de hipoteses uteis no ultimo ciclo do hypothesis_generator.

    util = created / (created + rejected_diversity + rejected_family_cap +
    rejected_duplicate). Usado pra adaptive cooldown -- se o ciclo anterior
    teve baixa utilidade (LLM gerando redundancia), aumenta o cooldown e
    evita queimar mais $.
    """
    row = conn.execute(
        """
        SELECT payload_json FROM events
        WHERE type='llm_research_completed' AND campaign_id=?
        ORDER BY ts DESC LIMIT 1
        """,
        (campaign_id,),
    ).fetchone()
    if not row:
        return 1.0
    try:
        payload = json.loads(row["payload_json"] if hasattr(row, "keys") else row[0] or "{}")
        created = int(payload.get("created") or 0)
        rd = int(payload.get("rejected_diversity") or 0)
        rf = int(payload.get("rejected_family_cap") or 0)
        rdup = int(payload.get("rejected_duplicate") or 0)
        total = created + rd + rf + rdup
        return float(created / total) if total > 0 else 1.0
    except Exception:
        return 1.0


def _hourly_llm_cap_exhausted(conn, campaign_id: str) -> bool:
    """Esteira industrial: cap rolling de chamadas LLM por hora.
    Le hyper_controls.max_llm_calls_per_hour (DB). Se 0 ou nao setado, sem cap.
    """
    try:
        from .db import get_setting
        ctrl = get_setting(conn, "hyper_controls", default=None)
        cap = 0
        if isinstance(ctrl, dict) and ctrl.get("max_llm_calls_per_hour") is not None:
            cap = max(0, int(ctrl["max_llm_calls_per_hour"]))
        if cap <= 0:
            return False
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        n = conn.execute(
            """
            SELECT COUNT(*) AS n FROM llm_traces
            WHERE campaign_id=? AND agent_role='hypothesis_generator'
              AND action='hypothesis_generation' AND ts > ?
            """,
            (campaign_id, cutoff),
        ).fetchone()[0]
        return int(n or 0) >= cap
    except Exception:
        return False


def _recent_generation_count(conn, campaign_id: str) -> int:
    # Esteira industrial: cap rolling 1h tem prioridade absoluta.
    if _hourly_llm_cap_exhausted(conn, campaign_id):
        return 999  # forca skip "cooldown" no caller
    # hyper_controls.llm_cooldown_minutes (DB) tem prioridade sobre env var.
    cooldown_minutes = _env_int("AETHER_LLM_HYPOTHESIS_COOLDOWN_MINUTES", 45)
    try:
        from .db import get_setting
        ctrl = get_setting(conn, "hyper_controls", default=None)
        if isinstance(ctrl, dict) and ctrl.get("llm_cooldown_minutes") is not None:
            cooldown_minutes = max(0, int(ctrl["llm_cooldown_minutes"]))
    except Exception:
        pass
    if cooldown_minutes <= 0:
        return 0
    # Adaptive cooldown: se o ultimo ciclo teve <50% util, dobra a janela
    # pra dar tempo dos signals (validador, critic, hyper) abrirem espaco
    # antes de chamar o LLM de novo. Evita queimar $ em redundancia.
    try:
        ratio = _last_cycle_useful_ratio(conn, campaign_id)
        if ratio < 0.5:
            cooldown_minutes = int(cooldown_minutes * 2)
    except Exception:
        pass
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM llm_traces
        WHERE campaign_id=?
          AND agent_role='hypothesis_generator'
          AND action='hypothesis_generation'
          AND ts >= ?
        """,
        (campaign_id, cutoff),
    ).fetchone()
    return int(rows["n"] if rows else 0)


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return text[:54] or "llm-hypothesis"


def _hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def _scoped_id(campaign_id: str, base_id: str) -> str:
    if campaign_id == CURRENT_CAMPAIGN_ID:
        return base_id
    return f"{campaign_id}:{base_id}"


def _extract_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    if not raw.startswith("{"):
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            raw = match.group(0)
    return json.loads(raw)


def _clean_probe_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for spec in specs:
        engine = str(spec.get("engine") or "")
        if engine not in ALLOWED_ENGINES:
            continue
        params = spec.get("params")
        if not isinstance(params, dict):
            params = {}
        out.append({"engine": engine, "params": params})
    return out


def _clean_hypotheses(payload: dict[str, Any], *, max_new: int) -> list[dict[str, Any]]:
    hypotheses = payload.get("hypotheses")
    if not isinstance(hypotheses, list):
        return []
    cleaned = []
    for item in hypotheses:
        if not isinstance(item, dict):
            continue
        probe_specs = _clean_probe_specs(list(item.get("probe_specs") or []))
        engines = sorted({spec["engine"] for spec in probe_specs} | {str(e) for e in item.get("engines") or [] if str(e) in ALLOWED_ENGINES})
        if not engines or not probe_specs:
            continue
        name = str(item.get("name") or "LLM hypothesis")
        cleaned.append(
            {
                "slug": _slug(str(item.get("slug") or name)),
                "name": name[:120],
                "hypothesis": str(item.get("hypothesis") or name)[:800],
                "causal_mechanism": str(item.get("causal_mechanism") or "")[:800],
                "invalidation": str(item.get("invalidation") or "Scout negativo ou drawdown excessivo.")[:400],
                "engines": engines,
                "probe_specs": probe_specs,
                "base_score": max(0.1, min(0.75, float(item.get("base_score") or 0.55))),
            }
        )
        if len(cleaned) >= max_new:
            break
    return cleaned


def create_llm_hypothesis_batch(
    conn,
    *,
    max_new: int = 3,
    force_exploration: bool = False,
    caller: Callable[..., dict[str, Any]] = call_llm_agent,
) -> dict[str, Any]:
    campaign_id = get_active_campaign_id(conn)
    if _recent_generation_count(conn, campaign_id) > 0 and not force_exploration:
        return {
            "created_hypotheses": 0,
            "created_nodes": 0,
            "provider": None,
            "model": None,
            "skipped": "cooldown",
            "error": None,
        }
    # Tree focus: bloqueia novas raizes quando ha muitos ramos abertos.
    # Forca o ciclo a finalizar / expandir antes de pesquisar coisas novas.
    try:
        from .tree_focus import should_open_new_branch
        allow_new, focus_reason, focus_state = should_open_new_branch(conn, campaign_id)
        if not allow_new and not force_exploration:
            add_event(
                conn,
                type="focus_block_new_root",
                campaign_id=campaign_id,
                agent_role="hypothesis_generator",
                message=f"Hypothesis Generator bloqueado pelo tree_focus: {focus_reason}",
                payload=focus_state,
            )
            return {
                "created_hypotheses": 0,
                "created_nodes": 0,
                "provider": None,
                "model": None,
                "skipped": "tree_focus",
                "focus_reason": focus_reason,
                "focus_state": focus_state,
                "error": None,
            }
    except Exception:
        pass
    max_new = max(1, min(max_new, _env_int("AETHER_LLM_MAX_NEW", 3)))
    add_event(
        conn,
        type="llm_research_started",
        campaign_id=campaign_id,
        agent_role="hypothesis_generator",
        message="Hypothesis Generator chamou agente LLM para novas hipoteses.",
    )
    (
        context_block,
        dominant_engine,
        dominance_pct,
        family_active,
        family_validated,
    ) = _collect_exploration_context(conn, campaign_id)
    # Memoria semantica: similares ja testados no historico (validados/podados).
    # Vira parte do contexto para que o LLM nao repita ideias ja exploradas.
    similar_block = ""
    try:
        from . import semantic_memory as _sm

        seed_text = f"familia dominante {dominant_engine} dominance {dominance_pct:.0f}% / {context_block}"
        similar = _sm.find_similar(conn, text=seed_text, k=_env_int("AETHER_SEMANTIC_K", 6))
        rendered = _sm.render_for_prompt(similar)
        if rendered:
            similar_block = "\n\nMemoria semantica (estrategias parecidas ja testadas):\n" + rendered
    except Exception:
        similar_block = ""
    # Feedback loop com Critic: top razoes de rejeicao recentes viram
    # avisos dentro do prompt. Pesquisador deixa de gerar ideias que ja sabe
    # que serao mortas pelos mesmos motivos (cross-symbol, regime, MTF, etc).
    critic_block = ""
    try:
        failures = _recent_critic_failures(conn, campaign_id, days=7, top_n=5)
        if failures:
            critic_block = "\n\nFEEDBACK DO CRITIC (ultimas rejeicoes — EVITE estes modos de falha):\n" + "\n".join(
                f"- {n}x: {reason}" for n, reason in failures
            )
    except Exception:
        critic_block = ""

    prompt = USER_PROMPT.format(
        max_new=max_new,
        exploration_context=context_block + similar_block + critic_block,
        dominance_pct=f"{dominance_pct:.0f}",
    )
    if force_exploration:
        prompt += (
            "\n\nMODO EXPLORACAO OBRIGATORIA ATIVO:\n"
            "- Esta rodada PROIBE qualquer hipotese cuja unica engine seja a familia dominante.\n"
            "- Priorize combinacoes multi-engine ou um engine ainda pouco usado na arvore.\n"
            "- Se nao houver angulo realmente novo, retorne 'hypotheses': [] em vez de repetir.\n"
        )
        add_event(
            conn,
            type="exploration_forced_round",
            campaign_id=campaign_id,
            agent_role="hypothesis_generator",
            message=f"Rodada de exploracao forcada acionada (dominante={dominant_engine} {dominance_pct:.0f}%).",
            payload={"dominant_engine": dominant_engine, "dominance_pct": dominance_pct},
        )
    two_stage = _env_flag("AETHER_HYPOTHESIS_TWO_STAGE", "0")
    theorist_response: dict[str, Any] | None = None
    if two_stage:
        response, theorist_response = _two_stage_generate(
            context_block=context_block,
            similar_block=similar_block,
            critic_block=critic_block,
            dominance_pct=dominance_pct,
            max_new=max_new,
            caller=caller,
        )
        # Trace separado pro Theorist (transparencia + custo)
        if theorist_response is not response:
            try:
                record_llm_trace(
                    conn,
                    agent_role="hypothesis_generator",
                    action="theorist_stage",
                    response=theorist_response,
                    system=THEORIST_SYSTEM_PROMPT,
                    prompt="(theorist stage)",
                    campaign_id=campaign_id,
                )
            except Exception:
                pass
        active_system = ENGINEER_SYSTEM_PROMPT
        active_prompt = "(engineer stage — two-stage on)"
    else:
        response = caller(
            "hypothesis_generator",
            system=SYSTEM_PROMPT,
            prompt=prompt,
            max_tokens=_env_int("AETHER_HYPOTHESIS_MAX_TOKENS", 4000),
        )
        active_system = SYSTEM_PROMPT
        active_prompt = prompt
    record_llm_trace(
        conn,
        agent_role="hypothesis_generator",
        action="hypothesis_generation",
        response=response,
        system=active_system,
        prompt=active_prompt,
        campaign_id=campaign_id,
    )
    if not response.get("ok"):
        add_event(
            conn,
            type="llm_research_unavailable",
            campaign_id=campaign_id,
            agent_role="hypothesis_generator",
            message=str(response.get("error") or "Agente LLM indisponivel.")[:500],
            payload={"provider": response.get("provider"), "model": response.get("model")},
        )
        return {"created_hypotheses": 0, "created_nodes": 0, "provider": response.get("provider"), "model": response.get("model"), "error": response.get("error")}

    try:
        payload = _extract_json(str(response.get("text") or ""))
        hypotheses = _clean_hypotheses(payload, max_new=max_new)
    except Exception as exc:
        add_event(
            conn,
            type="llm_research_parse_error",
            campaign_id=campaign_id,
            agent_role="hypothesis_generator",
            message=f"Resposta LLM nao virou JSON de hipotese: {exc}",
            payload={"provider": response.get("provider"), "model": response.get("model")},
        )
        return {"created_hypotheses": 0, "created_nodes": 0, "provider": response.get("provider"), "model": response.get("model"), "error": str(exc)}

    created = 0
    rejected_diversity = 0
    rejected_family_cap = 0
    rejected_duplicate = 0
    existing_count = conn.execute(
        "SELECT COUNT(*) AS n FROM nodes WHERE parent_id='root' AND campaign_id=? AND id LIKE '%llmhyp:%'",
        (campaign_id,),
    ).fetchone()["n"]
    DOMINANCE_BLOCK_PCT = 30.0 if force_exploration else 60.0
    FAMILY_NODE_CAP = _env_int("AETHER_FAMILY_NODE_CAP", 8)
    FAMILY_VALIDATED_CAP = _env_int("AETHER_FAMILY_VALIDATED_CAP", 4)
    for spec in hypotheses:
        spec_engines = {e.lower() for e in spec["engines"]}
        if (
            dominant_engine
            and dominance_pct >= DOMINANCE_BLOCK_PCT
            and spec_engines == {dominant_engine}
        ):
            rejected_diversity += 1
            add_event(
                conn,
                type="hypothesis_rejected_diversity",
                campaign_id=campaign_id,
                agent_role="hypothesis_generator",
                message=(
                    f"Hipotese '{spec['name']}' rejeitada: familia '{dominant_engine}' "
                    f"ja domina {dominance_pct:.0f}% da arvore."
                ),
                payload={"slug": spec["slug"], "engines": list(spec_engines), "dominance_pct": dominance_pct},
            )
            continue
        # Cap absoluto: rejeita hipotese single-engine cuja familia ja
        # saturou nos limites configurados (nos ativos ou validados).
        if len(spec_engines) == 1:
            only_engine = next(iter(spec_engines))
            active_count = family_active.get(only_engine, 0)
            validated_count = family_validated.get(only_engine, 0)
            saturated_node = FAMILY_NODE_CAP > 0 and active_count >= FAMILY_NODE_CAP
            saturated_validated = FAMILY_VALIDATED_CAP > 0 and validated_count >= FAMILY_VALIDATED_CAP
            if saturated_node or saturated_validated:
                rejected_family_cap += 1
                add_event(
                    conn,
                    type="hypothesis_rejected_family_cap",
                    campaign_id=campaign_id,
                    agent_role="hypothesis_generator",
                    message=(
                        f"Hipotese '{spec['name']}' rejeitada: familia '{only_engine}' saturada "
                        f"(ativas={active_count}/cap={FAMILY_NODE_CAP}, "
                        f"validadas={validated_count}/cap={FAMILY_VALIDATED_CAP})."
                    ),
                    payload={
                        "slug": spec["slug"],
                        "engine": only_engine,
                        "active": active_count,
                        "active_cap": FAMILY_NODE_CAP,
                        "validated": validated_count,
                        "validated_cap": FAMILY_VALIDATED_CAP,
                    },
                )
                continue
        parent_id = _scoped_id(campaign_id, f"llmhyp:{spec['slug']}")
        if conn.execute("SELECT 1 FROM nodes WHERE id=?", (parent_id,)).fetchone():
            continue
        slug_collision = conn.execute(
            "SELECT 1 FROM nodes WHERE parent_id='root' AND campaign_id=? AND id LIKE ?",
            (campaign_id, f"%llmhyp:{spec['slug']}%"),
        ).fetchone()
        if slug_collision:
            continue
        # Gate de duplicata semantica: bloqueia hipoteses praticamente identicas
        # a alguma ja salva (validada/em teste/podada). Usa ST encoder quando
        # disponivel; com fallback hash ainda barra duplicatas estruturais.
        try:
            from . import semantic_memory as _sm
            candidate_text = " | ".join([
                spec["name"],
                spec["hypothesis"],
                spec["causal_mechanism"],
                " ".join(spec["engines"]),
            ])
            dup_threshold = float(os.environ.get("AETHER_DUPLICATE_THRESHOLD", "0.92"))
            is_dup, dup_match = _sm.is_duplicate_idea(
                conn, text=candidate_text, threshold=dup_threshold
            )
            if is_dup and dup_match:
                rejected_duplicate += 1
                add_event(
                    conn,
                    type="hypothesis_rejected_duplicate",
                    campaign_id=campaign_id,
                    agent_role="hypothesis_generator",
                    message=(
                        f"Hipotese '{spec['name']}' rejeitada: similar a node existente "
                        f"'{dup_match.get('node_id')}' (sim={float(dup_match.get('similarity', 0.0)):.3f})."
                    ),
                    payload={
                        "slug": spec["slug"],
                        "match_node_id": dup_match.get("node_id"),
                        "similarity": dup_match.get("similarity"),
                        "match_status": dup_match.get("status"),
                        "match_engine": dup_match.get("engine"),
                        "threshold": dup_threshold,
                    },
                )
                continue
        except Exception:
            pass
        base_score = float(spec["base_score"])
        upsert_node(
            conn,
            id=parent_id,
            campaign_id=campaign_id,
            parent_id="root",
            code=f"LLM-{existing_count + created + 1}",
            name=spec["name"],
            hypothesis=spec["hypothesis"],
            causal_mechanism=spec["causal_mechanism"],
            mutation_from_parent=f"Agente LLM {response.get('provider')} / {response.get('model')}",
            agent_role="hypothesis_generator",
            status="visited",
            score=base_score,
            priority=0.09,
            depth=1,
            metrics={
                "engines": spec["engines"],
                "probe_specs": spec["probe_specs"],
                "generator": "llm_agent",
                "provider": response.get("provider"),
                "model": response.get("model"),
                "invalidation": spec["invalidation"],
            },
            tags=["hypothesis", "llm", *(f"engine:{engine}" for engine in spec["engines"])],
            source_type="llm_agent",
            source_path=str(response.get("provider") or "llm"),
        )
        for idx, timeframe in enumerate(TIMEFRAMES, start=1):
            child_score = max(0.1, base_score - idx * 0.02)
            upsert_node(
                conn,
                id=f"{parent_id}:tf:{timeframe}",
                campaign_id=campaign_id,
                parent_id=parent_id,
                code=f"LLM-{timeframe}",
                name=f"{spec['name']} / {timeframe}",
                hypothesis=f"{spec['hypothesis']} Timeframe alvo: {timeframe}.",
                causal_mechanism=spec["causal_mechanism"],
                mutation_from_parent=f"Especializacao LLM para {timeframe}.",
                timeframe=timeframe,
                agent_role=role_for_timeframe(timeframe),
                status="candidate",
                score=child_score,
                priority=child_score,
                depth=2,
                metrics={
                    "engines": spec["engines"],
                    "probe_specs": spec["probe_specs"],
                    "generator": "llm_agent",
                    "provider": response.get("provider"),
                    "model": response.get("model"),
                    "invalidation": spec["invalidation"],
                },
                tags=["hypothesis", "llm", timeframe, *(f"engine:{engine}" for engine in spec["engines"])],
                source_type="llm_agent",
                source_path=str(response.get("provider") or "llm"),
            )
        created += 1

    add_event(
        conn,
        type="llm_research_completed",
        campaign_id=campaign_id,
        agent_role="hypothesis_generator",
        message=(
            f"Agente LLM criou {created} hipoteses; "
            f"{rejected_diversity} bloqueadas por diversidade; "
            f"{rejected_family_cap} bloqueadas por cap de familia; "
            f"{rejected_duplicate} bloqueadas por duplicata semantica."
        ),
        payload={
            "provider": response.get("provider"),
            "model": response.get("model"),
            "created": created,
            "rejected_diversity": rejected_diversity,
            "rejected_family_cap": rejected_family_cap,
            "rejected_duplicate": rejected_duplicate,
            "dominant_engine": dominant_engine,
            "dominance_pct": dominance_pct,
        },
    )
    return {
        "created_hypotheses": created,
        "created_nodes": created * (len(TIMEFRAMES) + 1),
        "provider": response.get("provider"),
        "model": response.get("model"),
        "rejected_diversity": rejected_diversity,
        "rejected_family_cap": rejected_family_cap,
        "rejected_duplicate": rejected_duplicate,
        "dominant_engine": dominant_engine,
        "dominance_pct": dominance_pct,
        "error": None,
    }

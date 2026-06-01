from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any

from .campaign_constants import CURRENT_CAMPAIGN_ID
from .campaigns import get_active_campaign_id
from .config import TIMEFRAMES
from .db import add_event, get_setting
from .repository import add_artifact, role_for_timeframe, upsert_node


SEARCH_QUERIES = [
    "crypto algorithmic trading strategy RSI Bollinger mean reversion backtest",
    "crypto quantitative trading breakout volatility squeeze Bollinger RSI volume ATR",
    "cryptocurrency trend following moving average breakout strategy backtest",
    "crypto mean reversion strategy backtest RSI Bollinger ATR",
]

ARXIV_QUERIES = [
    "all:cryptocurrency AND all:trading AND all:strategy",
    "all:crypto AND all:algorithmic AND all:trading",
    "all:market AND all:microstructure AND all:cryptocurrency",
]


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_title = False
        self._in_snippet = False
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        classes = set(attr.get("class", "").split())
        if tag == "a" and "result__a" in classes:
            href = attr.get("href", "")
            self._current = {"title": "", "url": _clean_duck_url(href), "snippet": ""}
            self._in_title = True
            return
        if self._current is not None and "result__snippet" in classes:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
            if self._current and self._current.get("title") and self._current.get("url"):
                self.results.append(self._current)
            self._current = None
        if self._in_snippet and tag in {"a", "div"}:
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        text = " ".join(html.unescape(data).split())
        if not text:
            return
        if self._in_title:
            self._current["title"] = (self._current.get("title", "") + " " + text).strip()
        elif self._in_snippet:
            self._current["snippet"] = (self._current.get("snippet", "") + " " + text).strip()


def _clean_duck_url(raw: str) -> str:
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urllib.parse.urlparse(raw)
    query = urllib.parse.parse_qs(parsed.query)
    uddg = query.get("uddg", [""])[0]
    if uddg:
        return urllib.parse.unquote(uddg)
    return raw


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return text[:54] or "web-strategy"


def _hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def _scoped_id(campaign_id: str, base_id: str) -> str:
    if campaign_id == CURRENT_CAMPAIGN_ID:
        return base_id
    return f"{campaign_id}:{base_id}"


def _ranking_reset_after(conn, campaign_id: str) -> str | None:
    try:
        state = get_setting(conn, f"ranking_hidden_node_ids:{campaign_id}", default={})
    except Exception:
        return None
    if isinstance(state, dict) and state.get("reset_at"):
        return str(state["reset_at"])
    return None


def search_web(query: str, *, max_results: int = 5, timeout: float = 8.0) -> list[dict[str, str]]:
    url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AetherResearchBot/0.1 (+local research assistant)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    parser = _DuckDuckGoParser()
    parser.feed(raw)
    deduped = []
    seen = set()
    for result in parser.results:
        key = result["url"]
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(result)
        if len(deduped) >= max_results:
            break
    return deduped


def search_arxiv(query: str, *, max_results: int = 5, timeout: float = 15.0) -> list[dict[str, str]]:
    params = urllib.parse.urlencode(
        {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
    )
    url = f"https://export.arxiv.org/api/query?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AetherResearchBot/0.1 (local research assistant; contact: local)",
            "Accept": "application/atom+xml,application/xml,text/xml",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")

    root = ET.fromstring(raw)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    results: list[dict[str, str]] = []
    for entry in root.findall("atom:entry", ns):
        title = " ".join((entry.findtext("atom:title", default="", namespaces=ns) or "").split())
        url_value = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
        summary = " ".join((entry.findtext("atom:summary", default="", namespaces=ns) or "").split())
        if not title or not url_value:
            continue
        results.append(
            {
                "title": title,
                "url": url_value,
                "snippet": summary[:800],
                "query": query,
                "source": "arxiv",
            }
        )
        if len(results) >= max_results:
            break
    return results


def collect_web_sources(*, max_queries: int = 3, per_query: int = 4) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen = set()
    for query in SEARCH_QUERIES[:max_queries]:
        try:
            results = search_web(query, max_results=per_query)
        except Exception:
            continue
        for result in results:
            url = result.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append({**result, "query": query, "source": result.get("source", "web")})

    if len(sources) >= per_query:
        return sources

    for query in ARXIV_QUERIES[:max_queries]:
        try:
            results = search_arxiv(query, max_results=per_query)
        except Exception:
            continue
        for result in results:
            url = result.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(result)
    return sources


def _contains(text: str, *terms: str) -> bool:
    hay = text.lower()
    return any(term in hay for term in terms)


def strategy_specs_from_sources(sources: list[dict[str, str]], *, max_specs: int = 3) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    seen_families = set()
    for source in sources:
        title = source.get("title") or source.get("url") or "web strategy"
        snippet = source.get("snippet") or ""
        text = f"{title} {snippet}".lower()

        family = None
        if _contains(text, "bollinger") and _contains(text, "breakout", "squeeze", "volatility"):
            family = "bollinger-breakout-reversion"
            spec = {
                "slug": family,
                "name": "Web Bollinger breakout/reversion scout",
                "hypothesis": "Volatilidade comprimida seguida de expansao pode alternar breakout e reversao em cripto.",
                "causal_mechanism": "Bandas capturam compressao/expansao; o scout compara interpretacoes de reversao e rompimento antes de gastar validacao profunda.",
                "engines": ["bollinger", "donchian"],
                "probe_specs": [
                    {"engine": "bollinger", "params": {"window": 20, "num_std": 2.0, "long_only": False}},
                    {"engine": "bollinger", "params": {"window": 20, "num_std": 2.5, "long_only": False}},
                    {"engine": "donchian", "params": {"entry_window": 20, "exit_window": 10, "long_only": False}},
                ],
                "base_score": 0.62,
            }
        elif _contains(text, "rsi") and _contains(text, "mean reversion", "reversion", "oversold", "overbought"):
            family = "rsi-reversion-recovery"
            spec = {
                "slug": family,
                "name": "Web RSI reversion recovery scout",
                "hypothesis": "Extremos de RSI podem capturar recuperacao curta apos exaustao direcional.",
                "causal_mechanism": "Osciladores tendem a sinalizar esticamento; o scout testa limiares mais frouxos e mais extremos.",
                "engines": ["rsi"],
                "probe_specs": [
                    {"engine": "rsi", "params": {"period": 14, "oversold": 30.0, "overbought": 70.0, "long_only": False}},
                    {"engine": "rsi", "params": {"period": 14, "oversold": 25.0, "overbought": 75.0, "long_only": False}},
                    {"engine": "rsi", "params": {"period": 7, "oversold": 25.0, "overbought": 75.0, "long_only": False}},
                ],
                "base_score": 0.6,
            }
        elif _contains(text, "adaptive", "trailing stop", "rolling sharpe", "long-short allocation", "portfolio construction"):
            family = "web-adaptive-trend-risk"
            spec = {
                "slug": family,
                "name": "Web adaptive trend-risk scout",
                "hypothesis": "Trend-following em cripto melhora quando momentum direcional e stop volatilidade-adaptativo sao combinados com filtro de regime.",
                "causal_mechanism": "Papers recentes destacam momentum persistente, volatilidade regime-dependent e trailing stops dinamicos; o scout aproxima isso com ADX/MACD/Supertrend antes de validacao profunda.",
                "engines": ["adx", "macd", "supertrend"],
                "probe_specs": [
                    {"engine": "adx", "params": {"period": 14, "threshold": 24, "long_only": True}},
                    {"engine": "macd", "params": {"fast": 12, "slow": 26, "signal": 9, "long_only": True}},
                    {"engine": "supertrend", "params": {"atr_period": 14, "atr_mult": 3.0, "long_only": True}},
                ],
                "base_score": 0.63,
            }
        elif _contains(text, "order flow", "order-flow", "order book", "order books", "microstructure", "spread", "shap", "trade features"):
            family = "web-microstructure-flow-proxy"
            spec = {
                "slug": family,
                "name": "Web microstructure flow proxy scout",
                "hypothesis": "Padroes estaveis de microestrutura podem ser aproximados em candles por desequilibrio de volume, desvio de VWAP e quebra estrutural.",
                "causal_mechanism": "Sem livro de ofertas completo, OBV, VWAP deviation e structure_break funcionam como proxies auditaveis de fluxo, impacto e ruptura de liquidez citados pelas fontes externas.",
                "engines": ["obv_trend", "vwap_dev", "structure_break"],
                "probe_specs": [
                    {"engine": "obv_trend", "params": {"window": 34, "long_only": False}},
                    {"engine": "vwap_dev", "params": {"window": 48, "z_threshold": 1.8, "long_only": False}},
                    {"engine": "structure_break", "params": {"swing_lookback": 12, "confirm_bars": 2, "long_only": False}},
                ],
                "base_score": 0.61,
            }
        elif _contains(text, "sentiment", "fear & greed", "extreme fear", "extreme greed", "uncertainty", "extremity premium"):
            family = "web-sentiment-extremity-proxy"
            spec = {
                "slug": family,
                "name": "Web sentiment extremity proxy scout",
                "hypothesis": "Regimes extremos de medo/ganancia tendem a elevar spread e incerteza; em candles isso deve aparecer como fade seletivo de fluxo esticado.",
                "causal_mechanism": "Como o dataset atual nao tem indice de sentimento ao vivo, MFI, VWAP deviation e regime_filter atuam como proxy de fluxo extremo e volatilidade absorvida.",
                "engines": ["mfi", "vwap_dev", "regime_filter"],
                "probe_specs": [
                    {"engine": "mfi", "params": {"period": 14, "oversold": 18, "overbought": 82, "long_only": False}},
                    {"engine": "vwap_dev", "params": {"window": 72, "z_threshold": 2.0, "long_only": False}},
                    {"engine": "regime_filter", "params": {"method": "high_vol_fade", "vol_window": 20, "vol_percentile_max": 85, "ma_window": 100, "slope_window": 30, "long_only": False}},
                ],
                "base_score": 0.59,
            }
        elif _contains(text, "genetic algorithm", "optimization", "parameter optimization", "adaptive strategy"):
            family = "web-adaptive-parameter-sweep"
            spec = {
                "slug": family,
                "name": "Web adaptive parameter sweep scout",
                "hypothesis": "Mercados cripto nao estacionarios favorecem familias robustas em varios parametros, nao um unico ajuste fixo.",
                "causal_mechanism": "A fonte aponta otimizacao adaptativa; o scout testa variantes simples de momentum, tendencia e compressao para identificar estabilidade antes de qualquer overfit fino.",
                "engines": ["macd", "adx", "vol_compression"],
                "probe_specs": [
                    {"engine": "macd", "params": {"fast": 8, "slow": 21, "signal": 5, "long_only": False}},
                    {"engine": "adx", "params": {"period": 14, "threshold": 28, "long_only": False}},
                    {"engine": "vol_compression", "params": {"atr_window": 20, "percentile_window": 180, "percentile_max": 25, "breakout_window": 20, "long_only": False}},
                ],
                "base_score": 0.57,
            }
        elif _contains(text, "trend", "momentum", "moving average", "ema", "macd"):
            family = "web-trend-continuation"
            spec = {
                "slug": family,
                "name": "Web trend continuation scout",
                "hypothesis": "Tendencias persistentes podem sobreviver a custos quando janelas curtas e longas divergem.",
                "causal_mechanism": "Fluxo direcional e inercia de preco sao aproximados por cruzamentos de medias e confirmados por score liquido.",
                "engines": ["ma_crossover", "donchian"],
                "probe_specs": [
                    {"engine": "ma_crossover", "params": {"short_window": 8, "long_window": 21, "long_only": True}},
                    {"engine": "ma_crossover", "params": {"short_window": 20, "long_window": 50, "long_only": True}},
                    {"engine": "donchian", "params": {"entry_window": 55, "exit_window": 20, "long_only": True}},
                ],
                "base_score": 0.58,
            }
        elif _contains(text, "breakout", "channel", "donchian"):
            family = "web-channel-breakout"
            spec = {
                "slug": family,
                "name": "Web channel breakout scout",
                "hypothesis": "Rompimentos de canais recentes podem capturar deslocamento assimetrico em ativos cripto.",
                "causal_mechanism": "Quebra de range aciona stops e fluxo de momentum; saidas curtas reduzem devolucao.",
                "engines": ["donchian"],
                "probe_specs": [
                    {"engine": "donchian", "params": {"entry_window": 20, "exit_window": 10, "long_only": False}},
                    {"engine": "donchian", "params": {"entry_window": 55, "exit_window": 20, "long_only": True}},
                ],
                "base_score": 0.56,
            }
        elif _contains(text, "crypto", "cryptocurrency", "bitcoin", "market microstructure", "machine learning", "forecasting") and _contains(
            text, "trading", "strategy", "returns", "price", "market", "signal"
        ):
            family = "web-general-alpha-scout"
            spec = {
                "slug": family,
                "name": "Web general alpha scout",
                "hypothesis": "Uma fonte externa sugere sinal preditivo em cripto; o scout traduz a ideia para familias simples e auditaveis antes de gastar validacao profunda.",
                "causal_mechanism": "Quando a fonte nao descreve regras completas, o pesquisador testa expressoes canonicas do possivel alpha: reversao, continuacao, canal e compressao de volatilidade.",
                "engines": ["rsi", "bollinger", "ma_crossover", "donchian"],
                "probe_specs": [
                    {"engine": "rsi", "params": {"period": 14, "oversold": 30.0, "overbought": 70.0, "long_only": False}},
                    {"engine": "bollinger", "params": {"window": 20, "num_std": 2.0, "long_only": False}},
                    {"engine": "ma_crossover", "params": {"short_window": 8, "long_window": 21, "long_only": True}},
                    {"engine": "donchian", "params": {"entry_window": 20, "exit_window": 10, "long_only": False}},
                ],
                "base_score": 0.52,
            }
        else:
            continue

        if family in seen_families:
            continue
        seen_families.add(family)
        specs.append(
            {
                **spec,
                "source_title": title,
                "source_url": source.get("url", ""),
                "source_snippet": snippet[:500],
                "source_query": source.get("query", ""),
                "source": source.get("source", "web"),
            }
        )
        if len(specs) >= max_specs:
            break
    return specs


def create_web_hypothesis_batch(conn, *, max_new: int = 1) -> dict[str, Any]:
    campaign_id = get_active_campaign_id(conn)
    reset_after = _ranking_reset_after(conn, campaign_id)
    add_event(
        conn,
        type="web_research_started",
        campaign_id=campaign_id,
        agent_role="hypothesis_generator",
        message="Busca web iniciada para novas hipoteses.",
    )
    # A coleta web pode levar varios segundos. Comitar o evento antes evita
    # segurar write-lock SQLite enquanto chamadas HTTP externas estao em voo.
    try:
        conn.commit()
    except Exception:
        pass
    sources = collect_web_sources(max_queries=3, per_query=4)
    specs = strategy_specs_from_sources(sources, max_specs=max_new * 2)
    created = 0
    used_sources: list[dict[str, str]] = []

    for spec in specs:
        source_url = spec.get("source_url", "")
        parent_base_id = _scoped_id(campaign_id, f"webhyp:{spec['slug']}:{_hash(source_url or spec['name'])}")
        parent_id = parent_base_id
        existing = conn.execute(
            "SELECT created_at FROM nodes WHERE id=? AND campaign_id=?",
            (parent_id, campaign_id),
        ).fetchone()
        if existing:
            created_at = str(existing["created_at"] if hasattr(existing, "keys") else existing[0])
            if reset_after and created_at <= reset_after:
                parent_id = f"{parent_base_id}:r:{_hash(reset_after)[:6]}"
                if conn.execute("SELECT 1 FROM nodes WHERE id=? AND campaign_id=?", (parent_id, campaign_id)).fetchone():
                    continue
            else:
                continue
        base_score = float(spec.get("base_score", 0.55))
        upsert_node(
            conn,
            id=parent_id,
            campaign_id=campaign_id,
            parent_id="root",
            code=f"WEB-{created + 1}",
            name=str(spec["name"]),
            hypothesis=str(spec["hypothesis"]),
            causal_mechanism=str(spec["causal_mechanism"]),
            mutation_from_parent=f"Pesquisa web: {spec.get('source_title', '')}",
            agent_role="hypothesis_generator",
            status="visited",
            score=base_score,
            priority=0.08,
            depth=1,
            metrics={
                "engines": spec["engines"],
                "probe_specs": spec["probe_specs"],
                "generator": "web_research",
                "source_url": source_url,
                "source_title": spec.get("source_title"),
                "source_snippet": spec.get("source_snippet"),
                "source_query": spec.get("source_query"),
                "source": spec.get("source"),
            },
            tags=["hypothesis", "web", *[f"engine:{engine}" for engine in spec["engines"]]],
            source_type="web_research",
            source_path=source_url,
        )
        if source_url:
            add_artifact(
                conn,
                node_id=parent_id,
                campaign_id=campaign_id,
                kind="web_source",
                path=source_url,
                title=str(spec.get("source_title") or source_url),
                payload={"snippet": spec.get("source_snippet"), "query": spec.get("source_query")},
            )

        for idx, timeframe in enumerate(TIMEFRAMES, start=1):
            child_score = max(0.1, base_score - idx * 0.02)
            upsert_node(
                conn,
                id=f"{parent_id}:tf:{timeframe}",
                campaign_id=campaign_id,
                parent_id=parent_id,
                code=f"WEB-{timeframe}",
                name=f"{spec['name']} / {timeframe}",
                hypothesis=f"{spec['hypothesis']} Timeframe alvo: {timeframe}.",
                causal_mechanism=str(spec["causal_mechanism"]),
                mutation_from_parent=f"Especializacao automatica web para {timeframe}.",
                timeframe=timeframe,
                agent_role=role_for_timeframe(timeframe),
                status="candidate",
                score=child_score,
                priority=child_score,
                depth=2,
                metrics={
                    "engines": spec["engines"],
                    "probe_specs": spec["probe_specs"],
                    "generator": "web_research",
                    "source_url": source_url,
                    "source_title": spec.get("source_title"),
                },
                tags=["hypothesis", "web", timeframe, *[f"engine:{engine}" for engine in spec["engines"]]],
                source_type="web_research",
                source_path=source_url,
            )

        created += 1
        used_sources.append({"title": spec.get("source_title", ""), "url": source_url})
        if created >= max_new:
            break

    add_event(
        conn,
        type="web_research_completed",
        campaign_id=campaign_id,
        agent_role="hypothesis_generator",
        message=f"Pesquisa web concluiu: {created} hipoteses criadas a partir de {len(sources)} fontes.",
        payload={"created": created, "source_count": len(sources), "sources": used_sources},
    )
    return {"created_hypotheses": created, "created_nodes": created * (len(TIMEFRAMES) + 1), "source_count": len(sources), "sources": used_sources}

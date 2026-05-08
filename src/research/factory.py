"""
Carrega o catalogo de estrategias aprovadas e converte em StrategySpec
prontos para serem usados pelo bot principal (`src/main.py`) ou por
qualquer fluxo de portfolio.

Exemplo:

    from src.research.factory import load_approved_strategies
    specs = load_approved_strategies()
    for spec in specs:
        # spec.function eh callable(stock_data, **params) -> bool|None
        # spec.take_profit_pct, stop_loss_pct, max_hold_candles configurados
        ...
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.modules.StrategyResearchEngine import StrategySpec
from src.research.catalog import CatalogEntry, StrategyCatalog
from src.research.templates import build_strategy_fn, TemplateSpec


DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "research" / "strategy_catalog.json"


def _entry_to_spec(entry: CatalogEntry) -> StrategySpec:
    template_spec = TemplateSpec(template=entry.template, params=entry.params)
    fn = build_strategy_fn(template_spec)
    name = f"{entry.template}#{entry.id[:8]}"
    return StrategySpec(
        name=name,
        function=fn,
        params=dict(entry.params),
        needs_instance=False,
        take_profit_pct=entry.risk.get("take_profit_pct"),
        stop_loss_pct=entry.risk.get("stop_loss_pct"),
        max_hold_candles=entry.risk.get("max_hold_candles"),
    )


def load_approved_strategies(
    catalog_path: Path | None = None,
    min_score: float | None = None,
    max_per_template: int | None = None,
    limit: int | None = None,
) -> list[StrategySpec]:
    """Retorna estrategias aprovadas, ordenadas por score desc.

    Args:
      min_score:           descarta abaixo desse score
      max_per_template:    no maximo N aprovadas por template (diversifica)
      limit:               teto global
    """
    catalog = StrategyCatalog(catalog_path or DEFAULT_CATALOG_PATH)
    approved = catalog.approved()
    if min_score is not None:
        approved = [e for e in approved if e.score >= min_score]
    if max_per_template:
        per: dict[str, int] = {}
        kept: list[CatalogEntry] = []
        for entry in approved:
            n = per.get(entry.template, 0)
            if n >= max_per_template:
                continue
            kept.append(entry)
            per[entry.template] = n + 1
        approved = kept
    if limit:
        approved = approved[:limit]
    return [_entry_to_spec(entry) for entry in approved]


def describe_approved(catalog_path: Path | None = None) -> list[dict[str, Any]]:
    """Resumo legivel de cada aprovada — util para logs."""
    catalog = StrategyCatalog(catalog_path or DEFAULT_CATALOG_PATH)
    out: list[dict[str, Any]] = []
    for entry in catalog.approved():
        s = entry.summary or {}
        out.append({
            "id": entry.id,
            "template": entry.template,
            "params": entry.params,
            "risk": entry.risk,
            "score": entry.score,
            "origin": entry.origin,
            "parents": entry.parents,
            "scenarios_passed": s.get("scenarios_passed"),
            "scenarios_total": s.get("scenarios_total"),
            "avg_winrate": s.get("avg_winrate"),
            "avg_profit_factor": s.get("avg_profit_factor"),
            "avg_drawdown": s.get("avg_drawdown"),
            "approved_at": entry.approved_at,
        })
    return out

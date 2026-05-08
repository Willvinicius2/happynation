"""
Gerencia o conjunto de estrategias ativas que o bot usa em producao.

Sempre que uma estrategia eh aprovada no pesquisador, ela eh
auto-deployada para `src/logs/research/active_strategies.json`. O
arquivo serve como "manifest" lido pelo bot principal.

Para consumir do bot:

    from src.research.active import get_active_specs
    specs = get_active_specs()  # lista de StrategySpec compatible com o engine
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.modules.StrategyResearchEngine import StrategySpec
from src.research.catalog import CatalogEntry
from src.research.templates import TemplateSpec, build_strategy_fn


DEFAULT_PATH = Path(__file__).resolve().parents[1] / "logs" / "research" / "active_strategies.json"
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "research" / "strategy_catalog.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_lock = threading.Lock()


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "updated_at": None, "strategies": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "updated_at": None, "strategies": []}


def _save_raw(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now_iso()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def deploy(entry: CatalogEntry, path: Path | None = None, max_per_template: int | None = 5) -> dict[str, Any]:
    """Adiciona uma estrategia aprovada ao manifest. Retorna o registro deployado."""
    target = path or DEFAULT_PATH
    record = {
        "id": entry.id,
        "template": entry.template,
        "params": entry.params,
        "risk": entry.risk,
        "score": entry.score,
        "origin": entry.origin,
        "parents": entry.parents,
        "summary": entry.summary,
        "deployed_at": _now_iso(),
    }
    with _lock:
        data = _load_raw(target)
        data["strategies"] = [s for s in data["strategies"] if s.get("id") != entry.id]
        data["strategies"].append(record)
        # diversifica: limita aprovadas por template, mantendo as de maior score
        if max_per_template:
            data["strategies"].sort(key=lambda s: s.get("score", 0), reverse=True)
            kept: list[dict[str, Any]] = []
            counts: dict[str, int] = {}
            for s in data["strategies"]:
                tpl = s.get("template", "?")
                if counts.get(tpl, 0) >= max_per_template:
                    continue
                kept.append(s)
                counts[tpl] = counts.get(tpl, 0) + 1
            data["strategies"] = kept
        _save_raw(target, data)
    return record


def deploy_record(record: dict[str, Any], path: Path | None = None, max_per_family: int | None = 5) -> dict[str, Any]:
    """Adiciona um registro serializado ao manifest ativo."""
    target = path or DEFAULT_PATH
    payload = dict(record)
    payload.setdefault("source", "manual")
    with _lock:
        data = _load_raw(target)
        current = list(data.get("strategies") or [])
        existing = next((s for s in current if s.get("id") == payload.get("id")), None)
        payload.setdefault("deployed_at", (existing or {}).get("deployed_at") or _now_iso())
        data["strategies"] = [s for s in current if s.get("id") != payload.get("id")]
        data["strategies"].append(payload)
        if max_per_family:
            data["strategies"].sort(key=lambda s: s.get("score", 0), reverse=True)
            kept: list[dict[str, Any]] = []
            counts: dict[str, int] = {}
            for item in data["strategies"]:
                family = item.get("template") or item.get("engine") or "?"
                if counts.get(family, 0) >= max_per_family:
                    continue
                kept.append(item)
                counts[family] = counts.get(family, 0) + 1
            data["strategies"] = kept
        if data["strategies"] != current:
            _save_raw(target, data)
    return payload


def remove(strategy_id: str, path: Path | None = None) -> bool:
    target = path or DEFAULT_PATH
    with _lock:
        data = _load_raw(target)
        before = len(data["strategies"])
        data["strategies"] = [s for s in data["strategies"] if s.get("id") != strategy_id]
        after = len(data["strategies"])
        if after != before:
            _save_raw(target, data)
            return True
    return False


def list_active(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or DEFAULT_PATH
    with _lock:
        data = _load_raw(target)
    return list(data.get("strategies", []))


def _approved_ids(catalog_path: Path | None = None) -> set[str]:
    target = catalog_path or DEFAULT_CATALOG_PATH
    if not target.exists():
        return set()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return set()
    raw_entries = data.get("entries") or {}
    if isinstance(raw_entries, dict):
        entries = raw_entries.values()
    elif isinstance(raw_entries, list):
        entries = raw_entries
    else:
        entries = []
    approved: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") == "approved" or bool(entry.get("approved", False)):
            strategy_id = entry.get("id")
            if strategy_id:
                approved.add(str(strategy_id))
    return approved


def require_catalog_approval(default: bool = True) -> bool:
    value = os.getenv("REQUIRE_ACTIVE_STRATEGY_APPROVAL")
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "on"}


def _aether_record_approved(record: dict[str, Any]) -> bool:
    source = str(record.get("source") or record.get("origin") or "")
    if not source.startswith("aether"):
        return False
    summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
    validation_passed = record.get("validation_passed") is True or summary.get("validation_passed") is True
    critic_status = record.get("critic_status") or summary.get("critic_status")
    return validation_passed and critic_status == "approved"


def _stock_data_for_aether(stock_data):
    return stock_data.rename(
        columns={
            "open_price": "open",
            "high_price": "high",
            "low_price": "low",
            "close_price": "close",
        }
    )[["open", "high", "low", "close", "volume"]]


def _build_aether_fn(engine: str, params: dict[str, Any]):
    from src.research.aether.compute import _build_signals

    def fn(stock_data, verbose: bool = False, **_: Any) -> bool | None:
        try:
            data = _stock_data_for_aether(stock_data)
            signal = _build_signals(engine, data, params).dropna()
        except Exception as exc:
            if verbose:
                print(f"[AETHER_ACTIVE] erro {engine}: {exc}")
            return None
        if signal.empty:
            return None
        last = int(signal.iloc[-1])
        if verbose:
            print(f"[AETHER_ACTIVE] {engine} sinal={last} params={params}")
        if last > 0:
            return True
        if last < 0:
            return False
        return None

    return fn


def _matches_asset(record: dict[str, Any], asset: str | None) -> bool:
    if not asset:
        return True
    expected = str(record.get("asset") or record.get("symbol") or "").upper()
    return not expected or expected == asset.upper()


def get_active_specs(
    path: Path | None = None,
    asset: str | None = None,
    catalog_path: Path | None = None,
) -> list[StrategySpec]:
    """Retorna StrategySpec de cada estrategia ativa, prontos para o engine."""
    out: list[StrategySpec] = []
    approved = _approved_ids(catalog_path) if require_catalog_approval() else None
    for record in list_active(path):
        if not _matches_asset(record, asset):
            continue
        strategy_id = str(record.get("id") or "")
        if approved is not None and strategy_id not in approved:
            if not _aether_record_approved(record):
                continue
        try:
            params = dict(record.get("params", {}))
            if record.get("timeframe"):
                params["_timeframe"] = record.get("timeframe")
            probe_specs = record.get("probe_specs") if isinstance(record.get("probe_specs"), list) else []
            if record.get("engine") == "combo" or len(probe_specs) > 1:
                combo_params = dict(record.get("params", {}))
                combo_params.setdefault("mode", "consensus")
                if probe_specs:
                    combo_params.setdefault("specs", probe_specs)
                fn = _build_aether_fn("combo", combo_params)
                name = f"Aether:combo#{str(record.get('id', ''))[-8:]}"
            elif record.get("engine"):
                fn = _build_aether_fn(str(record["engine"]), dict(record.get("params", {})))
                name = f"Aether:{record['engine']}#{str(record.get('id', ''))[-8:]}"
            else:
                template_spec = TemplateSpec(template=record["template"], params=record.get("params", {}))
                fn = build_strategy_fn(template_spec)
                name = f"{record['template']}#{record['id'][:8]}"
        except Exception:
            continue
        risk = record.get("risk", {}) or {}
        out.append(StrategySpec(
            name=name,
            function=fn,
            params=params,
            needs_instance=False,
            take_profit_pct=risk.get("take_profit_pct"),
            stop_loss_pct=risk.get("stop_loss_pct"),
            max_hold_candles=risk.get("max_hold_candles"),
        ))
    return out


def active_manifest_enabled(default: bool = True) -> bool:
    value = os.getenv("USE_ACTIVE_STRATEGIES")
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "on"}

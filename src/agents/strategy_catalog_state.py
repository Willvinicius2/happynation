from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CATALOG_PATH = Path("src/logs/research/strategy_catalog.json")
DEFAULT_ACTIVE_PATH = Path("src/logs/research/active_strategies.json")


def load_json(path: Path | str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    target = Path(path)
    fallback = default if default is not None else {}
    if not target.exists():
        return fallback
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def catalog_entries(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    entries = catalog.get("entries", [])
    if isinstance(entries, dict):
        return [entry for entry in entries.values() if isinstance(entry, dict)]
    if isinstance(entries, list):
        return [entry for entry in entries if isinstance(entry, dict)]
    strategies = catalog.get("strategies", [])
    if isinstance(strategies, list):
        return [entry for entry in strategies if isinstance(entry, dict)]
    return []


def active_records(active: dict[str, Any]) -> list[dict[str, Any]]:
    records = active.get("strategies", [])
    if isinstance(records, list):
        return [record for record in records if isinstance(record, dict)]
    return []


def is_approved(entry: dict[str, Any]) -> bool:
    return entry.get("status") == "approved" or bool(entry.get("approved", False))


def is_rejected(entry: dict[str, Any]) -> bool:
    return entry.get("status") == "rejected" or bool(entry.get("rejected", False))


def parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def metric_value(entry: dict[str, Any], *names: str) -> float | None:
    summary = entry.get("summary", {}) or {}
    pools: list[dict[str, Any]] = []
    if isinstance(summary, dict):
        pools.append(summary)
        for nested_name in ("full", "quick", "test", "recent", "train"):
            nested = summary.get(nested_name)
            if isinstance(nested, dict):
                pools.append(nested)
    for pool in pools:
        for name in names:
            value = pool.get(name)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def pass_ratio(entry: dict[str, Any]) -> tuple[float | None, int | None, int | None]:
    summary = entry.get("summary", {}) or {}
    if not isinstance(summary, dict):
        return None, None, None
    pairs = (
        ("windows_passed", "windows_total"),
        ("scenarios_passed", "scenarios_total"),
    )
    for passed_key, total_key in pairs:
        try:
            passed = int(summary[passed_key])
            total = int(summary[total_key])
        except (KeyError, TypeError, ValueError):
            continue
        if total <= 0:
            return None, passed, total
        return passed / total, passed, total
    return None, None, None

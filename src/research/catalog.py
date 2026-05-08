"""
Catalogo persistente de estrategias descobertas.

Arquivo unico em `src/logs/research/strategy_catalog.json`. Cada entrada
representa um candidato testado pelo loop de descoberta — aprovado ou
rejeitado — com seus parametros, metricas por cenario e status final.

Formato:
{
  "version": 1,
  "updated_at": "2026-04-27T13:45:11Z",
  "rounds": 3,
  "entries": {
    "<id>": {
      "id": "<id>",
      "template": "DualMA_Trend",
      "params": {...},
      "risk": {...},
      "status": "approved" | "rejected",
      "approved_at": "<iso>",
      "score": 142.7,
      "scenarios": { "BTCUSDT": {...}, "ETHUSDT": {...} },
      "summary": { "scenarios_passed": 4, "scenarios_total": 5, ... }
    }
  }
}
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CATALOG_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_strategy_id(template: str, params: dict[str, Any], risk: dict[str, Any]) -> str:
    payload = json.dumps(
        {"t": template, "p": _normalize(params), "r": _normalize(risk)},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:14]


def _normalize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 6)
    return obj


@dataclass
class CatalogEntry:
    id: str
    template: str
    params: dict[str, Any]
    risk: dict[str, Any]
    status: str = "pending"            # pending | rejected | approved
    score: float = 0.0
    summary: dict[str, Any] = field(default_factory=dict)
    scenarios: dict[str, Any] = field(default_factory=dict)
    discovered_at: str = field(default_factory=_now_iso)
    approved_at: str | None = None
    rejected_at: str | None = None
    rejection_reason: str | None = None
    origin: str = "grid"               # grid | random | mutation | crossover
    parents: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "template": self.template,
            "params": self.params,
            "risk": self.risk,
            "status": self.status,
            "score": self.score,
            "summary": self.summary,
            "scenarios": self.scenarios,
            "discovered_at": self.discovered_at,
            "approved_at": self.approved_at,
            "rejected_at": self.rejected_at,
            "rejection_reason": self.rejection_reason,
            "origin": self.origin,
            "parents": self.parents,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CatalogEntry":
        return cls(
            id=data["id"],
            template=data["template"],
            params=data.get("params", {}),
            risk=data.get("risk", {}),
            status=data.get("status", "pending"),
            score=float(data.get("score", 0.0)),
            summary=data.get("summary", {}),
            scenarios=data.get("scenarios", {}),
            discovered_at=data.get("discovered_at", _now_iso()),
            approved_at=data.get("approved_at"),
            rejected_at=data.get("rejected_at"),
            rejection_reason=data.get("rejection_reason"),
            origin=data.get("origin", "grid"),
            parents=list(data.get("parents", [])),
        )


class StrategyCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._entries: dict[str, CatalogEntry] = {}
        self._rounds: int = 0
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        self._rounds = int(data.get("rounds", 0))
        for entry_data in (data.get("entries") or {}).values():
            try:
                entry = CatalogEntry.from_dict(entry_data)
                self._entries[entry.id] = entry
            except Exception:
                continue

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": CATALOG_VERSION,
                "updated_at": _now_iso(),
                "rounds": self._rounds,
                "entries": {entry_id: entry.to_dict() for entry_id, entry in self._entries.items()},
            }
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def has(self, strategy_id: str) -> bool:
        with self._lock:
            return strategy_id in self._entries

    def get(self, strategy_id: str) -> CatalogEntry | None:
        with self._lock:
            entry = self._entries.get(strategy_id)
            return entry

    def upsert(self, entry: CatalogEntry) -> None:
        with self._lock:
            self._entries[entry.id] = entry

    def increment_round(self) -> int:
        with self._lock:
            self._rounds += 1
            return self._rounds

    def stats(self) -> dict[str, Any]:
        with self._lock:
            entries = list(self._entries.values())
        approved = [e for e in entries if e.status == "approved"]
        rejected = [e for e in entries if e.status == "rejected"]
        per_template: dict[str, dict[str, int]] = {}
        for entry in entries:
            slot = per_template.setdefault(entry.template, {"total": 0, "approved": 0, "rejected": 0})
            slot["total"] += 1
            if entry.status == "approved":
                slot["approved"] += 1
            elif entry.status == "rejected":
                slot["rejected"] += 1
        return {
            "rounds": self._rounds,
            "total": len(entries),
            "approved": len(approved),
            "rejected": len(rejected),
            "per_template": per_template,
        }

    def approved(self, limit: int | None = None) -> list[CatalogEntry]:
        with self._lock:
            approved = [e for e in self._entries.values() if e.status == "approved"]
        approved.sort(key=lambda e: e.score, reverse=True)
        return approved[:limit] if limit else approved

    def all(self) -> list[CatalogEntry]:
        with self._lock:
            return list(self._entries.values())

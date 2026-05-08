from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agents.base import AgentFinding, AgentSeverity


class ResearchAgent:
    name = "research_agent"
    display_name = "Manifest"

    def __init__(
        self,
        catalog_path: Path | str = "src/logs/research/strategy_catalog.json",
        active_path: Path | str = "src/logs/research/active_strategies.json",
        min_approved: int = 1,
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self.active_path = Path(active_path)
        self.min_approved = min_approved

    def run(self) -> list[AgentFinding]:
        active = self._load_json(self.active_path, {"strategies": []})
        catalog = self._load_json(self.catalog_path, {"entries": []})
        active_count = len(active.get("strategies", []) or [])
        catalog_approved_count = self._approved_count(catalog)
        active_approved_count = self._approved_count(active)
        approved_count = max(catalog_approved_count, active_approved_count)

        findings: list[AgentFinding] = []
        if active_count < self.min_approved:
            severity = AgentSeverity.WARNING if approved_count else AgentSeverity.INFO
            action = (
                "Deployar estrategias aprovadas para active_strategies.json."
                if approved_count
                else "Rodar discovery/research para gerar candidatas aprovadas."
            )
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=severity,
                    title="Sem estrategias ativas suficientes",
                    message=(
                        f"Estrategias ativas: {active_count}. "
                        f"Aprovadas no catalogo: {approved_count}."
                    ),
                    action=action,
                    metadata={
                        "active_path": str(self.active_path),
                        "catalog_path": str(self.catalog_path),
                        "active_count": active_count,
                        "approved_count": approved_count,
                    },
                )
            )
        else:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Research com manifest ativo",
                    message=f"{active_count} estrategia(s) ativa(s) disponiveis para consumo do bot.",
                    metadata={
                        "active_count": active_count,
                        "approved_count": approved_count,
                        "catalog_approved_count": catalog_approved_count,
                        "active_approved_count": active_approved_count,
                    },
                )
            )

        return findings

    def _load_json(self, path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default

    def _approved_count(self, catalog: dict[str, Any]) -> int:
        entries = catalog.get("entries", [])
        if isinstance(entries, dict):
            entries = list(entries.values())
        elif not isinstance(entries, list) or not entries:
            entries = catalog.get("strategies", [])
        return sum(
            1
            for entry in entries
            if self._is_approved(entry)
        )

    def _is_approved(self, entry: dict[str, Any]) -> bool:
        if not isinstance(entry, dict):
            return False
        summary = entry.get("summary") if isinstance(entry, dict) else None
        summary_ok = isinstance(summary, dict) and (
            summary.get("critic_status") == "approved"
            or summary.get("validation_passed") is True
        )
        return (
            entry.get("status") == "approved"
            or bool(entry.get("approved", False))
            or summary_ok
        )

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from src.agents.base import AgentFinding, AgentSeverity
from src.agents.strategy_catalog_state import (
    DEFAULT_CATALOG_PATH,
    catalog_entries,
    is_approved,
    is_rejected,
    load_json,
    now_utc,
    parse_iso_utc,
)


class StrategyResearcherAgent:
    name = "strategy_researcher_agent"
    display_name = "Cacador"

    def __init__(
        self,
        catalog_path: Path | str = DEFAULT_CATALOG_PATH,
        max_catalog_age_days: int = 7,
        saturated_rejections: int = 5,
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self.max_catalog_age_days = max_catalog_age_days
        self.saturated_rejections = saturated_rejections

    def run(self) -> list[AgentFinding]:
        if not self.catalog_path.exists():
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Catalogo de estrategias ausente",
                    message=f"Nao encontrei {self.catalog_path}.",
                    action="Rodar discovery/research para criar o catalogo inicial.",
                    metadata={"catalog_path": str(self.catalog_path)},
                )
            ]

        catalog = load_json(self.catalog_path, {"entries": []})
        entries = catalog_entries(catalog)
        if not entries:
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Catalogo de estrategias vazio",
                    message="O catalogo existe, mas nao tem candidatas registradas.",
                    action="Rodar uma rodada de pesquisa para gerar candidatas.",
                    metadata={"catalog_path": str(self.catalog_path)},
                )
            ]

        stats = self._build_stats(catalog, entries)
        findings: list[AgentFinding] = []
        findings.extend(self._catalog_age_findings(catalog, stats))

        if stats["approved"] == 0:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Catalogo sem estrategias aprovadas",
                    message=(
                        f"{stats['total']} candidatas no catalogo, "
                        f"{stats['rejected']} rejeitadas e nenhuma aprovada."
                    ),
                    action="Ajustar criterios ou gerar nova rodada com templates diferentes.",
                    metadata=stats,
                )
            )
        else:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Catalogo com estrategias aprovadas",
                    message=(
                        f"{stats['approved']} de {stats['total']} candidatas estao aprovadas."
                    ),
                    action="Enviar aprovadas ao Juiz antes de qualquer paper/deploy.",
                    metadata=stats,
                )
            )

        findings.extend(self._saturated_template_findings(stats))
        return findings

    def _build_stats(self, catalog: dict, entries: list[dict]) -> dict:
        status_counts = Counter()
        origin_counts = Counter()
        template_stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {"total": 0, "approved": 0, "rejected": 0, "pending": 0}
        )

        for entry in entries:
            template = str(entry.get("template", "unknown"))
            origin = str(entry.get("origin", "unknown"))
            if is_approved(entry):
                status = "approved"
            elif is_rejected(entry):
                status = "rejected"
            else:
                status = "pending"

            status_counts[status] += 1
            origin_counts[origin] += 1
            template_stats[template]["total"] += 1
            template_stats[template][status] += 1

        return {
            "catalog_path": str(self.catalog_path),
            "updated_at": catalog.get("updated_at"),
            "rounds": int(catalog.get("rounds", 0) or 0),
            "total": len(entries),
            "approved": status_counts["approved"],
            "rejected": status_counts["rejected"],
            "pending": status_counts["pending"],
            "by_origin": dict(origin_counts),
            "by_template": dict(template_stats),
        }

    def _catalog_age_findings(self, catalog: dict, stats: dict) -> list[AgentFinding]:
        updated_at = parse_iso_utc(catalog.get("updated_at"))
        if updated_at is None:
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Catalogo sem data valida",
                    message="Nao foi possivel identificar quando o catalogo foi atualizado.",
                    action="Rodar nova pesquisa para gerar timestamp confiavel.",
                    metadata=stats,
                )
            ]

        age_days = (now_utc() - updated_at).total_seconds() / 86400
        if age_days <= self.max_catalog_age_days:
            return []
        metadata = dict(stats)
        metadata["catalog_age_days"] = round(age_days, 2)
        return [
            AgentFinding(
                agent=self.name,
                display_name=self.display_name,
                severity=AgentSeverity.WARNING,
                title="Catalogo de estrategias antigo",
                message=f"O catalogo foi atualizado ha {age_days:.1f} dias.",
                action="Rodar nova pesquisa antes de promover estrategias.",
                metadata=metadata,
            )
        ]

    def _saturated_template_findings(self, stats: dict) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        for template, tpl_stats in sorted(stats["by_template"].items()):
            if tpl_stats["approved"] > 0:
                continue
            if tpl_stats["rejected"] < self.saturated_rejections:
                continue
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Template saturado de rejeicoes",
                    message=(
                        f"{template} tem {tpl_stats['rejected']} rejeicoes "
                        "e nenhuma aprovada."
                    ),
                    action="Reduzir prioridade desse template ou alterar o espaco de parametros.",
                    metadata={
                        "template": template,
                        "template_stats": tpl_stats,
                        "fingerprint": f"{self.name}:template_saturated:{template}",
                    },
                )
            )
        return findings

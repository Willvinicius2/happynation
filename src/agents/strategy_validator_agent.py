from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.agents.base import AgentFinding, AgentSeverity
from src.agents.strategy_catalog_state import (
    DEFAULT_ACTIVE_PATH,
    DEFAULT_CATALOG_PATH,
    active_records,
    catalog_entries,
    is_approved,
    load_json,
    metric_value,
    pass_ratio,
)


@dataclass(frozen=True)
class StrategyValidationCriteria:
    min_total_trades: float = 20.0
    min_profit_factor: float = 1.15
    max_drawdown_pct: float = 25.0
    max_loss_streak: float = 4.0
    min_consistency_score: float = 55.0
    min_windows_passed_ratio: float = 0.6


class StrategyValidatorAgent:
    name = "strategy_validator_agent"
    display_name = "Juiz"

    def __init__(
        self,
        catalog_path: Path | str = DEFAULT_CATALOG_PATH,
        active_path: Path | str = DEFAULT_ACTIVE_PATH,
        criteria: StrategyValidationCriteria | None = None,
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self.active_path = Path(active_path)
        self.criteria = criteria or StrategyValidationCriteria()

    def run(self) -> list[AgentFinding]:
        catalog = load_json(self.catalog_path, {"entries": []})
        active = load_json(self.active_path, {"strategies": []})
        entries = catalog_entries(catalog)
        approved = [entry for entry in entries if is_approved(entry)]
        approved_by_id = {str(entry.get("id")): entry for entry in approved if entry.get("id")}
        active = active_records(active)

        findings: list[AgentFinding] = []
        findings.extend(self._active_without_approval_findings(active, approved_by_id))

        if not approved:
            active_aether_approved = [record for record in active if self._has_aether_approval(record)]
            if active_aether_approved and not findings:
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.INFO,
                        title="Estrategias Aether ativas com critic aprovado",
                        message=(
                            f"{len(active_aether_approved)} estrategia(s) ativas vieram do "
                            "aether_auto_deploy com critic/validacao aprovada."
                        ),
                        action="Manter auditoria via Critic v4 e monitorar drift/performance no paper.",
                        metadata={
                            "active_count": len(active),
                            "aether_approved_count": len(active_aether_approved),
                            "active_path": str(self.active_path),
                        },
                    )
                )
                return findings
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Nenhuma estrategia aprovada para validar",
                    message="O catalogo ainda nao tem candidatas aprovadas.",
                    action="Aguardar o Cacador gerar aprovadas ou rodar nova pesquisa.",
                    metadata={
                        "catalog_path": str(self.catalog_path),
                        "active_path": str(self.active_path),
                    },
                )
            )
            return findings

        invalid_count = 0
        inconclusive_count = 0
        for entry in approved:
            audit = self._audit_entry(entry)
            if audit["missing"]:
                inconclusive_count += 1
                findings.append(self._missing_metrics_finding(entry, audit))
                continue
            if audit["failed"]:
                invalid_count += 1
                findings.append(self._failed_criteria_finding(entry, audit))

        if invalid_count == 0 and inconclusive_count == 0:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Estrategias aprovadas passam na auditoria",
                    message=f"{len(approved)} estrategia(s) aprovada(s) cumprem os criterios do Juiz.",
                    action="Mover para paper somente via Promotor e com registro de decisao.",
                    metadata={
                        "approved_count": len(approved),
                        "criteria": self.criteria.__dict__,
                    },
                )
            )
        return findings

    def _active_without_approval_findings(
        self,
        active: list[dict[str, Any]],
        approved_by_id: dict[str, dict[str, Any]],
    ) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        for record in active:
            strategy_id = record.get("id")
            if not strategy_id:
                continue
            strategy_id = str(strategy_id)
            if strategy_id in approved_by_id:
                continue
            # Aether auto_deploy bypass-a o catálogo legado: as estratégias
            # passam pelo Critic v4 (mais rigoroso que os critérios do Juiz).
            # Se o record traz prova dessa auditoria, aceitamos como aprovada.
            if record.get("source") == "aether_auto_deploy" and self._has_aether_approval(record):
                continue
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.CRITICAL,
                    title="Estrategia ativa nao aprovada",
                    message=f"{strategy_id} esta ativa, mas nao aparece como aprovada no catalogo.",
                    action="Remover do manifest ativo ou revalidar antes de usar no paper/producao.",
                    metadata={
                        "strategy_id": strategy_id,
                        "template": record.get("template"),
                        "active_path": str(self.active_path),
                        "catalog_path": str(self.catalog_path),
                    },
                )
            )
        return findings

    def _has_aether_approval(self, record: dict[str, Any]) -> bool:
        summary = record.get("summary") or {}
        return isinstance(summary, dict) and (
            summary.get("critic_status") == "approved"
            or summary.get("validation_passed") is True
        )

    def _audit_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        metrics = {
            "total_trades": metric_value(entry, "total_trades", "avg_trades"),
            "profit_factor": metric_value(entry, "profit_factor", "median_profit_factor", "avg_profit_factor"),
            "max_drawdown_pct": metric_value(entry, "max_drawdown_pct", "median_drawdown", "avg_drawdown"),
            "max_loss_streak": metric_value(entry, "max_loss_streak"),
            "consistency_score": metric_value(entry, "consistency_score", "avg_consistency"),
        }
        ratio, passed, total = pass_ratio(entry)
        metrics["windows_passed_ratio"] = ratio
        metrics["windows_passed"] = passed
        metrics["windows_total"] = total

        checks = [
            ("total_trades", ">=", self.criteria.min_total_trades, metrics["total_trades"]),
            ("profit_factor", ">=", self.criteria.min_profit_factor, metrics["profit_factor"]),
            ("max_drawdown_pct", "<=", self.criteria.max_drawdown_pct, metrics["max_drawdown_pct"]),
            ("max_loss_streak", "<=", self.criteria.max_loss_streak, metrics["max_loss_streak"]),
            ("consistency_score", ">=", self.criteria.min_consistency_score, metrics["consistency_score"]),
            (
                "windows_passed_ratio",
                ">=",
                self.criteria.min_windows_passed_ratio,
                metrics["windows_passed_ratio"],
            ),
        ]

        missing = [name for name, _, _, actual in checks if actual is None]
        failed = []
        for name, op, expected, actual in checks:
            if actual is None:
                continue
            passed_check = actual >= expected if op == ">=" else actual <= expected
            if not passed_check:
                failed.append(
                    {
                        "metric": name,
                        "actual": actual,
                        "operator": op,
                        "expected": expected,
                    }
                )
        return {"metrics": metrics, "missing": missing, "failed": failed}

    def _missing_metrics_finding(self, entry: dict[str, Any], audit: dict[str, Any]) -> AgentFinding:
        strategy_id = str(entry.get("id", "unknown"))
        return AgentFinding(
            agent=self.name,
            display_name=self.display_name,
            severity=AgentSeverity.WARNING,
            title="Aprovada sem metricas suficientes",
            message=f"{strategy_id} esta aprovada, mas faltam metricas: {', '.join(audit['missing'])}.",
            action="Revalidar a estrategia com validacao completa antes de paper/deploy.",
            metadata={
                "strategy_id": strategy_id,
                "template": entry.get("template"),
                "missing": audit["missing"],
                "metrics": audit["metrics"],
                "fingerprint": f"{self.name}:missing_metrics:{strategy_id}",
            },
        )

    def _failed_criteria_finding(self, entry: dict[str, Any], audit: dict[str, Any]) -> AgentFinding:
        strategy_id = str(entry.get("id", "unknown"))
        failed_labels = ", ".join(
            f"{item['metric']}={item['actual']:.3g} {item['operator']} {item['expected']}"
            for item in audit["failed"]
        )
        return AgentFinding(
            agent=self.name,
            display_name=self.display_name,
            severity=AgentSeverity.WARNING,
            title="Aprovada viola criterios do Juiz",
            message=f"{strategy_id} falhou em: {failed_labels}.",
            action="Rebaixar para inconclusive/rejected ou rodar nova validacao antes de paper.",
            metadata={
                "strategy_id": strategy_id,
                "template": entry.get("template"),
                "failed": audit["failed"],
                "metrics": audit["metrics"],
                "criteria": self.criteria.__dict__,
                "fingerprint": f"{self.name}:failed_criteria:{strategy_id}",
            },
        )

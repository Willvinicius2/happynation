from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.base import AgentFinding, AgentReport, AgentSeverity, BotAgent
from src.agents.aether_researcher_agent import AetherResearcherAgent
from src.agents.config_agent import ConfigAgent
from src.agents.data_quality_agent import DataQualityAgent
from src.agents.drift_agent import DriftAgent
from src.agents.execution_agent import ExecutionAgent
from src.agents.health_agent import HealthAgent
from src.agents.market_regime_agent import MarketRegimeAgent
from src.agents.performance_agent import PerformanceAgent
from src.agents.policy_agent import PolicyAgent
from src.agents.portfolio_agent import PortfolioAgent
from src.agents.research_agent import ResearchAgent
from src.agents.risk_agent import RiskAgent
from src.agents.security_agent import SecurityAgent
from src.agents.strategy_researcher_agent import StrategyResearcherAgent
from src.agents.strategy_validator_agent import StrategyValidatorAgent


DEFAULT_REPORT_PATH = Path("src/logs/agents/latest_report.json")


class AgentOrchestrator:
    def __init__(self, agents: list[BotAgent], report_path: Path | str = DEFAULT_REPORT_PATH) -> None:
        self.agents = agents
        self.report_path = Path(report_path)

    def run(self, write_report: bool = True) -> AgentReport:
        findings: list[AgentFinding] = []
        for agent in self.agents:
            try:
                if hasattr(agent, "run_with_context"):
                    findings.extend(agent.run_with_context(findings))  # type: ignore[attr-defined]
                else:
                    findings.extend(agent.run())
            except Exception as exc:
                findings.append(
                    AgentFinding(
                        agent=getattr(agent, "name", agent.__class__.__name__),
                        severity=AgentSeverity.CRITICAL,
                        title="Agente falhou",
                        message=f"{agent.__class__.__name__}: {exc}",
                        action="Verificar excecao antes de confiar no relatorio.",
                    )
                )
        report = self._build_report_with_diff(findings)
        if write_report:
            self.write_report(report)
        return report

    def write_report(self, report: AgentReport) -> None:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _build_report_with_diff(self, findings: list[AgentFinding]) -> AgentReport:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        previous = self._load_previous_findings()
        current_by_key = {finding.fingerprint(): finding for finding in findings}
        annotated: list[AgentFinding] = []

        for key, finding in current_by_key.items():
            old = previous.get(key)
            if old:
                first_seen_at = old.get("first_seen_at") or old.get("last_seen_at") or now
                status = "persistent"
            else:
                first_seen_at = now
                status = "new"
            annotated.append(
                replace(
                    finding,
                    first_seen_at=first_seen_at,
                    last_seen_at=now,
                    status=status,
                )
            )

        resolved: list[AgentFinding] = []
        for key, old in previous.items():
            if key in current_by_key:
                continue
            resolved.append(self._finding_from_dict(old, fallback_time=now, status="resolved"))

        annotated.sort(key=lambda finding: (finding.severity.value, finding.agent, finding.asset or "", finding.title))
        resolved.sort(key=lambda finding: (finding.agent, finding.asset or "", finding.title))
        return AgentReport(generated_at=now, findings=annotated, resolved_findings=resolved)

    def _load_previous_findings(self) -> dict[str, dict[str, Any]]:
        if not self.report_path.exists():
            return {}
        try:
            data = json.loads(self.report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

        findings = data.get("findings", [])
        if not isinstance(findings, list):
            return {}

        previous: dict[str, dict[str, Any]] = {}
        for item in findings:
            if not isinstance(item, dict):
                continue
            key = item.get("fingerprint")
            if not key:
                key = self._finding_from_dict(item).fingerprint()
            previous[str(key)] = item
        return previous

    def _finding_from_dict(
        self,
        item: dict[str, Any],
        fallback_time: str | None = None,
        status: str | None = None,
    ) -> AgentFinding:
        severity_raw = item.get("severity", AgentSeverity.INFO.value)
        try:
            severity = AgentSeverity(severity_raw)
        except ValueError:
            severity = AgentSeverity.INFO
        return AgentFinding(
            agent=str(item.get("agent", "unknown_agent")),
            severity=severity,
            title=str(item.get("title", "Finding resolvido")),
            message=str(item.get("message", "")),
            asset=item.get("asset"),
            action=item.get("action"),
            metadata=dict(item.get("metadata", {}) or {}),
            first_seen_at=item.get("first_seen_at") or fallback_time,
            last_seen_at=fallback_time or item.get("last_seen_at"),
            status=status or str(item.get("status", "persistent")),
            display_name=item.get("display_name"),
        )


def build_default_orchestrator(report_path: Path | str = DEFAULT_REPORT_PATH) -> AgentOrchestrator:
    return AgentOrchestrator(
        agents=[
            ConfigAgent(),
            SecurityAgent(),
            HealthAgent(),
            StrategyValidatorAgent(),
            StrategyResearcherAgent(),
            ExecutionAgent(),
            DataQualityAgent(),
            DriftAgent(),
            MarketRegimeAgent(),
            RiskAgent(),
            PerformanceAgent(),
            PortfolioAgent(),
            ResearchAgent(),
            PolicyAgent(),
            AetherResearcherAgent(),
        ],
        report_path=report_path,
    )


def run_default_agents(report_path: Path | str = DEFAULT_REPORT_PATH, write_report: bool = True) -> AgentReport:
    return build_default_orchestrator(report_path=report_path).run(write_report=write_report)

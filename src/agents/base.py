from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol


class AgentSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class AgentFinding:
    agent: str
    severity: AgentSeverity
    title: str
    message: str
    asset: str | None = None
    action: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    status: str = "new"
    display_name: str | None = None

    _IDENTITY_METADATA_KEYS = (
        "asset",
        "decision",
        "env_key",
        "field",
        "path",
        "strategy",
        "strategy_id",
        "template",
    )

    def fingerprint(self) -> str:
        """
        Identidade estavel do finding entre execucoes.

        O dashboard/Telegram usa isso para saber se um alerta e novo,
        persistente ou resolvido. Metricas numericas ficam em metadata e
        podem mudar sem transformar o mesmo problema em outro alerta.
        """
        explicit_key = self.metadata.get("fingerprint")
        if explicit_key:
            raw = str(explicit_key)
        else:
            identity_metadata = {
                key: self.metadata[key]
                for key in self._IDENTITY_METADATA_KEYS
                if key in self.metadata
            }
            raw = json.dumps(
                {
                    "agent": self.agent,
                    "severity": self.severity.value,
                    "title": self.title,
                    "asset": self.asset,
                    "metadata": identity_metadata,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        data["fingerprint"] = self.fingerprint()
        return data


@dataclass(frozen=True)
class AgentReport:
    generated_at: str
    findings: list[AgentFinding]
    resolved_findings: list[AgentFinding] = field(default_factory=list)

    @classmethod
    def from_findings(
        cls,
        findings: list[AgentFinding],
        resolved_findings: list[AgentFinding] | None = None,
    ) -> "AgentReport":
        return cls(
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            findings=findings,
            resolved_findings=list(resolved_findings or []),
        )

    def to_dict(self) -> dict[str, Any]:
        counts = {severity.value: 0 for severity in AgentSeverity}
        for finding in self.findings:
            counts[finding.severity.value] += 1
        diff_counts = {"new": 0, "persistent": 0, "resolved": len(self.resolved_findings)}
        for finding in self.findings:
            if finding.status in diff_counts:
                diff_counts[finding.status] += 1
        return {
            "generated_at": self.generated_at,
            "counts": counts,
            "diff_counts": diff_counts,
            "findings": [finding.to_dict() for finding in self.findings],
            "resolved_findings": [finding.to_dict() for finding in self.resolved_findings],
        }


class BotAgent(Protocol):
    name: str

    def run(self) -> list[AgentFinding]:
        """Executa uma avaliacao e retorna achados acionaveis."""

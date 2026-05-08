from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

from src.agents.base import AgentFinding, AgentSeverity
from src.agents.paper_state import load_paper_state


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_finite(value) -> bool:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


class DataQualityAgent:
    name = "data_quality_agent"
    display_name = "Inspetor"

    REQUIRED_POSITION_FIELDS = ("entry_price", "qty", "time")

    def __init__(
        self,
        state_path: Path | str = "src/logs/paper_balance/state.json",
    ) -> None:
        self.state_path = Path(state_path)

    def run(self) -> list[AgentFinding]:
        if not self.state_path.exists():
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Sem estado para inspecionar",
                    message=f"Nao encontrei {self.state_path}.",
                )
            ]

        state = load_paper_state(self.state_path)
        if not state:
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Estado vazio",
                    message="Nenhum ativo no estado de paper trading.",
                )
            ]

        findings: list[AgentFinding] = []
        for asset, raw in sorted(state.items()):
            findings.extend(self._inspect_asset(asset, raw))

        if not findings:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Inspecao limpa",
                    message="Estado de cada ativo coerente.",
                )
            )
        return findings

    def _inspect_asset(self, asset: str, raw) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        if not isinstance(raw, dict):
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.CRITICAL,
                    title="Estado de ativo invalido",
                    message=f"{asset} nao tem objeto valido em state.json.",
                    asset=asset,
                    action="Restaurar backup do state.json antes de operar.",
                    metadata={"asset": asset},
                )
            )
            return findings

        balance = raw.get("balance")
        initial = raw.get("initial_balance")
        for label, value in (("balance", balance), ("initial_balance", initial)):
            if value is None:
                continue
            if not _is_finite(value):
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.CRITICAL,
                        title="Valor numerico invalido",
                        message=f"{asset}.{label}={value!r} nao e numero finito.",
                        asset=asset,
                        action="Restaurar backup do state.json e investigar como NaN/Inf chegou la.",
                        metadata={"asset": asset, "field": label, "value": str(value)},
                    )
                )

        if _is_finite(balance) and float(balance) < 0:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.CRITICAL,
                    title="Balance negativo",
                    message=f"{asset}.balance={float(balance):.4f}",
                    asset=asset,
                    action="Pausar imediatamente; balance negativo indica bug de calculo.",
                    metadata={"asset": asset, "balance": float(balance)},
                )
            )

        total_trades = raw.get("total_trades")
        wins = raw.get("wins")
        losses = raw.get("losses")
        if all(isinstance(v, int) for v in (total_trades, wins, losses)):
            if (wins or 0) + (losses or 0) > (total_trades or 0):
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.WARNING,
                        title="Contadores inconsistentes",
                        message=f"{asset} wins({wins})+losses({losses}) > total_trades({total_trades}).",
                        asset=asset,
                        action="Reconciliar contadores no state.json.",
                        metadata={
                            "asset": asset,
                            "total_trades": total_trades,
                            "wins": wins,
                            "losses": losses,
                        },
                    )
                )

        risk = raw.get("risk", {}) or {}
        consecutive = risk.get("consecutive_losses")
        if isinstance(consecutive, int) and isinstance(total_trades, int):
            if consecutive > total_trades:
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.WARNING,
                        title="Sequencia de perdas impossivel",
                        message=f"{asset} consecutive_losses={consecutive} > total_trades={total_trades}.",
                        asset=asset,
                        action="Resetar consecutive_losses no risk guard.",
                        metadata={
                            "asset": asset,
                            "consecutive_losses": consecutive,
                            "total_trades": total_trades,
                        },
                    )
                )

        position = raw.get("position")
        if position:
            if not isinstance(position, dict):
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.CRITICAL,
                        title="Posicao com formato invalido",
                        message=f"{asset}.position nao e objeto.",
                        asset=asset,
                        action="Restaurar backup; o bot pode nao conseguir fechar a posicao.",
                        metadata={"asset": asset},
                    )
                )
            else:
                missing = [
                    field for field in self.REQUIRED_POSITION_FIELDS if field not in position
                ]
                if missing:
                    findings.append(
                        AgentFinding(
                            agent=self.name,
                            display_name=self.display_name,
                            severity=AgentSeverity.CRITICAL,
                            title="Posicao sem campos obrigatorios",
                            message=f"{asset}.position faltando {missing}.",
                            asset=asset,
                            action="Reconciliar posicao manualmente antes de proximo ciclo.",
                            metadata={"asset": asset, "missing_fields": missing},
                        )
                    )
                entry_price = position.get("entry_price")
                qty = position.get("qty")
                for label, value in (("entry_price", entry_price), ("qty", qty)):
                    if value is None:
                        continue
                    if not _is_finite(value) or float(value) <= 0:
                        findings.append(
                            AgentFinding(
                                agent=self.name,
                                display_name=self.display_name,
                                severity=AgentSeverity.CRITICAL,
                                title="Campo numerico de posicao invalido",
                                message=f"{asset}.position.{label}={value!r}",
                                asset=asset,
                                action="Investigar como o valor foi gravado; bot pode falhar ao sair.",
                                metadata={"asset": asset, "field": label, "value": str(value)},
                            )
                        )
                if _parse_iso(position.get("time")) is None:
                    findings.append(
                        AgentFinding(
                            agent=self.name,
                            display_name=self.display_name,
                            severity=AgentSeverity.WARNING,
                            title="Timestamp de posicao ilegivel",
                            message=f"{asset}.position.time={position.get('time')!r}",
                            asset=asset,
                            action="Corrigir formato ISO do campo time.",
                            metadata={"asset": asset, "time": str(position.get("time"))},
                        )
                    )

        return findings

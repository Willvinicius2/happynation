from __future__ import annotations

from pathlib import Path

from src.agents.base import AgentFinding, AgentSeverity
from src.agents.paper_state import asset_metrics, load_paper_state


class RiskAgent:
    name = "risk_agent"
    display_name = "Sentinela"

    def __init__(
        self,
        state_path: Path | str = "src/logs/paper_balance/state.json",
        max_daily_loss_pct: float = 1.25,
        max_drawdown_pct: float = 3.0,
        max_consecutive_losses: int = 2,
    ) -> None:
        self.state_path = Path(state_path)
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.max_consecutive_losses = max_consecutive_losses

    def run(self) -> list[AgentFinding]:
        state = load_paper_state(self.state_path)
        if not state:
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Sem estado de paper trading",
                    message=f"Nao encontrei estado em {self.state_path}.",
                    action="Rodar o bot em PAPER_TRADING=true antes de avaliar risco operacional.",
                )
            ]

        findings: list[AgentFinding] = []
        meta = state.get("_meta") if isinstance(state, dict) else None
        for asset, raw in sorted(state.items()):
            if asset == "_meta" or not isinstance(raw, dict):
                continue
            metrics = asset_metrics(asset, raw, meta)
            findings.extend(self._evaluate_asset(metrics))

        if not findings:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Risco dentro dos limites",
                    message="Nenhum ativo ultrapassou os limites configurados de perda ou drawdown.",
                )
            )
        return findings

    def _evaluate_asset(self, metrics: dict) -> list[AgentFinding]:
        asset = metrics["asset"]
        findings: list[AgentFinding] = []

        if metrics["daily_pnl_pct"] <= -self.max_daily_loss_pct:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.CRITICAL,
                    title="Perda diaria acima do limite",
                    message=f"{asset} esta com {metrics['daily_pnl_pct']:.2f}% no dia.",
                    asset=asset,
                    action="Pausar novas entradas nesse ativo ate o proximo ciclo diario.",
                    metadata=metrics,
                )
            )

        if metrics["drawdown_pct"] >= self.max_drawdown_pct:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.CRITICAL,
                    title="Drawdown acima do limite",
                    message=f"{asset} esta com drawdown de {metrics['drawdown_pct']:.2f}%.",
                    asset=asset,
                    action="Remover o ativo do portfolio ativo ou reduzir exposicao.",
                    metadata=metrics,
                )
            )

        if metrics["consecutive_losses"] >= self.max_consecutive_losses:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Sequencia de perdas detectada",
                    message=f"{asset} tem {metrics['consecutive_losses']} perdas seguidas.",
                    asset=asset,
                    action="Bloquear reentrada ate nova validacao ou troca de estrategia.",
                    metadata=metrics,
                )
            )

        if metrics["last_block_reason"]:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Risk guard bloqueou entrada",
                    message=f"{asset}: {metrics['last_block_reason']}.",
                    asset=asset,
                    action="Manter bloqueio enquanto a condicao persistir.",
                    metadata=metrics,
                )
            )

        return findings

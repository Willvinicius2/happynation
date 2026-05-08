from __future__ import annotations

from pathlib import Path

from src.agents.base import AgentFinding, AgentSeverity
from src.agents.paper_state import asset_metrics, load_paper_state


class PerformanceAgent:
    name = "performance_agent"
    display_name = "Analista"

    def __init__(
        self,
        state_path: Path | str = "src/logs/paper_balance/state.json",
        min_trades: int = 3,
        min_winrate_pct: float = 45.0,
        min_pnl_pct: float = 0.0,
    ) -> None:
        self.state_path = Path(state_path)
        self.min_trades = min_trades
        self.min_winrate_pct = min_winrate_pct
        self.min_pnl_pct = min_pnl_pct

    def run(self) -> list[AgentFinding]:
        state = load_paper_state(self.state_path)
        findings: list[AgentFinding] = []

        if not state:
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Sem performance acumulada",
                    message="Ainda nao ha historico de paper trading para avaliar performance.",
                )
            ]

        meta = state.get("_meta") if isinstance(state, dict) else None
        for asset, raw in sorted(state.items()):
            if asset == "_meta" or not isinstance(raw, dict):
                continue
            metrics = asset_metrics(asset, raw, meta)
            if metrics["total_trades"] < self.min_trades:
                continue
            findings.extend(self._evaluate_asset(metrics))

        if not findings:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Performance sem alerta",
                    message="Ativos com amostra suficiente seguem dentro dos limites de performance.",
                )
            )
        return findings

    def _evaluate_asset(self, metrics: dict) -> list[AgentFinding]:
        asset = metrics["asset"]
        findings: list[AgentFinding] = []

        if metrics["winrate_pct"] < self.min_winrate_pct:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Win rate abaixo do minimo",
                    message=(
                        f"{asset} esta com WR {metrics['winrate_pct']:.1f}% "
                        f"em {metrics['total_trades']} trades."
                    ),
                    asset=asset,
                    action="Revalidar estrategia do ativo antes de aumentar exposicao.",
                    metadata=metrics,
                )
            )

        if metrics["pnl_pct"] < self.min_pnl_pct:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="PnL acumulado negativo",
                    message=f"{asset} esta com PnL {metrics['pnl_pct']:.2f}% no paper.",
                    asset=asset,
                    action="Comparar com backtest recente e considerar remover do portfolio.",
                    metadata=metrics,
                )
            )

        return findings

from __future__ import annotations

from pathlib import Path

from src.agents.base import AgentFinding, AgentSeverity
from src.agents.paper_state import DEFAULT_PAPER_STATE_PATH, asset_metrics, load_paper_state


class PortfolioAgent:
    name = "portfolio_agent"
    display_name = "Gerente"

    def __init__(
        self,
        state_path: Path | str = DEFAULT_PAPER_STATE_PATH,
        max_asset_weight_pct: float = 45.0,
        weak_asset_weight_pct: float = 25.0,
        weak_asset_pnl_pct: float = 0.0,
        weak_asset_winrate_pct: float = 45.0,
        min_trades_for_weak_signal: int = 3,
    ) -> None:
        self.state_path = Path(state_path)
        self.max_asset_weight_pct = max_asset_weight_pct
        self.weak_asset_weight_pct = weak_asset_weight_pct
        self.weak_asset_pnl_pct = weak_asset_pnl_pct
        self.weak_asset_winrate_pct = weak_asset_winrate_pct
        self.min_trades_for_weak_signal = min_trades_for_weak_signal

    def run(self) -> list[AgentFinding]:
        state = load_paper_state(self.state_path)
        if not state:
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Sem portfolio para avaliar",
                    message="Ainda nao ha estado de paper trading para calcular exposicao.",
                    action="Rodar o bot em paper para o Gerente avaliar pesos por ativo.",
                )
            ]

        meta = state.get("_meta") if isinstance(state, dict) else None
        metrics = [
            asset_metrics(asset, raw, meta)
            for asset, raw in sorted(state.items())
            if asset != "_meta" and isinstance(raw, dict)
        ]
        total_reference = self._portfolio_reference(state, metrics)
        if total_reference <= 0:
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Portfolio sem saldo calculavel",
                    message="Nao foi possivel calcular pesos porque o saldo total e zero.",
                    action="Verificar state.json do paper trading.",
                    metadata={"state_path": str(self.state_path)},
                )
            ]

        findings: list[AgentFinding] = []
        enriched = []
        for item in metrics:
            exposure_value = self._asset_exposure_value(item)
            weight_pct = exposure_value / total_reference * 100
            row = dict(item)
            row["exposure_value"] = exposure_value
            row["weight_pct"] = weight_pct
            enriched.append(row)
            findings.extend(self._evaluate_asset(row, total_reference))

        if not findings:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Portfolio balanceado",
                    message="Nenhum ativo ultrapassou limites de concentracao ou peso ruim.",
                    metadata={
                        "total_balance": total_reference,
                        "assets": enriched,
                    },
                )
            )
        return findings

    def _portfolio_reference(self, state: dict, metrics: list[dict]) -> float:
        meta = state.get("_meta") if isinstance(state, dict) else None
        if isinstance(meta, dict):
            equity = meta.get("equity")
            if equity is not None:
                try:
                    parsed = float(equity)
                except (TypeError, ValueError):
                    parsed = 0.0
                if parsed > 0:
                    return parsed
        return sum(self._asset_exposure_value(item) for item in metrics)

    def _asset_exposure_value(self, metrics: dict) -> float:
        market_value = float(metrics.get("market_value") or 0.0)
        allocated_usd = float(metrics.get("allocated_usd") or 0.0)
        balance = float(metrics.get("balance") or 0.0)
        if market_value > 0 or allocated_usd > 0:
            return max(market_value, allocated_usd)
        return balance

    def _evaluate_asset(self, metrics: dict, total_balance: float) -> list[AgentFinding]:
        asset = metrics["asset"]
        weight_pct = float(metrics["weight_pct"])
        findings: list[AgentFinding] = []

        if weight_pct > self.max_asset_weight_pct:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Concentracao alta em ativo",
                    message=f"{asset} representa {weight_pct:.1f}% do portfolio paper.",
                    asset=asset,
                    action="Reduzir exposicao ou redistribuir peso entre ativos.",
                    metadata={
                        **metrics,
                        "total_balance": total_balance,
                        "limit_pct": self.max_asset_weight_pct,
                        "fingerprint": f"{self.name}:high_concentration:{asset}",
                    },
                )
            )

        has_sample = int(metrics["total_trades"]) >= self.min_trades_for_weak_signal
        weak_performance = (
            float(metrics["pnl_pct"]) < self.weak_asset_pnl_pct
            or float(metrics["winrate_pct"]) < self.weak_asset_winrate_pct
        )
        if has_sample and weak_performance and weight_pct > self.weak_asset_weight_pct:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Ativo fraco com peso alto",
                    message=(
                        f"{asset} pesa {weight_pct:.1f}% com PnL {metrics['pnl_pct']:.2f}% "
                        f"e WR {metrics['winrate_pct']:.1f}%."
                    ),
                    asset=asset,
                    action="Reduzir peso ate nova validacao pelo Juiz e pelo Analista.",
                    metadata={
                        **metrics,
                        "total_balance": total_balance,
                        "weak_weight_limit_pct": self.weak_asset_weight_pct,
                        "fingerprint": f"{self.name}:weak_high_weight:{asset}",
                    },
                )
            )

        return findings

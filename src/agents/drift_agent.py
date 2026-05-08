"""DriftAgent ("Farejador").

Compara a performance ao vivo (paper) vs a esperada pelo backtest. Se o
profit_factor cair OU o drawdown subir mais que os thresholds, emite
finding (warning ou critical). PolicyAgent consome esses criticals e dispara
kill switch por asset.

Estrutura real do paper state.json:
  state["_meta"]["strategy_stats"][strategy_name] = {
    trades, wins, losses, pnl_usd, gross_profit, gross_loss, profit_factor, ...
  }
strategy_name no formato "Aether:engine#hashfragment". Mapeamento pra asset
e feito via active_strategies.json (cada record tem "id" no formato
"aether-XXXX" e o "asset"; cruzamos pelo hashfragment final).

Drift por ASSET (nao por strategy isolada): se um asset tem multiplas
strategies, agregamos profit_factor e drawdown ponderado pelo numero de
trades. Faz mais sentido pro PolicyAgent que decide "pausar asset".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agents.base import AgentFinding, AgentSeverity
from src.agents.paper_state import load_paper_state
from src.agents.strategy_catalog_state import catalog_entries, load_json, active_records, metric_value


# Tunaveis -- env vars permitem ajuste sem mudar codigo
import os
_DRIFT_WARN_PCT = float(os.environ.get("DRIFT_WARN_PCT", "10"))
_DRIFT_CRIT_PCT = float(os.environ.get("DRIFT_CRIT_PCT", "25"))
_MIN_TRADES_FOR_DRIFT = int(os.environ.get("DRIFT_MIN_TRADES", "5"))


class DriftAgent:
    name = "drift_agent"
    display_name = "Farejador"

    def __init__(
        self,
        state_path: Path | str = "src/logs/paper_balance/state.json",
        active_path: Path | str = "src/logs/research/active_strategies.json",
        catalog_path: Path | str | None = None,
        min_trades: int | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.active_path = Path(active_path)
        self.catalog_path = Path(catalog_path) if catalog_path is not None else None
        self.min_trades = int(min_trades) if min_trades is not None else _MIN_TRADES_FOR_DRIFT

    def run(self) -> list[AgentFinding]:
        live = self._aggregate_live_by_asset()
        backtest = self._backtest_by_asset()

        if not live and not backtest:
            return [
                AgentFinding(
                    agent=self.name, display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Sem dados pra analise de drift",
                    message="Paper state vazio E active_strategies vazio. Aguardando bot operar.",
                    action="Confirmar que paper bot esta rodando e gerando trades.",
                )
            ]

        # Se ainda nao houve trade fechado em nenhum asset (totalmente novo),
        # emite info ao inves de comparar contra strategy_stats vazio.
        total_trades_live = sum(int(d.get("trades") or 0) for d in live.values())
        if total_trades_live == 0:
            return [
                AgentFinding(
                    agent=self.name, display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Aguardando primeiros trades",
                    message=f"{len(backtest)} estrategia(s) backtest ativas, 0 trades fechados ao vivo ainda.",
                    action="Bot esta operando -- analise de drift ativa quando trades fecharem.",
                    metadata={"backtest_assets": list(backtest.keys())},
                )
            ]

        out: list[AgentFinding] = []
        # Evaluar cada asset que aparece em ambos
        for asset, live_data in live.items():
            if asset not in backtest:
                continue
            trades = int(live_data.get("trades") or 0)
            if trades < self.min_trades:
                # Amostra fraca -- emite INFO mas nao gera critical
                out.append(AgentFinding(
                    agent=self.name, display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title=f"Drift inconclusivo em {asset}",
                    message=f"{trades} trade(s) ao vivo (< {self.min_trades} minimo). Aguardando amostra maior.",
                    asset=asset,
                    metadata={"trades_live": trades, "min_trades": self.min_trades},
                ))
                continue

            bt = backtest[asset]
            live_pf = float(live_data.get("profit_factor") or 0.0)
            bt_pf = float(bt.get("profit_factor") or 1.0)
            live_dd = float(live_data.get("drawdown_pct") or 0.0)
            bt_dd = float(bt.get("drawdown_pct") or 0.0)

            pf_drop_pct = 0.0
            if bt_pf > 0 and live_pf < bt_pf:
                pf_drop_pct = (bt_pf - live_pf) / bt_pf * 100
            dd_excess_pct = 0.0
            if bt_dd > 0 and live_dd > bt_dd:
                dd_excess_pct = (live_dd - bt_dd) / bt_dd * 100

            max_drift = max(pf_drop_pct, dd_excess_pct)
            metadata = {
                "trades_live": trades,
                "live_profit_factor": round(live_pf, 3),
                "bt_profit_factor": round(bt_pf, 3),
                "pf_drop_pct": round(pf_drop_pct, 1),
                "live_drawdown_pct": round(live_dd, 2),
                "bt_drawdown_pct": round(bt_dd, 2),
                "dd_excess_pct": round(dd_excess_pct, 1),
                "winrate_pct_live": round(float(live_data.get("winrate_pct") or 0.0), 1),
            }

            if max_drift >= _DRIFT_CRIT_PCT:
                out.append(AgentFinding(
                    agent=self.name, display_name=self.display_name,
                    severity=AgentSeverity.CRITICAL,
                    title=f"Drift severo em {asset}",
                    message=f"Performance ao vivo divergiu {max_drift:.1f}% do backtest (PF live={live_pf:.2f} vs bt={bt_pf:.2f}).",
                    action=f"Pausar {asset} ate revalidar (kill graceful sera disparado pelo policy_agent).",
                    asset=asset, metadata=metadata,
                ))
            elif max_drift >= _DRIFT_WARN_PCT:
                out.append(AgentFinding(
                    agent=self.name, display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title=f"Drift moderado em {asset}",
                    message=f"Divergencia {max_drift:.1f}% ainda dentro do tolerado, mas tendencia de degradacao.",
                    action="Monitorar proximos trades. Sem acao automatica.",
                    asset=asset, metadata=metadata,
                ))
            else:
                out.append(AgentFinding(
                    agent=self.name, display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title=f"Estavel em {asset}",
                    message=f"Live alinhado com backtest (drift {max_drift:.1f}% < {_DRIFT_WARN_PCT}%).",
                    asset=asset, metadata=metadata,
                ))

        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _strategy_id_to_asset(self) -> dict[str, str]:
        """Mapeia 'Aether:engine#hashfrag' -> 'ETHUSDT' usando active_strategies."""
        mapping: dict[str, str] = {}
        try:
            data = load_json(self.active_path, {"strategies": []})
        except Exception:
            return mapping
        for s in (data.get("strategies") or []):
            sid = s.get("id") or ""
            asset = (s.get("asset") or "").upper()
            engine = s.get("engine") or ""
            if not sid or not asset or not engine:
                continue
            # active_strategies usa "aether-<14 hex>"; o paper_state usa
            # "Aether:<engine>#<8 hex>". O hashfrag final do paper e os
            # ultimos 8 chars do id active. Construimos mapping por essa chave.
            tail = sid.replace("aether-", "")[-8:]
            paper_key = f"Aether:{engine}#{tail}"
            mapping[paper_key] = asset
        return mapping

    def _aggregate_live_by_asset(self) -> dict[str, dict[str, float]]:
        state = load_paper_state(self.state_path)
        if not state:
            return {}
        meta = state.get("_meta") or {}
        stats_map = meta.get("strategy_stats") or {}
        if not stats_map:
            return self._aggregate_legacy_state_by_asset(state, meta)
        sid_to_asset = self._strategy_id_to_asset()
        portfolio_drawdown_pct = self._portfolio_drawdown_pct(meta)

        per_asset: dict[str, dict[str, Any]] = {}
        for sid, stats in stats_map.items():
            asset = sid_to_asset.get(sid)
            if not asset:
                continue
            agg = per_asset.setdefault(asset, {
                "trades": 0, "wins": 0, "losses": 0,
                "gross_profit": 0.0, "gross_loss": 0.0, "pnl_usd": 0.0,
            })
            agg["trades"]       += int(stats.get("trades") or 0)
            agg["wins"]         += int(stats.get("wins") or 0)
            agg["losses"]       += int(stats.get("losses") or 0)
            agg["gross_profit"] += float(stats.get("gross_profit") or 0.0)
            agg["gross_loss"]   += float(stats.get("gross_loss") or 0.0)
            agg["pnl_usd"]      += float(stats.get("pnl_usd") or 0.0)

        # O estado por asset guarda caixa/exposicao parcial. Usar balance vs
        # initial aqui gera drawdowns falsos quando ha banca global em _meta.
        for asset, agg in per_asset.items():
            agg["drawdown_pct"] = portfolio_drawdown_pct
            gp = agg["gross_profit"]; gl = abs(agg["gross_loss"])
            if gl > 0:
                agg["profit_factor"] = gp / gl
            elif gp > 0:
                agg["profit_factor"] = 99.0
            else:
                agg["profit_factor"] = 0.0
            agg["winrate_pct"] = (agg["wins"] / agg["trades"] * 100) if agg["trades"] else 0.0
        return per_asset

    def _portfolio_drawdown_pct(self, meta: dict[str, Any]) -> float:
        try:
            bankroll = float(meta.get("total_bankroll") or 0.0)
            equity = meta.get("equity")
            if bankroll > 0 and equity is not None:
                return max((bankroll - float(equity)) / bankroll * 100, 0.0)
        except (TypeError, ValueError):
            return 0.0
        return 0.0

    def _aggregate_legacy_state_by_asset(self, state: dict[str, Any], meta: dict[str, Any]) -> dict[str, dict[str, float]]:
        portfolio_drawdown_pct = self._portfolio_drawdown_pct(meta)
        out: dict[str, dict[str, float]] = {}
        for asset, raw in state.items():
            if asset == "_meta" or not isinstance(raw, dict):
                continue
            trades = int(raw.get("total_trades") or 0)
            wins = int(raw.get("wins") or 0)
            losses = int(raw.get("losses") or 0)
            gross_profit = float(raw.get("gross_profit") or max(wins, 0))
            gross_loss = abs(float(raw.get("gross_loss") or max(losses, 0)))
            if gross_loss > 0:
                profit_factor = gross_profit / gross_loss
            elif gross_profit > 0:
                profit_factor = 99.0
            else:
                profit_factor = 0.0
            if portfolio_drawdown_pct > 0:
                drawdown_pct = portfolio_drawdown_pct
            else:
                initial = float(raw.get("initial_balance") or 0.0)
                balance = float(raw.get("balance") or 0.0)
                drawdown_pct = max((initial - balance) / initial * 100, 0.0) if initial > 0 else 0.0
            out[str(asset).upper()] = {
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "gross_profit": gross_profit,
                "gross_loss": gross_loss,
                "pnl_usd": float(raw.get("total_pnl_usd") or raw.get("pnl_usd") or 0.0),
                "profit_factor": profit_factor,
                "drawdown_pct": drawdown_pct,
                "winrate_pct": (wins / trades * 100) if trades else 0.0,
            }
        return out

    def _backtest_by_asset(self) -> dict[str, dict[str, float]]:
        """Stats agregadas por asset a partir do active_strategies.json.

        Quando profit_factor nao esta presente (caso atual do auto_deploy),
        usa hit_rate (winrate) como proxy: PF aproximado por
            pf = (hit_rate / (1 - hit_rate))  -- com avg_win=avg_loss assumed
        Quando hit_rate tambem ausente, default neutro 1.0.
        """
        try:
            data = load_json(self.active_path, {"strategies": []})
        except Exception:
            return {}
        records = active_records(data)
        per_asset: dict[str, dict[str, float]] = {}
        for r in records:
            asset = (r.get("asset") or r.get("symbol") or "").upper()
            if not asset:
                continue
            pf = metric_value(r, "profit_factor", "median_profit_factor", "avg_profit_factor")
            dd = metric_value(r, "max_drawdown_pct", "median_drawdown", "avg_drawdown", "mdd_pct")
            hit = metric_value(r, "hit_rate", "winrate", "winrate_pct")
            # Se PF nao disponivel mas hit_rate sim, aproxima
            if pf is None and hit is not None:
                hr = float(hit)
                if hr > 1.5:  # veio em %, normaliza
                    hr = hr / 100
                if 0.0 < hr < 1.0:
                    # Avg-win = Avg-loss assumption (conservador)
                    pf = hr / (1.0 - hr) if hr < 1.0 else 99.0
            cur = per_asset.setdefault(asset, {"profit_factor": 0.0, "drawdown_pct": 0.0, "winrate_pct": 0.0, "n": 0})
            if pf is not None:
                cur["profit_factor"] = (cur["profit_factor"] * cur["n"] + float(pf)) / (cur["n"] + 1)
            if dd is not None:
                cur["drawdown_pct"] = max(cur["drawdown_pct"], abs(float(dd)))
            if hit is not None:
                hr_norm = float(hit) * 100 if float(hit) <= 1.5 else float(hit)
                cur["winrate_pct"] = (cur["winrate_pct"] * cur["n"] + hr_norm) / (cur["n"] + 1)
            cur["n"] += 1
        for v in per_asset.values():
            v.pop("n", None)
            if not v.get("profit_factor"):
                v["profit_factor"] = 1.0  # default neutro quando metric ausente
        if not per_asset and self.catalog_path is not None:
            return self._legacy_backtest_by_asset()
        return per_asset

    def _legacy_backtest_by_asset(self) -> dict[str, dict[str, float]]:
        if self.catalog_path is None:
            return {}
        strategy_assets = self._legacy_strategy_assets()
        catalog = load_json(self.catalog_path, {"entries": []})
        out: dict[str, dict[str, float]] = {}
        for entry in catalog_entries(catalog):
            strategy_id = str(entry.get("id") or "")
            name = str(entry.get("name") or "")
            asset = (
                entry.get("asset")
                or entry.get("symbol")
                or strategy_assets.get(strategy_id)
                or strategy_assets.get(name)
            )
            if not asset:
                continue
            pf = metric_value(entry, "profit_factor", "median_profit_factor", "avg_profit_factor")
            dd = metric_value(entry, "max_drawdown_pct", "median_drawdown", "avg_drawdown", "mdd_pct")
            hit = metric_value(entry, "hit_rate", "winrate", "winrate_pct")
            out[str(asset).upper()] = {
                "profit_factor": float(pf) if pf is not None else 1.0,
                "drawdown_pct": abs(float(dd)) if dd is not None else 0.0,
                "winrate_pct": float(hit) if hit is not None else 0.0,
            }
        return out

    def _legacy_strategy_assets(self) -> dict[str, str]:
        state = load_paper_state(self.state_path)
        mapping: dict[str, str] = {}
        for asset, raw in state.items():
            if asset == "_meta" or not isinstance(raw, dict):
                continue
            strategy = raw.get("strategy")
            if strategy:
                mapping[str(strategy)] = str(asset).upper()
        return mapping

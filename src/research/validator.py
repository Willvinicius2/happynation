"""
Validador multi-cenario para candidatos do pesquisador.

Cada candidato eh testado em uma matriz de cenarios:
  - 5 ativos (ou quantos forem informados)
  - 2 niveis de fee (configurable)
  - 3 splits temporais por par (full / oos_train 60% / oos_test 40%)

Total padrao: 5 × 2 × 3 = 30 backtests independentes por candidato.

Aprovado se passa em todos os criterios:
  1. >= X% dos cenarios "full" passam pelos filtros do engine
  2. winrate medio (full) >= min
  3. profit factor medio (full) >= min
  4. drawdown medio (full) <= max
  5. trades medios (full) >= min
  6. PF do conjunto OOS-test >= 1.0 (nao despencou no fora-da-amostra)
  7. winrate do OOS-test >= 45
  8. overfit: queda do PF entre train e test <= 0.6
  9. overfit: queda do winrate entre train e test <= 12 pontos

A lista de criterios checados eh anexada ao relatorio para o dashboard
exibir exatamente o que passou e o que falhou.
"""

from __future__ import annotations

import hashlib
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from src.modules.StrategyResearchEngine import (
    ResearchRules,
    StrategySpec,
    StrategyResearchResult,
    run_strategy_backtest,
    validate_strategy,
)


@dataclass
class ScenarioMetric:
    name: str
    asset: str
    fee_pct: float
    split: str   # "full" | "oos_train" | "oos_test"
    passed: bool
    score: float
    winrate_pct: float
    profit_factor: float
    max_drawdown_pct: float
    total_trades: int
    total_profit_pct: float
    consistency_score: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


@dataclass
class CriteriaCheck:
    name: str
    value: float
    op: str             # ">=" | "<="
    threshold: float
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


@dataclass
class ValidationReport:
    strategy_id: str
    template: str
    params: dict[str, Any]
    risk: dict[str, Any]
    scenarios: list[ScenarioMetric] = field(default_factory=list)
    criteria_checks: list[CriteriaCheck] = field(default_factory=list)
    score: float = 0.0
    summary: dict[str, Any] = field(default_factory=dict)
    approved: bool = False
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "template": self.template,
            "params": self.params,
            "risk": self.risk,
            "scenarios": [s.to_dict() for s in self.scenarios],
            "criteria_checks": [c.to_dict() for c in self.criteria_checks],
            "score": self.score,
            "summary": self.summary,
            "approved": self.approved,
            "rejection_reason": self.rejection_reason,
        }


@dataclass(frozen=True)
class ValidationCriteria:
    min_scenarios_passed_ratio: float = 0.4
    # Usamos MEDIANA (robusta a outliers) — PF clipado em 5
    min_median_winrate: float = 45.0
    min_median_profit_factor: float = 1.15
    max_median_drawdown_pct: float = 25.0
    min_total_trades_avg: float = 10.0
    fee_levels: tuple[float, ...] = (0.05, 0.1, 0.2)
    # OOS / overfit (mediana)
    min_oos_test_profit_factor: float = 1.05
    min_oos_test_winrate: float = 44.0
    max_overfit_pf_drop: float = 0.5
    max_overfit_winrate_drop: float = 10.0
    # Recente (ultimos 30%)
    min_oos_recent_winrate: float = 42.0
    # Monte Carlo bootstrap drawdown
    monte_carlo_iters: int = 200
    max_mc_drawdown_ratio: float = 2.0
    train_ratio: float = 0.6
    recent_ratio: float = 0.7
    max_scenario_workers: int = 4
    # campos legados (compatibilidade)
    min_avg_winrate: float = 0.0
    min_avg_profit_factor: float = 0.0
    max_avg_drawdown_pct: float = 999.0


# Filtro proprio do cenario — independe das regras agressivas de janela
# do engine. Um cenario eh "saudavel" se atende todos abaixo.
# Calibrado para cripto 1h: winrate natural fica entre 38-50%, entao 38 eh o minimo.
SCENARIO_MIN_TRADES = 5
SCENARIO_MIN_WINRATE = 38.0
SCENARIO_MIN_PF = 1.0
SCENARIO_MAX_DD = 32.0
PF_CAP = 5.0  # clip em PF para nao poluir agregacoes com outliers (sem losses)


def _clip_pf(pf: float) -> float:
    return min(float(pf), PF_CAP)


def _scenario_passed(result: StrategyResearchResult) -> bool:
    if result.error:
        return False
    if result.total_trades < SCENARIO_MIN_TRADES:
        return False
    if result.winrate_pct < SCENARIO_MIN_WINRATE:
        return False
    if result.profit_factor < SCENARIO_MIN_PF:
        return False
    if result.max_drawdown_pct > SCENARIO_MAX_DD:
        return False
    return True


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return float(s[n // 2])
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _result_to_metric(result: StrategyResearchResult, scenario_name: str, fee_pct: float, split: str) -> ScenarioMetric:
    return ScenarioMetric(
        name=scenario_name,
        asset=result.asset,
        fee_pct=fee_pct,
        split=split,
        passed=_scenario_passed(result),
        score=float(result.score),
        winrate_pct=float(result.winrate_pct),
        profit_factor=float(result.profit_factor),
        max_drawdown_pct=float(result.max_drawdown_pct),
        total_trades=int(result.total_trades),
        total_profit_pct=float(result.total_profit_pct),
        consistency_score=float(result.consistency_score),
        error=result.error,
    )


def _aggregate(metrics: list[ScenarioMetric]) -> dict[str, Any]:
    valid = [m for m in metrics if not m.error]
    if not valid:
        return {
            "scenarios_total": len(metrics), "scenarios_passed": 0,
            "avg_winrate": 0.0, "median_winrate": 0.0,
            "avg_profit_factor": 0.0, "median_profit_factor": 0.0,
            "avg_drawdown": 0.0, "median_drawdown": 0.0,
            "avg_score": 0.0, "avg_trades": 0.0, "avg_consistency": 0.0,
        }
    n = len(valid)
    pfs_clipped = [_clip_pf(m.profit_factor) for m in valid]
    winrates = [m.winrate_pct for m in valid]
    drawdowns = [m.max_drawdown_pct for m in valid]
    return {
        "scenarios_total": len(metrics),
        "scenarios_passed": sum(1 for m in metrics if m.passed),
        "avg_winrate": sum(winrates) / n,
        "median_winrate": _median(winrates),
        "avg_profit_factor": sum(pfs_clipped) / n,
        "median_profit_factor": _median(pfs_clipped),
        "avg_drawdown": sum(drawdowns) / n,
        "median_drawdown": _median(drawdowns),
        "avg_score": sum(m.score for m in valid) / n,
        "avg_trades": sum(m.total_trades for m in valid) / n,
        "avg_consistency": sum(m.consistency_score for m in valid) / n,
    }


def _build_checks(
    summary: dict[str, Any],
    train_summary: dict[str, Any],
    test_summary: dict[str, Any],
    recent_summary: dict[str, Any],
    mc_ratio: float,
    criteria: ValidationCriteria,
) -> list[CriteriaCheck]:
    pass_ratio = summary["scenarios_passed"] / max(summary["scenarios_total"], 1)
    # Usamos MEDIANA para overfit tambem
    overfit_pf = train_summary["median_profit_factor"] - test_summary["median_profit_factor"]
    overfit_win = train_summary["median_winrate"] - test_summary["median_winrate"]

    checks: list[CriteriaCheck] = [
        CriteriaCheck("Cenarios saudaveis (full)", round(pass_ratio, 3), ">=",
                      criteria.min_scenarios_passed_ratio,
                      pass_ratio >= criteria.min_scenarios_passed_ratio),
        CriteriaCheck("Winrate mediano (full)", round(summary["median_winrate"], 2), ">=",
                      criteria.min_median_winrate,
                      summary["median_winrate"] >= criteria.min_median_winrate),
        CriteriaCheck("Profit factor mediano (full)", round(summary["median_profit_factor"], 2), ">=",
                      criteria.min_median_profit_factor,
                      summary["median_profit_factor"] >= criteria.min_median_profit_factor),
        CriteriaCheck("Drawdown mediano (full)", round(summary["median_drawdown"], 2), "<=",
                      criteria.max_median_drawdown_pct,
                      summary["median_drawdown"] <= criteria.max_median_drawdown_pct),
        CriteriaCheck("Trades medios (full)", round(summary["avg_trades"], 1), ">=",
                      criteria.min_total_trades_avg,
                      summary["avg_trades"] >= criteria.min_total_trades_avg),
        CriteriaCheck("PF mediano OOS-test", round(test_summary["median_profit_factor"], 2), ">=",
                      criteria.min_oos_test_profit_factor,
                      test_summary["median_profit_factor"] >= criteria.min_oos_test_profit_factor),
        CriteriaCheck("Winrate mediano OOS-test", round(test_summary["median_winrate"], 2), ">=",
                      criteria.min_oos_test_winrate,
                      test_summary["median_winrate"] >= criteria.min_oos_test_winrate),
        CriteriaCheck("Winrate mediano recente (30%)", round(recent_summary["median_winrate"], 2), ">=",
                      criteria.min_oos_recent_winrate,
                      recent_summary["median_winrate"] >= criteria.min_oos_recent_winrate),
        CriteriaCheck("Overfit PF (mediana train-test)", round(overfit_pf, 2), "<=",
                      criteria.max_overfit_pf_drop,
                      overfit_pf <= criteria.max_overfit_pf_drop),
        CriteriaCheck("Overfit Win (mediana train-test)", round(overfit_win, 2), "<=",
                      criteria.max_overfit_winrate_drop,
                      overfit_win <= criteria.max_overfit_winrate_drop),
        CriteriaCheck("Monte Carlo DD ratio (p95/real)", round(mc_ratio, 2), "<=",
                      criteria.max_mc_drawdown_ratio,
                      mc_ratio <= criteria.max_mc_drawdown_ratio),
    ]
    return checks


def _build_full_checks(summary: dict[str, Any], criteria: ValidationCriteria) -> list[CriteriaCheck]:
    pass_ratio = summary["scenarios_passed"] / max(summary["scenarios_total"], 1)
    return [
        CriteriaCheck("Cenarios saudaveis (full)", round(pass_ratio, 3), ">=",
                      criteria.min_scenarios_passed_ratio,
                      pass_ratio >= criteria.min_scenarios_passed_ratio),
        CriteriaCheck("Winrate mediano (full)", round(summary["median_winrate"], 2), ">=",
                      criteria.min_median_winrate,
                      summary["median_winrate"] >= criteria.min_median_winrate),
        CriteriaCheck("Profit factor mediano (full)", round(summary["median_profit_factor"], 2), ">=",
                      criteria.min_median_profit_factor,
                      summary["median_profit_factor"] >= criteria.min_median_profit_factor),
        CriteriaCheck("Drawdown mediano (full)", round(summary["median_drawdown"], 2), "<=",
                      criteria.max_median_drawdown_pct,
                      summary["median_drawdown"] <= criteria.max_median_drawdown_pct),
        CriteriaCheck("Trades medios (full)", round(summary["avg_trades"], 1), ">=",
                      criteria.min_total_trades_avg,
                      summary["avg_trades"] >= criteria.min_total_trades_avg),
    ]


def _rejection_reason(checks: list[CriteriaCheck]) -> str | None:
    failed = [c for c in checks if not c.passed]
    if not failed:
        return None
    return "; ".join(f"{c.name}: {c.value} {c.op} {c.threshold}? NAO" for c in failed[:3])


def quick_filter(
    strategy_id: str,
    template: str,
    params: dict[str, Any],
    risk: dict[str, Any],
    fn,
    sample_data: pd.DataFrame,
    rules: ResearchRules,
) -> tuple[bool, ScenarioMetric]:
    """Backtest rapido em um unico ativo. Corta candidato claramente ruim cedo."""
    spec = StrategySpec(
        name=template, function=fn, params=params,
        take_profit_pct=risk.get("take_profit_pct"),
        stop_loss_pct=risk.get("stop_loss_pct"),
        max_hold_candles=risk.get("max_hold_candles"),
    )
    result = validate_strategy(asset="QUICK", stock_data=sample_data, strategy=spec, rules=rules)
    metric = _result_to_metric(result, scenario_name="quick", fee_pct=rules.trade_fee_pct, split="full")
    if result.error:
        return False, metric
    if result.total_trades < 5:
        return False, metric
    if result.winrate_pct < 35.0:
        return False, metric
    if result.profit_factor < 0.85:
        return False, metric
    if result.max_drawdown_pct > 45.0:
        return False, metric
    return True, metric


def _bootstrap_drawdown_p95(profit_pcts: list[float], iters: int, rng: random.Random) -> tuple[float, float]:
    """Bootstrap: re-amostra ordem dos trades. Retorna (real_dd_pct, p95_mc_dd_pct)."""
    if not profit_pcts:
        return 0.0, 0.0

    def _dd(order: list[float]) -> float:
        equity = 1.0
        peak = 1.0
        worst = 0.0
        for p in order:
            equity *= 1 + (p / 100.0)
            if equity > peak:
                peak = equity
            elif peak > 0:
                worst = max(worst, (peak - equity) / peak * 100)
        return worst

    real_dd = _dd(profit_pcts)
    samples: list[float] = []
    n = len(profit_pcts)
    for _ in range(iters):
        order = [profit_pcts[rng.randrange(n)] for _ in range(n)]
        samples.append(_dd(order))
    samples.sort()
    p95 = samples[int(len(samples) * 0.95)] if samples else 0.0
    return real_dd, p95


def _rules_with_fee(rules: ResearchRules, fee: float) -> ResearchRules:
    return ResearchRules(
        window_days=rules.window_days,
        min_window_profit_pct=rules.min_window_profit_pct,
        min_window_trades=rules.min_window_trades,
        min_total_trades=rules.min_total_trades,
        min_winrate_pct=rules.min_winrate_pct,
        max_drawdown_pct=rules.max_drawdown_pct,
        min_profit_factor=rules.min_profit_factor,
        max_loss_streak=rules.max_loss_streak,
        min_consistency_score=rules.min_consistency_score,
        initial_balance=rules.initial_balance,
        lookback_candles=rules.lookback_candles,
        trade_fee_pct=fee,
    )


def _scenario_jobs(
    data_by_asset: dict[str, pd.DataFrame],
    fee_levels: tuple[float, ...],
    criteria: ValidationCriteria,
    splits_to_run: tuple[str, ...],
) -> list[tuple[float, str, str, pd.DataFrame]]:
    jobs: list[tuple[float, str, str, pd.DataFrame]] = []
    for fee in fee_levels:
        for asset, data in data_by_asset.items():
            n = len(data)
            split_idx_train = max(50, int(n * criteria.train_ratio))
            split_idx_recent = max(60, int(n * criteria.recent_ratio))
            split_map = {
                "full": data,
                "oos_train": data.iloc[:split_idx_train].reset_index(drop=True),
                "oos_test": data.iloc[split_idx_train:].reset_index(drop=True),
                "oos_recent": data.iloc[split_idx_recent:].reset_index(drop=True),
            }
            for split_name in splits_to_run:
                split_data = split_map[split_name]
                if len(split_data) >= 60:
                    jobs.append((fee, asset, split_name, split_data))
    return jobs


def _run_scenario(
    template: str,
    params: dict[str, Any],
    risk: dict[str, Any],
    fn,
    rules: ResearchRules,
    job: tuple[float, str, str, pd.DataFrame],
) -> ScenarioMetric:
    fee, asset, split_name, split_data = job
    spec = StrategySpec(
        name=template, function=fn, params=params,
        take_profit_pct=risk.get("take_profit_pct"),
        stop_loss_pct=risk.get("stop_loss_pct"),
        max_hold_candles=risk.get("max_hold_candles"),
    )
    rules_for_fee = _rules_with_fee(rules, fee)
    result = validate_strategy(asset=asset, stock_data=split_data, strategy=spec, rules=rules_for_fee)
    scenario_label = f"{asset}@fee={fee:.2f}/{split_name}"
    return _result_to_metric(result, scenario_name=scenario_label, fee_pct=fee, split=split_name)


def _run_scenarios(
    template: str,
    params: dict[str, Any],
    risk: dict[str, Any],
    fn,
    data_by_asset: dict[str, pd.DataFrame],
    rules: ResearchRules,
    criteria: ValidationCriteria,
    splits_to_run: tuple[str, ...],
    on_scenario: Callable[[ScenarioMetric], None] | None,
) -> list[ScenarioMetric]:
    jobs = _scenario_jobs(data_by_asset, criteria.fee_levels, criteria, splits_to_run)
    if not jobs:
        return []

    max_workers = max(1, min(criteria.max_scenario_workers, len(jobs)))
    metrics: list[ScenarioMetric] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_run_scenario, template, params, risk, fn, rules, job)
            for job in jobs
        ]
        for future in as_completed(futures):
            metric = future.result()
            metrics.append(metric)
            if on_scenario:
                try:
                    on_scenario(metric)
                except Exception:
                    pass

    return sorted(metrics, key=lambda item: (item.fee_pct, item.asset, item.split))


def full_validation(
    strategy_id: str,
    template: str,
    params: dict[str, Any],
    risk: dict[str, Any],
    fn,
    data_by_asset: dict[str, pd.DataFrame],
    rules: ResearchRules,
    criteria: ValidationCriteria | None = None,
    on_scenario: Callable[[ScenarioMetric], None] | None = None,
) -> ValidationReport:
    criteria = criteria or ValidationCriteria()
    seed = int(hashlib.sha1(strategy_id.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)

    full_metrics = _run_scenarios(
        template=template,
        params=params,
        risk=risk,
        fn=fn,
        data_by_asset=data_by_asset,
        rules=rules,
        criteria=criteria,
        splits_to_run=("full",),
        on_scenario=on_scenario,
    )
    summary_full = _aggregate(full_metrics)
    full_checks = _build_full_checks(summary_full, criteria)
    full_reason = _rejection_reason(full_checks)
    if full_reason:
        return ValidationReport(
            strategy_id=strategy_id,
            template=template,
            params=params,
            risk=risk,
            scenarios=full_metrics,
            criteria_checks=full_checks,
            score=round(
                summary_full["avg_score"] * 0.3
                + summary_full["avg_consistency"] * 0.5
                + (summary_full["scenarios_passed"] / max(summary_full["scenarios_total"], 1)) * 80
                + min(summary_full["avg_profit_factor"], 4.0) * 12
                - summary_full["avg_drawdown"] * 0.5,
                2,
            ),
            summary=summary_full,
            approved=False,
            rejection_reason=full_reason,
        )

    oos_metrics = _run_scenarios(
        template=template,
        params=params,
        risk=risk,
        fn=fn,
        data_by_asset=data_by_asset,
        rules=rules,
        criteria=criteria,
        splits_to_run=("oos_train", "oos_test", "oos_recent"),
        on_scenario=on_scenario,
    )
    scenarios = sorted(full_metrics + oos_metrics, key=lambda item: (item.fee_pct, item.asset, item.split))
    train_metrics = [m for m in scenarios if m.split == "oos_train"]
    test_metrics = [m for m in scenarios if m.split == "oos_test"]
    recent_metrics = [m for m in scenarios if m.split == "oos_recent"]

    summary_train = _aggregate(train_metrics)
    summary_test = _aggregate(test_metrics)
    summary_recent = _aggregate(recent_metrics)
    summary = dict(summary_full)
    summary["train"] = summary_train
    summary["test"] = summary_test
    summary["recent"] = summary_recent
    summary["overfit_pf_drop"] = round(summary_train["avg_profit_factor"] - summary_test["avg_profit_factor"], 3)
    summary["overfit_winrate_drop"] = round(summary_train["avg_winrate"] - summary_test["avg_winrate"], 3)

    # Monte Carlo
    mc_profit_pcts: list[float] = []
    if criteria.fee_levels and data_by_asset:
        try:
            first_asset, first_data = next(iter(data_by_asset.items()))
            spec = StrategySpec(
                name=template, function=fn, params=params,
                take_profit_pct=risk.get("take_profit_pct"),
                stop_loss_pct=risk.get("stop_loss_pct"),
                max_hold_candles=risk.get("max_hold_candles"),
            )
            _, trades = run_strategy_backtest(first_data, spec, _rules_with_fee(rules, criteria.fee_levels[0]))
            mc_profit_pcts = [float(t.profit_pct) for t in trades]
        except Exception:
            mc_profit_pcts = []
    mc_real, mc_p95 = _bootstrap_drawdown_p95(mc_profit_pcts, criteria.monte_carlo_iters, rng)
    mc_ratio = (mc_p95 / mc_real) if mc_real > 0.5 else 1.0
    summary["monte_carlo"] = {
        "trades": len(mc_profit_pcts),
        "real_dd_pct": round(mc_real, 2),
        "p95_dd_pct": round(mc_p95, 2),
        "ratio": round(mc_ratio, 2),
    }

    checks = _build_checks(summary, summary_train, summary_test, summary_recent, mc_ratio, criteria)
    reason = _rejection_reason(checks)
    approved = reason is None

    score = (
        summary["avg_score"] * 0.3
        + summary["avg_consistency"] * 0.5
        + summary_test["avg_consistency"] * 0.5
        + summary_recent["avg_consistency"] * 0.4
        + (summary["scenarios_passed"] / max(summary["scenarios_total"], 1)) * 80
        + min(summary_test["avg_profit_factor"], 4.0) * 12
        + min(summary_recent["avg_profit_factor"], 4.0) * 8
        - summary["avg_drawdown"] * 0.5
        - max(0.0, summary["overfit_pf_drop"]) * 14
        - max(0.0, mc_ratio - 1.0) * 10  # penaliza fragilidade MC
    )

    return ValidationReport(
        strategy_id=strategy_id,
        template=template,
        params=params,
        risk=risk,
        scenarios=scenarios,
        criteria_checks=checks,
        score=round(score, 2),
        summary=summary,
        approved=approved,
        rejection_reason=reason,
    )

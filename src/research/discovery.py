"""
Loop de descoberta de estrategias.

Em cada round:
  1. Gera candidatos novos (que nao estao no catalogo)
  2. Backtest rapido (1 ativo amostra) — corta o que e claramente ruim
  3. Validacao completa multi-cenario para os que passaram no filtro
  4. Persiste no catalogo: aprovados ou rejeitados

Loop continuo enquanto stop_event nao for setado.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from src.modules.StrategyResearchEngine import ResearchRules
from src.research.active import deploy as deploy_active
from src.research.catalog import CatalogEntry, StrategyCatalog, make_strategy_id
from src.research.evolution import (
    PARAM_RANGES,
    crossover,
    mutate,
    random_candidate,
)
from src.research.templates import (
    RISK_PROFILES,
    TEMPLATE_GRIDS,
    TemplateSpec,
    build_strategy_fn,
    iter_template_specs,
)
from src.research.validator import (
    ValidationCriteria,
    ValidationReport,
    full_validation,
    quick_filter,
)


def _now_ts() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def all_candidates() -> list[tuple[TemplateSpec, dict[str, Any]]]:
    """Lista completa: (template_spec, risk_profile) — todas as combinacoes."""
    candidates: list[tuple[TemplateSpec, dict[str, Any]]] = []
    for spec in iter_template_specs():
        for risk in RISK_PROFILES:
            candidates.append((spec, risk))
    return candidates


class DiscoveryRunner:
    def __init__(
        self,
        catalog: StrategyCatalog,
        data_by_asset: dict[str, pd.DataFrame],
        rules: ResearchRules,
        criteria: ValidationCriteria,
        emit: Callable[[dict[str, Any]], None],
        sample_asset: str | None = None,
        candidates_per_round: int = 60,
        max_rounds: int | None = None,
        seed: int | None = None,
        generation_mix: dict[str, float] | None = None,
    ) -> None:
        self.catalog = catalog
        self.data_by_asset = data_by_asset
        self.rules = rules
        self.criteria = criteria
        self.emit = emit
        self.sample_asset = sample_asset or next(iter(data_by_asset))
        self.candidates_per_round = candidates_per_round
        self.max_rounds = max_rounds
        self.stop_event = threading.Event()
        self.rng = random.Random(seed)
        self._sample_data = data_by_asset[self.sample_asset]
        # Mistura padrao: 30% grid, 35% random, 25% mutacao, 10% crossover.
        # Quando ainda nao ha aprovadas, mutacao/crossover colapsam para random.
        self.generation_mix = generation_mix or {
            "grid": 0.30, "random": 0.35, "mutation": 0.25, "crossover": 0.10,
        }
        self._stats = {
            "candidates_generated": 0,
            "filtered_in": 0,
            "filtered_out": 0,
            "validated": 0,
            "approved": 0,
            "rejected": 0,
            "errors": 0,
            "by_origin": {"grid": 0, "random": 0, "mutation": 0, "crossover": 0},
        }

    def stop(self) -> None:
        self.stop_event.set()

    @property
    def stats(self) -> dict[str, Any]:
        return dict(self._stats)

    def _build_generation(self) -> list[tuple[TemplateSpec, dict[str, Any], str, str, list[str]]]:
        """Gera a lista de candidatos do round usando o mix configurado.

        Retorna tuplas (spec, risk, strategy_id, origin, parents).
        """
        approved = self.catalog.approved()
        # quotas por origem (arredondamento simples)
        target = self.candidates_per_round
        quotas = {k: max(0, int(round(v * target))) for k, v in self.generation_mix.items()}
        # se nao ha aprovadas ainda, mutacao/crossover viram random
        if not approved:
            quotas["random"] = quotas.get("random", 0) + quotas.get("mutation", 0) + quotas.get("crossover", 0)
            quotas["mutation"] = 0
            quotas["crossover"] = 0
        # ajusta para somar exatamente target
        total = sum(quotas.values())
        if total != target:
            quotas["random"] = quotas.get("random", 0) + (target - total)

        out: list[tuple[TemplateSpec, dict[str, Any], str, str, list[str]]] = []
        seen_ids: set[str] = set()

        def _try_add(spec: TemplateSpec, risk: dict[str, Any], origin: str, parents: list[str]) -> bool:
            sid = make_strategy_id(spec.template, spec.params, risk)
            if sid in seen_ids or self.catalog.has(sid):
                return False
            seen_ids.add(sid)
            out.append((spec, risk, sid, origin, parents))
            return True

        # 1) GRID (varredura ordenada do espaco fixo)
        if quotas.get("grid"):
            grid_pool = [(spec, risk) for spec in iter_template_specs() for risk in RISK_PROFILES]
            self.rng.shuffle(grid_pool)
            n = quotas["grid"]
            for spec, risk in grid_pool:
                if n <= 0:
                    break
                if _try_add(spec, risk, "grid", []):
                    n -= 1

        # 2) RANDOM SAMPLING contínuo
        attempts = quotas.get("random", 0) * 8
        n = quotas.get("random", 0)
        while n > 0 and attempts > 0:
            attempts -= 1
            try:
                spec, risk, _ = random_candidate(self.rng)
            except Exception:
                continue
            if _try_add(spec, risk, "random", []):
                n -= 1

        # 3) MUTACAO de aprovadas
        n = quotas.get("mutation", 0)
        attempts = n * 8
        if approved and n > 0:
            while n > 0 and attempts > 0:
                attempts -= 1
                parent = self.rng.choice(approved)
                spec, risk = mutate(self.rng, parent.template, parent.params, parent.risk)
                if _try_add(spec, risk, "mutation", [parent.id]):
                    n -= 1

        # 4) CROSSOVER entre aprovadas do mesmo template
        n = quotas.get("crossover", 0)
        if approved and n > 0:
            by_template: dict[str, list[CatalogEntry]] = {}
            for entry in approved:
                by_template.setdefault(entry.template, []).append(entry)
            templates_with_two = [t for t, arr in by_template.items() if len(arr) >= 2]
            attempts = n * 8
            while n > 0 and attempts > 0 and templates_with_two:
                attempts -= 1
                tpl = self.rng.choice(templates_with_two)
                a, b = self.rng.sample(by_template[tpl], 2)
                spec, risk = crossover(self.rng, a.params, a.risk, b.params, b.risk, template=tpl)
                if _try_add(spec, risk, "crossover", [a.id, b.id]):
                    n -= 1
            # se sobrou cota de crossover (so 1 template aprovado), vira mutacao
            if n > 0 and approved:
                while n > 0:
                    parent = self.rng.choice(approved)
                    spec, risk = mutate(self.rng, parent.template, parent.params, parent.risk)
                    if _try_add(spec, risk, "mutation", [parent.id]):
                        n -= 1
                    else:
                        n -= 1  # evita loop infinito caso nao se gere novidade

        return out

    def run(self) -> None:
        self.emit({
            "type": "discovery_started",
            "ts": _now_ts(),
            "ran_at": _now_iso(),
            "templates": list(TEMPLATE_GRIDS.keys()),
            "risk_profiles": RISK_PROFILES,
            "assets": list(self.data_by_asset.keys()),
            "fee_levels": list(self.criteria.fee_levels),
            "candidates_per_round": self.candidates_per_round,
        })

        rounds_done = 0
        while not self.stop_event.is_set():
            if self.max_rounds is not None and rounds_done >= self.max_rounds:
                break

            candidates = self._build_generation()
            if not candidates:
                self.emit({"type": "log", "ts": _now_ts(), "level": "warn",
                           "msg": "Sem novos candidatos validos para gerar — interrompendo loop."})
                break

            round_index = self.catalog.increment_round()
            rounds_done += 1
            origin_breakdown: dict[str, int] = {}
            for _, _, _, origin, _ in candidates:
                origin_breakdown[origin] = origin_breakdown.get(origin, 0) + 1
            self.emit({
                "type": "round_started",
                "ts": _now_ts(),
                "round": round_index,
                "candidates": len(candidates),
                "origin_breakdown": origin_breakdown,
            })
            self.emit({
                "type": "log", "ts": _now_ts(), "level": "info",
                "msg": f"Round {round_index}: {len(candidates)} candidatos · "
                       + " ".join(f"{k}={v}" for k, v in origin_breakdown.items()),
            })

            for spec, risk, strategy_id, origin, parents in candidates:
                if self.stop_event.is_set():
                    break
                self._process_candidate(spec, risk, strategy_id, round_index, origin, parents)

            self.catalog.save()
            self.emit({
                "type": "round_finished",
                "ts": _now_ts(),
                "round": round_index,
                "stats": self.stats,
                "catalog_stats": self.catalog.stats(),
            })

        self.emit({
            "type": "discovery_finished",
            "ts": _now_ts(),
            "stats": self.stats,
            "catalog_stats": self.catalog.stats(),
            "stopped_by_user": self.stop_event.is_set(),
        })

    # ---------- per-candidate -------------

    def _process_candidate(
        self,
        spec: TemplateSpec,
        risk: dict[str, Any],
        strategy_id: str,
        round_index: int,
        origin: str = "grid",
        parents: list[str] | None = None,
    ) -> None:
        parents = parents or []
        self._stats["candidates_generated"] += 1
        self._stats["by_origin"][origin] = self._stats["by_origin"].get(origin, 0) + 1
        self.emit({
            "type": "candidate_generated",
            "ts": _now_ts(),
            "round": round_index,
            "id": strategy_id,
            "template": spec.template,
            "params": spec.params,
            "risk": risk,
            "label": spec.label(),
            "origin": origin,
            "parents": parents,
        })

        try:
            fn = build_strategy_fn(spec)
        except Exception as exc:
            self._stats["errors"] += 1
            entry = CatalogEntry(
                id=strategy_id, template=spec.template, params=spec.params, risk=risk,
                status="rejected", rejection_reason=f"build error: {exc}",
                rejected_at=_now_iso(), origin=origin, parents=parents,
            )
            self.catalog.upsert(entry)
            self.emit({"type": "candidate_rejected", "ts": _now_ts(), "id": strategy_id,
                       "reason": entry.rejection_reason})
            return

        # 1) quick filter
        try:
            ok, quick_metric = quick_filter(
                strategy_id=strategy_id,
                template=spec.template,
                params=spec.params,
                risk=risk,
                fn=fn,
                sample_data=self._sample_data,
                rules=self.rules,
            )
        except Exception as exc:
            self._stats["errors"] += 1
            entry = CatalogEntry(
                id=strategy_id, template=spec.template, params=spec.params, risk=risk,
                status="rejected", rejection_reason=f"quick error: {exc}",
                rejected_at=_now_iso(), origin=origin, parents=parents,
            )
            self.catalog.upsert(entry)
            self.emit({"type": "candidate_rejected", "ts": _now_ts(), "id": strategy_id,
                       "reason": entry.rejection_reason})
            return

        self.emit({
            "type": "candidate_quick",
            "ts": _now_ts(),
            "id": strategy_id,
            "template": spec.template,
            "passed_quick": ok,
            "metric": quick_metric.to_dict(),
        })

        if not ok:
            self._stats["filtered_out"] += 1
            entry = CatalogEntry(
                id=strategy_id, template=spec.template, params=spec.params, risk=risk,
                status="rejected",
                rejection_reason=f"filtro rapido: trades={quick_metric.total_trades} win={quick_metric.winrate_pct:.1f}% pf={quick_metric.profit_factor:.2f}",
                summary={"quick": quick_metric.to_dict()},
                rejected_at=_now_iso(), origin=origin, parents=parents,
            )
            self.catalog.upsert(entry)
            self.emit({"type": "candidate_rejected", "ts": _now_ts(), "id": strategy_id,
                       "reason": entry.rejection_reason})
            return

        self._stats["filtered_in"] += 1

        # 2) full multi-scenario validation
        self.emit({
            "type": "validation_started",
            "ts": _now_ts(),
            "id": strategy_id,
            "template": spec.template,
            "scenarios_planned": len(self.criteria.fee_levels) * len(self.data_by_asset),
        })

        def _on_scenario(metric):
            self.emit({
                "type": "scenario_done",
                "ts": _now_ts(),
                "id": strategy_id,
                "metric": metric.to_dict(),
            })

        try:
            report: ValidationReport = full_validation(
                strategy_id=strategy_id,
                template=spec.template,
                params=spec.params,
                risk=risk,
                fn=fn,
                data_by_asset=self.data_by_asset,
                rules=self.rules,
                criteria=self.criteria,
                on_scenario=_on_scenario,
            )
        except Exception as exc:
            self._stats["errors"] += 1
            entry = CatalogEntry(
                id=strategy_id, template=spec.template, params=spec.params, risk=risk,
                status="rejected", rejection_reason=f"validation error: {exc}",
                rejected_at=_now_iso(), origin=origin, parents=parents,
            )
            self.catalog.upsert(entry)
            self.emit({"type": "candidate_rejected", "ts": _now_ts(), "id": strategy_id,
                       "reason": entry.rejection_reason})
            return

        self._stats["validated"] += 1
        scenarios_dict = {m.name: m.to_dict() for m in report.scenarios}
        if report.approved:
            self._stats["approved"] += 1
            entry = CatalogEntry(
                id=strategy_id, template=spec.template, params=spec.params, risk=risk,
                status="approved",
                score=report.score,
                summary=report.summary,
                scenarios=scenarios_dict,
                approved_at=_now_iso(),
                origin=origin, parents=parents,
            )
        else:
            self._stats["rejected"] += 1
            entry = CatalogEntry(
                id=strategy_id, template=spec.template, params=spec.params, risk=risk,
                status="rejected",
                score=report.score,
                summary=report.summary,
                scenarios=scenarios_dict,
                rejection_reason=report.rejection_reason,
                rejected_at=_now_iso(),
                origin=origin, parents=parents,
            )
        self.catalog.upsert(entry)
        self.emit({
            "type": "validation_done",
            "ts": _now_ts(),
            "id": strategy_id,
            "approved": report.approved,
            "score": report.score,
            "summary": report.summary,
            "criteria_checks": [c.to_dict() for c in report.criteria_checks],
            "rejection_reason": report.rejection_reason,
            "entry": entry.to_dict(),
        })

        # Auto-deploy quando aprovada — bot consome de active_strategies.json
        if report.approved:
            try:
                record = deploy_active(entry)
                self.emit({
                    "type": "strategy_deployed",
                    "ts": _now_ts(),
                    "id": strategy_id,
                    "record": record,
                })
                self.emit({"type": "log", "ts": _now_ts(), "level": "ok",
                           "msg": f"DEPLOY {entry.template}#{strategy_id[:8]} score={entry.score:.1f} -> active_strategies.json"})
            except Exception as exc:
                self.emit({"type": "log", "ts": _now_ts(), "level": "err",
                           "msg": f"Falha no deploy de {strategy_id}: {exc}"})

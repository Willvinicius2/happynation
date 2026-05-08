from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .campaign_constants import LEGACY_CAMPAIGN_ID
from .db import add_event
from .repository import add_artifact, upsert_node
from .scoring import clamp, score_from_metrics


def _safe_json(path: Path) -> dict[str, Any] | list[Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _first_present(metrics: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in metrics and metrics[key] is not None:
            return metrics[key]
    return None


def _score_manifest(payload: dict[str, Any]) -> float:
    metrics = payload.get("aggregated_metrics") or {}
    if not metrics:
        combos = payload.get("approved_combos") or []
        if combos:
            best = max(combos, key=lambda c: float(c.get("oos_sharpe") or 0.0))
            metrics = {
                "ok": True,
                "sharpe": best.get("oos_sharpe"),
                "trade_count": best.get("oos_trades"),
                "mdd_pct": best.get("oos_mdd_pct"),
                "pnl_pct": best.get("oos_pnl_pct"),
            }
    if metrics:
        sharpe = _first_present(metrics, "sharpe", "oos_sharpe")
        trade_count = _first_present(metrics, "trade_count", "trades", "oos_trades")
        mdd_pct = _first_present(metrics, "mdd_pct", "oos_mdd_pct")
        pnl_pct = _first_present(metrics, "pnl_pct", "oos_pnl_pct")
        probe_like = {
            "ok": True,
            "sharpe": sharpe if sharpe is not None else 0.0,
            "trade_count": trade_count if trade_count is not None else 60,
            "mdd_pct": mdd_pct if mdd_pct is not None else 10.0,
            "pnl_pct": pnl_pct if pnl_pct is not None else 1.0,
        }
        return score_from_metrics(probe_like)[0]
    return 0.35


def _status_for_manifest(payload: dict[str, Any], bucket: str) -> str:
    live_status = str(payload.get("live_status") or "").lower()
    if live_status == "deprecated":
        return "pruned"
    if bucket == "approved":
        return "validated"
    if bucket == "staging":
        return "promising"
    return "visited"


def import_legacy(conn, legacy_root: Path) -> dict[str, int]:
    counts = {"nodes": 0, "artifacts": 0, "skipped": 0}
    if not legacy_root.exists():
        add_event(
            conn,
            type="legacy_import_error",
            message=f"Legacy root nao encontrado: {legacy_root}",
            agent_role="infra_data_agent",
        )
        return counts

    upsert_node(
        conn,
        id="legacy-root",
        campaign_id=LEGACY_CAMPAIGN_ID,
        parent_id="root" if conn.execute("SELECT 1 FROM nodes WHERE id='root'").fetchone() else None,
        code="LEGACY",
        name="Alpha Forge Legacy Archive",
        hypothesis="Acervo historico importado do projeto antigo.",
        causal_mechanism="Manifests, ADRs e diagnosticos preservam memoria da busca.",
        status="visited",
        score=0.5,
        priority=0.2,
        depth=1,
        tags=["legacy"],
        source_type="legacy_root",
        source_path=str(legacy_root),
    )
    counts["nodes"] += 1

    for bucket in ("approved", "staging"):
        folder = legacy_root / "exports" / bucket
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            payload = _safe_json(path)
            if not isinstance(payload, dict):
                counts["skipped"] += 1
                continue
            name = payload.get("portfolio_name") or payload.get("strategy_name") or path.stem
            nid = f"legacy:{bucket}:{path.stem}"
            score = _score_manifest(payload)
            status = _status_for_manifest(payload, bucket)
            metrics = payload.get("aggregated_metrics") or payload.get("validation") or {}
            prune_reason = payload.get("supersede_reason") if status == "pruned" else None
            upsert_node(
                conn,
                id=nid,
                campaign_id=LEGACY_CAMPAIGN_ID,
                parent_id="legacy-root",
                code=f"{bucket.upper()}-{counts['nodes']}",
                name=str(name),
                hypothesis=f"Manifest legado {bucket}: {name}",
                causal_mechanism="Evidencia historica importada de Alpha Forge.",
                mutation_from_parent=bucket,
                status=status,
                score=score,
                priority=score,
                depth=2,
                prune_reason=prune_reason,
                result_summary=str(payload.get("sub_allocation_note") or payload.get("supersede_rationale") or "")[:500],
                metrics=metrics if isinstance(metrics, dict) else {},
                tags=["legacy", bucket],
                source_type=bucket,
                source_path=str(path),
            )
            add_artifact(conn, node_id=nid, kind="manifest", path=str(path), title=path.name, payload={})
            counts["nodes"] += 1
            counts["artifacts"] += 1

    diag_root_id = "legacy-diag-root"
    upsert_node(
        conn,
        id=diag_root_id,
        campaign_id=LEGACY_CAMPAIGN_ID,
        parent_id="legacy-root",
        code="DIAG",
        name="Diagnostic Summaries",
        hypothesis="Resultados diagnosticos legados para consulta e anti-repeticao.",
        causal_mechanism="Cada JSON diag fica preservado como evidencia historica.",
        status="visited",
        score=0.32,
        priority=0.1,
        depth=2,
        tags=["legacy", "diag"],
    )
    counts["nodes"] += 1

    diag_dir = legacy_root / "exports" / "diag"
    if diag_dir.exists():
        for idx, path in enumerate(sorted(diag_dir.glob("*.json"))):
            payload = _safe_json(path)
            summary = payload if isinstance(payload, dict) else {}
            score = 0.28
            for key in ("concat", "aggregated_metrics"):
                if isinstance(summary.get(key), dict) and "sh" in summary[key]:
                    score = clamp(float(summary[key]["sh"]) / 4.0)
            nid = f"legacy:diag:{path.stem}"
            upsert_node(
                conn,
                id=nid,
                campaign_id=LEGACY_CAMPAIGN_ID,
                parent_id=diag_root_id,
                code=f"D{idx + 1}",
                name=path.stem,
                hypothesis=f"Diag legado: {path.stem}",
                causal_mechanism="Resultado de sweep/validacao legado.",
                status="visited",
                score=score,
                priority=0.05 + score * 0.1,
                depth=3,
                metrics=summary if isinstance(summary, dict) else {},
                tags=["legacy", "diag"],
                source_type="diag",
                source_path=str(path),
            )
            add_artifact(conn, node_id=nid, kind="diag", path=str(path), title=path.name, payload={})
            counts["nodes"] += 1
            counts["artifacts"] += 1

    for doc_name in ("HYPOTHESIS_TREE.md", "NODE_LOG.md", "SEARCH_STATE.md", "GRAVEYARD.md"):
        path = legacy_root / doc_name
        if path.exists():
            add_artifact(conn, node_id="legacy-root", kind="legacy_doc", path=str(path), title=doc_name, payload={})
            counts["artifacts"] += 1

    decisions = legacy_root / "decisions"
    if decisions.exists():
        for path in sorted(decisions.glob("*.md"))[-80:]:
            add_artifact(conn, node_id="legacy-root", kind="adr", path=str(path), title=path.name, payload={})
            counts["artifacts"] += 1

    add_event(
        conn,
        type="legacy_import",
        campaign_id=LEGACY_CAMPAIGN_ID,
        node_id="legacy-root",
        agent_role="archivist_agent",
        message=f"Import legado concluido: {counts['nodes']} nos, {counts['artifacts']} artefatos.",
        payload=counts,
    )
    return counts

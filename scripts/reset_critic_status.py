"""Reset critic_status no DB do Aether para forcar reciritism.

Usado depois de recalibrar thresholds do Critic. Nodes que ja foram
criticados (approved ou paused) sao revertidos para 'validated' sem
critic_status, fazendo com que o critic_agent re-avalie eles do zero.

Saida:
  src/logs/agents/critic_reset_report.json

Uso:
  python -u scripts/reset_critic_status.py             # dry-run
  python -u scripts/reset_critic_status.py --apply     # aplica mudancas
  python -u scripts/reset_critic_status.py --apply --keep-paused-non-critic  # nao mexe em paused por outro motivo
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.aether.config import db_path  # noqa: E402

DB = db_path()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="aplica as mudancas (sem isso, dry-run)")
    ap.add_argument(
        "--keep-paused-non-critic",
        action="store_true",
        help="nao reverte status='paused' se prune_reason nao comecar com 'critic:'",
    )
    args = ap.parse_args()

    print(f"[reset] DB: {DB}", flush=True)
    if not DB.exists():
        print(f"[reset] DB nao existe", flush=True)
        return 1

    conn = sqlite3.connect(str(DB), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    # Coletar todos os nodes que tem critic_status setado no metrics_json
    rows = list(conn.execute(
        """
        SELECT id, name, status, prune_reason, metrics_json
        FROM nodes
        WHERE id != 'root' AND id NOT LIKE 'tf:%'
          AND (status='paused' OR metrics_json LIKE '%critic_status%')
        """
    ).fetchall())

    print(f"[reset] candidatos encontrados: {len(rows)}", flush=True)

    to_reset_approved = []  # status já validated mas com critic_status=approved
    to_reset_paused_critic = []  # status=paused E prune_reason começa com 'critic:'
    to_keep_paused_other = []  # status=paused mas pause não foi do Critic

    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except Exception:
            metrics = {}
        crit_status = metrics.get("critic_status")
        prune_reason = (row["prune_reason"] or "").strip().lower()

        if row["status"] == "paused":
            if prune_reason.startswith("critic:"):
                to_reset_paused_critic.append(row)
            elif args.keep_paused_non_critic:
                to_keep_paused_other.append(row)
            else:
                to_reset_paused_critic.append(row)
        elif crit_status in ("approved", "rejected"):
            to_reset_approved.append(row)

    print(f"  approved (manter status=validated, limpar critic_status): {len(to_reset_approved)}", flush=True)
    print(f"  paused-by-critic (-> validated, limpar critic_status): {len(to_reset_paused_critic)}", flush=True)
    if to_keep_paused_other:
        print(f"  paused-other (mantido inalterado): {len(to_keep_paused_other)}", flush=True)

    total_changes = len(to_reset_approved) + len(to_reset_paused_critic)
    if total_changes == 0:
        print("[reset] nada a fazer.", flush=True)
        conn.close()
        return 0

    if not args.apply:
        print("\n[reset] DRY-RUN. Use --apply para aplicar.", flush=True)
        conn.close()
        return 0

    # === APPLY ===
    now = _now()
    n_approved = 0
    for row in to_reset_approved:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except Exception:
            metrics = {}
        # remove critic_*
        for k in list(metrics.keys()):
            if k.startswith("critic_"):
                metrics.pop(k, None)
        conn.execute(
            "UPDATE nodes SET metrics_json=?, updated_at=? WHERE id=?",
            (json.dumps(metrics, ensure_ascii=False), now, row["id"]),
        )
        n_approved += 1

    n_paused = 0
    for row in to_reset_paused_critic:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except Exception:
            metrics = {}
        for k in list(metrics.keys()):
            if k.startswith("critic_"):
                metrics.pop(k, None)
        conn.execute(
            """
            UPDATE nodes
            SET status='validated',
                metrics_json=?,
                prune_reason=NULL,
                updated_at=?
            WHERE id=?
            """,
            (json.dumps(metrics, ensure_ascii=False), now, row["id"]),
        )
        n_paused += 1

    print(f"\n[reset] APPLIED: {n_approved} approved + {n_paused} paused -> validated", flush=True)

    # Verifica counts pos-reset
    print("\n[reset] status counts apos reset:", flush=True)
    counts = list(conn.execute("SELECT status, COUNT(*) FROM nodes GROUP BY status").fetchall())
    for s, n in counts:
        print(f"  {s}: {n}", flush=True)
    conn.close()

    # Relatorio
    report = {
        "generated_at": now,
        "applied": True,
        "approved_reset_count": n_approved,
        "paused_critic_reset_count": n_paused,
        "kept_paused_other_count": len(to_keep_paused_other),
        "approved_ids": [r["id"] for r in to_reset_approved],
        "paused_critic_ids": [r["id"] for r in to_reset_paused_critic],
    }
    out = ROOT / "src/logs/agents/critic_reset_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[reset] relatorio: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

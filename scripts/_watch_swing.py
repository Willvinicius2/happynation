"""Watcher dedicado pra deteccao das engines swing recem-adicionadas.

Emite UMA linha por evento de interesse. Fica vivo enquanto Monitor estiver
escutando. Polling 30s.

Eventos detectados:
  - llm_research_started  -> "LLM_CALL_INICIOU"
  - llm_research_completed -> "LLM_CALL_OK created=N rej_exist=M dominante=X%"
  - llm_loop_detected      -> "LLM_LOOP_DETECTADO (session reset)"
  - hipotese nova com engine swing (golden_cross/swing_pivot/weekly_divergence)
  - validated com engine swing
  - critic_approved com engine swing
  - critic_rejected com engine swing
  - erro Exception/Traceback nos logs
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DB = Path(os.path.expandvars(r"%LOCALAPPDATA%\Aether\runtime\aether.sqlite3"))
ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"

SWING_ENGINES = {"golden_cross", "swing_pivot", "weekly_divergence"}
# variantes que aparecem em node_id/message (hifenizado, sem underscore)
SWING_TOKENS = {
    "golden_cross", "swing_pivot", "weekly_divergence",
    "golden-cross", "swing-pivot", "weekly-divergence",
    "swing-grade", "swing pivot", "weekly divergence", "golden cross",
}


def emit(tag: str, msg: str) -> None:
    hh = datetime.now().strftime("%H:%M:%S")
    print(f"[{hh}] {tag}: {msg}", flush=True)


def parse_payload(s: str | None) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s) if isinstance(s, str) else {}
    except Exception:
        return {}


def has_swing_engine(payload: dict) -> str | None:
    """Verifica se payload menciona alguma engine swing nova. Retorna o token ou None."""
    blob = json.dumps(payload).lower() if payload else ""
    for tok in SWING_TOKENS:
        if tok in blob:
            return tok
    return None


def main() -> int:
    last_event_id = 0
    # bootstrap: ler max(id) atual pra ignorar historico
    try:
        with sqlite3.connect(str(DB), timeout=2.0) as con:
            row = con.execute("SELECT MAX(id) FROM events").fetchone()
            last_event_id = int(row[0] or 0)
    except Exception as e:
        emit("WARN", f"bootstrap falhou: {e}")
    emit("READY", f"watcher swing armado last_id={last_event_id} engines={sorted(SWING_ENGINES)}")

    while True:
        try:
            with sqlite3.connect(str(DB), timeout=2.0) as con:
                rows = con.execute(
                    "SELECT id, ts, type, node_id, message, payload_json "
                    "FROM events WHERE id > ? ORDER BY id ASC LIMIT 500",
                    (last_event_id,),
                ).fetchall()
                for r in rows:
                    eid, ts, typ, node_id, message, payload_json = r
                    last_event_id = max(last_event_id, int(eid))
                    payload = parse_payload(payload_json)
                    short_ts = (ts or "")[-9:-1] if ts else "??"
                    # LLM lifecycle — so emite quando ha producao real ou loop
                    if typ == "llm_research_completed":
                        try:
                            created = int(payload.get("created", 0) or 0)
                        except (TypeError, ValueError):
                            created = 0
                        if created > 0:
                            rej_ex = payload.get("rejected_existing", "?")
                            dom = payload.get("dominance_pct", "?")
                            try:
                                dom_s = f"{float(dom):.1f}%"
                            except (TypeError, ValueError):
                                dom_s = str(dom)
                            emit("LLM_OK", f"{short_ts} created={created} rej_existing={rej_ex} dom={dom_s}")
                    elif typ == "llm_loop_detected":
                        emit("LLM_LOOP", f"{short_ts} loop detectado — session resetada")
                    # Swing-specific
                    eng = has_swing_engine({"msg": message or "", **payload})
                    if eng and typ in ("hypothesis_created", "validated", "critic_approved", "critic_rejected", "node_expanded"):
                        nm = payload.get("name") or message or node_id
                        emit(f"SWING_{typ.upper()}", f"{short_ts} engine={eng} node={node_id} | {str(nm)[:80]}")
                    elif typ == "critic_approved" and "1d" in (node_id or "") and "swing" in (message or "").lower():
                        emit("CRITIC_OK_SWING_NAMED", f"{short_ts} {node_id}")
                    elif typ == "critic_rejected" and eng:
                        emit("SWING_REJECTED", f"{short_ts} engine={eng} | {(message or '')[:100]}")
        except Exception as e:
            emit("WARN", f"poll error: {e}")
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())

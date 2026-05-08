"""
Re-embed dos nodes do Aether.

Quando o encoder semantico (sentence-transformers) fica disponivel pela
primeira vez (ou troca de modelo), os vetores antigos no fallback hash
(256-d) ficam incompativeis com os novos (384-d/1024-d). Esta funcao
percorre `nodes` e regrava os vetores no encoder atual.

Uso:
    python scripts/aether_reembed_memory.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from research.aether.config import db_path  # type: ignore
from research.aether import semantic_memory as sm  # type: ignore


def main() -> int:
    path = db_path()
    print(f"[reembed] DB: {path}")
    if not Path(path).exists():
        print("[reembed] DB nao existe; nada a fazer.")
        return 0
    conn = sqlite3.connect(str(path), timeout=120.0)
    conn.row_factory = sqlite3.Row
    try:
        # WAL minimiza contencao com agents do stack escrevendo em paralelo.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=120000")
        except Exception:
            pass
        encoder, dim = sm._try_load_encoder()
        if encoder is None:
            print("[reembed] Encoder ST nao disponivel (torch/sentence-transformers ausentes).")
            print("[reembed] Mantendo embeddings hash atuais; saindo sem alteracao.")
            return 0
        print(f"[reembed] Encoder ativo: dim={dim}")
        # batch menor reduz tempo de cada commit -> menos chance de lock prolongado
        stats = sm.rebuild_all(conn, batch_size=16)
        conn.commit()
        print(
            "[reembed] scanned={scanned} updated={updated} kept={kept} errors={errors}".format(**stats)
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

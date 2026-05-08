"""
Memoria semantica do Aether: 'aprendizado' entre ciclos.

Cada hipotese testada (validada/podada/em teste) vira um vetor. Quando o
hypothesis_generator vai criar nova hipotese, o archivist consulta esta
memoria por SIMILARIDADE para injetar no prompt:
  - 'voce ja testou X parecido e foi rejeitado por Y'
  - 'estrategia Z parecida ja foi validada com score W'

Backends:
  GPU + sentence-transformers (torch+st-transformers instalados):
    embeddings semanticos densos do texto (hipotese + mecanismo).
    1024-d, FP16 na 4060 Ti, 5-15 ms por consulta.
  Fallback CPU/numpy:
    embedding determinista por hash-de-features (engine, params discretizados,
    palavras-chave do mecanismo). Util mesmo sem GPU; estrutural.

Storage: tabela SQLite `node_embeddings (node_id, kind, vector BLOB, dim, ts)`.
Vector salvo como float16 little-endian. Pequeno (1k nos x 1024-d ~ 2MB).

API:
    ensure_table(conn)
    embed_node(node) -> np.ndarray (1D float32)
    record_node(conn, node)
    find_similar(conn, node_or_text, k=5, exclude_id=None) -> list[dict]
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
from typing import Any, Iterable

import numpy as np


_DEFAULT_DIM = 256
_DISABLED = os.environ.get("AETHER_SEMANTIC_DISABLE", "0").strip().lower() in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# Lazy GPU/text encoder (sentence-transformers + torch).
# ---------------------------------------------------------------------------

_ENCODER = None
_ENCODER_DIM = None
_ENCODER_TRIED = False


def _try_load_encoder():
    """Carrega sentence-transformers em GPU se possivel; caso contrario, CPU.

    Retorna (encoder, dim) ou (None, None) se nao disponivel.
    Modelo: 'sentence-transformers/all-MiniLM-L6-v2' (384-d, ~22M params).
    """
    global _ENCODER, _ENCODER_DIM, _ENCODER_TRIED
    if _DISABLED:
        return None, None
    if _ENCODER_TRIED:
        return _ENCODER, _ENCODER_DIM
    _ENCODER_TRIED = True
    try:
        import torch  # type: ignore
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception:
        return None, None
    try:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model_name = os.environ.get(
            "AETHER_SEMANTIC_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
        )
        model = SentenceTransformer(model_name, device=device)
        _ENCODER = model
        _ENCODER_DIM = int(model.get_sentence_embedding_dimension())
        return _ENCODER, _ENCODER_DIM
    except Exception:
        _ENCODER = None
        _ENCODER_DIM = None
        return None, None


# ---------------------------------------------------------------------------
# Fallback determinista: hashed feature bag.
# Mesma string sempre gera mesmo vetor; util pra similaridade estrutural.
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    out = []
    cur = []
    for ch in text.lower():
        if ch.isalnum() or ch == "-":
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return [t for t in out if len(t) > 1]


def _bucket_value(name: str, value: Any) -> str:
    try:
        v = float(value)
    except Exception:
        return f"{name}:{value}"
    # discretizar valores numericos em faixas: ajuda similaridade estrutural
    if abs(v) >= 1000:
        bucket = round(v / 100) * 100
    elif abs(v) >= 100:
        bucket = round(v / 10) * 10
    elif abs(v) >= 10:
        bucket = round(v)
    else:
        bucket = round(v * 4) / 4.0  # 0.25 step
    return f"{name}:{bucket}"


def _node_features(node: dict[str, Any]) -> list[str]:
    feats: list[str] = []
    engine = ""
    metrics = node.get("metrics") or {}
    if isinstance(metrics, str):
        try:
            metrics = json.loads(metrics)
        except Exception:
            metrics = {}
    best = (metrics or {}).get("best_probe") or {}
    if isinstance(best, dict):
        engine = str(best.get("engine") or "")
    if not engine:
        specs = (metrics or {}).get("probe_specs") or []
        if isinstance(specs, list) and specs and isinstance(specs[0], dict):
            engine = str(specs[0].get("engine") or "")
    if engine:
        feats.append(f"engine:{engine.lower()}")
    tf = node.get("timeframe") or ""
    if tf:
        feats.append(f"tf:{tf}")
    status = node.get("status") or ""
    if status:
        feats.append(f"status:{status}")
    # Params do best_probe
    params = (best or {}).get("params") if isinstance(best, dict) else None
    if not isinstance(params, dict):
        specs = (metrics or {}).get("probe_specs") or []
        if isinstance(specs, list) and specs and isinstance(specs[0], dict):
            params = specs[0].get("params") or {}
    if isinstance(params, dict):
        for k, v in params.items():
            feats.append(_bucket_value(str(k), v))
    # Texto livre da hipotese e mecanismo
    text = " ".join(
        str(node.get(field) or "")
        for field in ("name", "hypothesis", "causal_mechanism", "prune_reason")
    )
    feats.extend(_tokenize(text)[:60])
    return feats


def _hash_token(tok: str, dim: int) -> int:
    h = 0
    for ch in tok:
        h = (h * 1099511628211 + ord(ch)) & 0xFFFFFFFFFFFFFFFF
    return h % dim


def _hashed_embedding(features: Iterable[str], dim: int = _DEFAULT_DIM) -> np.ndarray:
    vec = np.zeros(dim, dtype=np.float32)
    for f in features:
        idx = _hash_token(f, dim)
        sign = 1.0 if (_hash_token("s:" + f, 16) & 1) == 0 else -1.0
        vec[idx] += sign
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    return vec


# ---------------------------------------------------------------------------
# Embedding publico: usa encoder GPU se disponivel, senao hashed fallback.
# Sempre soma o vetor estrutural (features) com peso menor para reforcar
# similaridade por engine/params.
# ---------------------------------------------------------------------------

def embed_text(text: str) -> np.ndarray:
    encoder, dim = _try_load_encoder()
    if encoder is not None and dim:
        v = encoder.encode([text], normalize_embeddings=True, show_progress_bar=False)
        v = np.asarray(v[0], dtype=np.float32)
        return v
    return _hashed_embedding(_tokenize(text), _DEFAULT_DIM)


def embed_node(node: dict[str, Any]) -> tuple[np.ndarray, str]:
    """Devolve (vetor_normalizado, kind). kind in {'st','hash'}."""
    encoder, dim = _try_load_encoder()
    text = " | ".join(
        str(node.get(field) or "")
        for field in ("name", "hypothesis", "causal_mechanism", "invalidation")
    ).strip()
    if encoder is not None and dim:
        v = encoder.encode([text or node.get("id", "")], normalize_embeddings=True, show_progress_bar=False)
        v = np.asarray(v[0], dtype=np.float32)
        # mescla com features estruturais (re-projetadas no mesmo dim)
        feats = _node_features(node)
        struct_v = _hashed_embedding(feats, dim)
        v = v + 0.35 * struct_v
        n = float(np.linalg.norm(v))
        if n > 0:
            v = v / n
        return v.astype(np.float32), "st"
    feats = _node_features(node)
    return _hashed_embedding(feats, _DEFAULT_DIM).astype(np.float32), "hash"


# ---------------------------------------------------------------------------
# Storage SQLite.
# ---------------------------------------------------------------------------

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS node_embeddings (
    node_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_node_embeddings_kind ON node_embeddings(kind);
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_TABLE_SQL)


def _vec_to_blob(v: np.ndarray) -> bytes:
    return v.astype(np.float32).tobytes()


def _blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def record_node(conn: sqlite3.Connection, node: dict[str, Any]) -> None:
    if _DISABLED:
        return
    try:
        ensure_table(conn)
        v, kind = embed_node(node)
        from .timeutils import now_iso

        conn.execute(
            """
            INSERT INTO node_embeddings(node_id, kind, dim, vector, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                kind=excluded.kind,
                dim=excluded.dim,
                vector=excluded.vector,
                updated_at=excluded.updated_at
            """,
            (str(node.get("id") or ""), kind, int(v.shape[0]), _vec_to_blob(v), now_iso()),
        )
    except Exception:
        # nunca deixa o pipeline cair por causa da memoria semantica
        pass


def _load_all(conn: sqlite3.Connection, kind: str | None = None) -> list[tuple[str, np.ndarray]]:
    ensure_table(conn)
    if kind:
        rows = conn.execute(
            "SELECT node_id, vector FROM node_embeddings WHERE kind=?", (kind,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT node_id, vector FROM node_embeddings").fetchall()
    out: list[tuple[str, np.ndarray]] = []
    for row in rows:
        try:
            out.append((str(row[0] if not hasattr(row, "keys") else row["node_id"]),
                        _blob_to_vec(row[1] if not hasattr(row, "keys") else row["vector"])))
        except Exception:
            continue
    return out


def find_similar(
    conn: sqlite3.Connection,
    *,
    text: str | None = None,
    node: dict[str, Any] | None = None,
    k: int = 5,
    exclude_id: str | None = None,
    only_kind: str | None = None,
) -> list[dict[str, Any]]:
    """Busca os k nos mais parecidos.

    Devolve lista de dicts com:
      { 'node_id': str, 'similarity': float, 'name', 'status', 'engine',
        'prune_reason', 'score', ... } (campos extras vem da tabela `nodes`)
    """
    if _DISABLED:
        return []
    try:
        ensure_table(conn)
        if node is not None:
            query_v, _kind = embed_node(node)
        elif text is not None:
            query_v = embed_text(text)
        else:
            return []
        if query_v.ndim != 1:
            return []
        all_vecs = _load_all(conn, kind=only_kind)
        if not all_vecs:
            return []
        # filtra dimensao compativel (caso tenha mistura de hash 256 + st 384)
        compat = [(nid, v) for nid, v in all_vecs if v.shape[0] == query_v.shape[0]]
        if not compat:
            return []
        ids = [nid for nid, _ in compat]
        mat = np.stack([v for _, v in compat], axis=0)
        # normaliza
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        mat = mat / np.where(norms > 0, norms, 1.0)
        qn = float(np.linalg.norm(query_v))
        if qn > 0:
            query_v = query_v / qn
        sims = mat @ query_v
        order = np.argsort(-sims)[: max(k * 3, k)]
        out: list[dict[str, Any]] = []
        for idx in order:
            nid = ids[idx]
            if exclude_id and nid == exclude_id:
                continue
            row = conn.execute(
                """SELECT id, name, status, score, prune_reason, timeframe, metrics_json
                   FROM nodes WHERE id=?""",
                (nid,),
            ).fetchone()
            if row is None:
                continue
            try:
                metrics = json.loads(row["metrics_json"] or "{}") if hasattr(row, "keys") else json.loads(row[6] or "{}")
            except Exception:
                metrics = {}
            best = (metrics or {}).get("best_probe") or {}
            engine = ""
            if isinstance(best, dict):
                engine = str(best.get("engine") or "")
            out.append({
                "node_id": nid,
                "similarity": float(sims[idx]),
                "name": (row["name"] if hasattr(row, "keys") else row[1]) or "",
                "status": (row["status"] if hasattr(row, "keys") else row[2]) or "",
                "score": float((row["score"] if hasattr(row, "keys") else row[3]) or 0.0),
                "prune_reason": (row["prune_reason"] if hasattr(row, "keys") else row[4]) or "",
                "timeframe": (row["timeframe"] if hasattr(row, "keys") else row[5]) or "",
                "engine": engine,
            })
            if len(out) >= k:
                break
        return out
    except Exception:
        return []


def rebuild_all(
    conn: sqlite3.Connection,
    *,
    only_kind: str | None = None,
    batch_size: int = 64,
) -> dict[str, int]:
    """Recalcula embeddings de todos os nodes existentes.

    Use depois de instalar/atualizar o encoder ST (ou se mudou o modelo) -- o
    fallback hash gera vetores 256-d que nao sao comparaveis aos 384-d/1024-d
    de um encoder denso. Esta funcao percorre `nodes` e regrava os vetores.
    Retorna {scanned, updated, kept, errors}.
    """
    if _DISABLED:
        return {"scanned": 0, "updated": 0, "kept": 0, "errors": 0}
    ensure_table(conn)
    encoder, _ = _try_load_encoder()
    target_kind = "st" if encoder is not None else "hash"
    if only_kind and only_kind != target_kind:
        return {"scanned": 0, "updated": 0, "kept": 0, "errors": 0}
    rows = conn.execute(
        """SELECT id, name, status, score, prune_reason, timeframe, metrics_json
           FROM nodes"""
    ).fetchall()
    scanned = 0
    updated = 0
    kept = 0
    errors = 0
    pending: list[dict[str, Any]] = []
    for row in rows:
        scanned += 1
        try:
            metrics_raw = row["metrics_json"] if hasattr(row, "keys") else row[6]
            try:
                metrics = json.loads(metrics_raw or "{}")
            except Exception:
                metrics = {}
            node_dict = {
                "id": row["id"] if hasattr(row, "keys") else row[0],
                "name": (row["name"] if hasattr(row, "keys") else row[1]) or "",
                "status": (row["status"] if hasattr(row, "keys") else row[2]) or "",
                "prune_reason": (row["prune_reason"] if hasattr(row, "keys") else row[4]) or "",
                "timeframe": (row["timeframe"] if hasattr(row, "keys") else row[5]) or "",
                "hypothesis": (metrics or {}).get("hypothesis") or "",
                "causal_mechanism": (metrics or {}).get("causal_mechanism") or "",
                "invalidation": (metrics or {}).get("invalidation") or "",
                "metrics": metrics,
            }
            existing = conn.execute(
                "SELECT kind, dim FROM node_embeddings WHERE node_id=?",
                (node_dict["id"],),
            ).fetchone()
            v, kind = embed_node(node_dict)
            if existing is not None:
                ex_kind = existing[0] if not hasattr(existing, "keys") else existing["kind"]
                ex_dim = int(existing[1] if not hasattr(existing, "keys") else existing["dim"])
                if ex_kind == kind and ex_dim == int(v.shape[0]):
                    kept += 1
                    continue
            pending.append({"id": node_dict["id"], "kind": kind, "vec": v})
            if len(pending) >= batch_size:
                _flush_rebuild(conn, pending)
                updated += len(pending)
                pending.clear()
        except Exception:
            errors += 1
            continue
    if pending:
        _flush_rebuild(conn, pending)
        updated += len(pending)
        pending.clear()
    return {"scanned": scanned, "updated": updated, "kept": kept, "errors": errors}


def _flush_rebuild(conn: sqlite3.Connection, pending: list[dict[str, Any]]) -> None:
    """Flush em batch com retry simples se DB estiver locked por agents."""
    from .timeutils import now_iso
    import time as _time

    ts = now_iso()
    last_exc: Exception | None = None
    for attempt in range(6):
        try:
            for item in pending:
                conn.execute(
                    """
                    INSERT INTO node_embeddings(node_id, kind, dim, vector, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(node_id) DO UPDATE SET
                        kind=excluded.kind,
                        dim=excluded.dim,
                        vector=excluded.vector,
                        updated_at=excluded.updated_at
                    """,
                    (str(item["id"]), item["kind"], int(item["vec"].shape[0]), _vec_to_blob(item["vec"]), ts),
                )
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                _time.sleep(0.5 * (attempt + 1))
                continue
            raise
    if last_exc is not None:
        raise last_exc


def is_duplicate_idea(
    conn: sqlite3.Connection,
    *,
    text: str,
    threshold: float = 0.92,
    only_kind: str | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Bloqueia hipoteses quase identicas a alguma ja registrada.

    Threshold ~0.92 = mesmo sentido / pequenas variacoes lexicas. Usa o
    encoder ST quando disponivel; com fallback hash a similaridade fica mais
    estrutural (engine+params+keywords), o que ainda barra duplicatas obvias.
    """
    if _DISABLED or not text:
        return False, None
    try:
        similar = find_similar(conn, text=text, k=1, only_kind=only_kind)
        if not similar:
            return False, None
        top = similar[0]
        if float(top.get("similarity", 0.0)) >= float(threshold):
            return True, top
        return False, None
    except Exception:
        return False, None


def render_for_prompt(similar: list[dict[str, Any]], *, max_chars: int = 600) -> str:
    """Formata lista de similares para injecao em prompt LLM."""
    if not similar:
        return ""
    lines = []
    for s in similar:
        sim = s.get("similarity", 0.0)
        status = s.get("status", "")
        name = (s.get("name") or s.get("node_id") or "")[:60]
        engine = s.get("engine", "")
        if status in ("pruned", "paused"):
            reason = (s.get("prune_reason") or "sem motivo")[:80]
            lines.append(f"- ja rejeitada ({sim:.2f}, {engine}): {name} -> {reason}")
        elif status == "validated":
            sc = s.get("score", 0.0)
            lines.append(f"- ja validada ({sim:.2f}, {engine}, score {sc:.2f}): {name}")
        else:
            lines.append(f"- em teste ({sim:.2f}, {engine}, {status}): {name}")
        if sum(len(l) for l in lines) > max_chars:
            break
    return "\n".join(lines)

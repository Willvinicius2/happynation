"""Context Broker: agente de contexto unico que conversa com o provedor LLM
configurado e despacha tarefas para os demais "papeis"
(hypothesis_generator, validation_agent, archivist_agent, etc).

Por que: cada role do Aether hoje abre uma chamada LLM stateless reenviando
system prompt e contexto da arvore (~120 nodes). Como esse contexto repete
quase identico entre roles, gastamos tokens replicando-o N vezes por ciclo.
O broker:
  1. Coleta o snapshot da arvore UMA vez por ciclo (TTL configuravel).
  2. Reusa sessao quando o transporte suporta isso; caso contrario, injeta o
     snapshot compacto na propria chamada para manter o comportamento correto.
  3. Expoe `dispatch(role, system, prompt) -> dict` no mesmo formato que
     `call_llm_agent`, para ser usado como caller drop-in.

Habilitar com: `AETHER_CONTEXT_BROKER=1`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time as _time
from typing import Any

from .config import db_path
from .llm_runtime import (
    LLMProvider,
    _provider_for_name,
    _effective_role_provider_name,
    call_claude_code_result,
    call_codex_cli_result,
    call_openai_codex_result,
    call_claude_api_result,
)


_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "session_id": None,
    "snapshot_ts": 0.0,
    "snapshot": "",
    "primed": False,
    "calls_in_session": 0,
}


_BROKER_SYSTEM = """Voce e o Context Broker do Aether: um unico agente que mantem em memoria o estado atual da arvore de hipoteses RAIO e atende multiplos papeis no mesmo chat para economizar tokens.

PAPEIS QUE VOCE ATENDE (responda no formato pedido por cada papel):

- hypothesis_generator: gera hipoteses cripto testaveis em backtest. Engines validas: rsi, bollinger, ma_crossover, donchian, supertrend. Responda SOMENTE JSON {"hypotheses": [...]}.
- validation_agent: avalia validados qualitativamente (sharpe, mdd, hit_rate). Responda JSON com {"verdict": "approved|paused|reject", "reason": "..."}.
- archivist_agent: resume aprendizados, marca duplicatas e relembra falhas. Responda em prosa concisa.
- chief_orchestrator: decide proximo passo do ciclo. Responda em prosa curta com diretivas.
- portfolio_agent: pesa estrategias validadas para o paper. Responda JSON.
- manifest_agent: gera/atualiza manifest. Responda JSON estruturado.
- infra_data_agent: planeja datasets/experimentos. Responda em prosa.

REGRAS GERAIS:
- O contexto da arvore foi enviado no primeiro turno desta sessao. NAO repita o contexto na resposta — apenas USE.
- Cada turno comeca com "[ROLE=<role>] [TASK]" + a tarefa especifica. Responda apenas a tarefa.
- Foque em FINALIZAR ramos abertos antes de propor raizes novas (ramo aberto = candidate/queued/running/expand). Se a arvore tem >12 ramos abertos, prefira mutacao/expansao, nao raiz nova.
- Priorize hipoteses fora da familia dominante.
"""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    return os.environ.get("AETHER_CONTEXT_BROKER", "0").strip() in {"1", "true", "yes", "on"}


def _build_snapshot(conn: sqlite3.Connection, campaign_id: str) -> str:
    """Snapshot compacto da arvore: contagens por status/engine + topos."""
    rows = conn.execute(
        """
        SELECT id, name, status, score, depth, parent_id, prune_reason, metrics_json, tags_json, updated_at
        FROM nodes
        WHERE campaign_id=? AND id != 'root' AND id NOT LIKE 'tf:%'
        ORDER BY updated_at DESC LIMIT 200
        """,
        (campaign_id,),
    ).fetchall()

    by_status: dict[str, int] = {}
    by_engine: dict[str, int] = {}
    open_branches: list[str] = []
    validated_top: list[str] = []
    pruned_recent: list[str] = []

    OPEN = {"candidate", "queued", "running", "expand", "expanded"}
    for row in rows:
        st = str(row["status"] or "").lower()
        by_status[st] = by_status.get(st, 0) + 1
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
            tags = json.loads(row["tags_json"] or "[]")
        except Exception:
            metrics, tags = {}, []
        engine = ""
        best = metrics.get("best_probe") or {}
        if isinstance(best, dict):
            engine = str(best.get("engine") or "")
        if not engine:
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("engine:"):
                    engine = tag.split(":", 1)[1]
                    break
        engine = (engine or "?").lower()
        by_engine[engine] = by_engine.get(engine, 0) + 1
        nm = str(row["name"] or row["id"])[:60]
        if st in OPEN and len(open_branches) < 16:
            open_branches.append(f"- [{st}] {nm} (depth={row['depth']}, engine={engine}, score={row['score']:.2f})")
        elif st == "validated" and len(validated_top) < 8:
            validated_top.append(f"- {nm} (engine={engine}, score={row['score']:.2f})")
        elif st in ("pruned", "paused") and len(pruned_recent) < 8:
            reason = str(row["prune_reason"] or "")[:60]
            pruned_recent.append(f"- {nm} (engine={engine}) -> {reason}")

    total = sum(by_status.values()) or 1
    open_count = sum(by_status.get(s, 0) for s in OPEN)
    closed_count = total - open_count
    completion = closed_count / total

    parts: list[str] = [
        f"ARVORE (campaign={campaign_id}): total={total}, abertos={open_count}, finalizados={closed_count}, completion={completion:.2f}",
        "STATUS: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items(), key=lambda kv: -kv[1])),
        "ENGINES: " + ", ".join(f"{k}={v}" for k, v in sorted(by_engine.items(), key=lambda kv: -kv[1])),
        "RAMOS ABERTOS (priorize finalizar antes de criar novos):",
    ]
    parts.extend(open_branches or ["- (nenhum)"])
    parts.append("VALIDATED TOPO:")
    parts.extend(validated_top or ["- (nenhum)"])
    parts.append("PODADOS RECENTES (evitar repetir):")
    parts.extend(pruned_recent or ["- (nenhum)"])
    return "\n".join(parts)


def _refresh_snapshot_if_stale(conn: sqlite3.Connection, campaign_id: str) -> str:
    ttl = _env_float("AETHER_BROKER_SNAPSHOT_TTL", 90.0)
    now = _time.time()
    if (now - float(_STATE.get("snapshot_ts") or 0.0)) > ttl or not _STATE.get("snapshot"):
        snap = _build_snapshot(conn, campaign_id)
        _STATE["snapshot"] = snap
        _STATE["snapshot_ts"] = now
        # Snapshot novo invalida sessao: o turno-1 ficou desatualizado.
        _STATE["primed"] = False
        _STATE["session_id"] = None
        _STATE["calls_in_session"] = 0
    return _STATE["snapshot"]


def _max_calls_per_session() -> int:
    return max(1, _env_int("AETHER_BROKER_MAX_CALLS", 30))


def _provider() -> LLMProvider:
    return _provider_for_name(os.environ.get("AETHER_BROKER_PROVIDER") or _effective_role_provider_name("hypothesis_generator"))


def _provider_supports_session(provider: LLMProvider) -> bool:
    return provider.name == "claude" and provider.transport in {"claude_code", "cli"}


def _direct_call_llm_agent(role: str, *, system: str, prompt: str, max_tokens: int, timeout: float) -> dict[str, Any]:
    from .llm_runtime import call_llm_agent

    old = os.environ.get("AETHER_CONTEXT_BROKER")
    os.environ["AETHER_CONTEXT_BROKER"] = "0"
    try:
        return call_llm_agent(role, system=system, prompt=prompt, max_tokens=max_tokens, timeout=timeout)
    finally:
        if old is None:
            os.environ.pop("AETHER_CONTEXT_BROKER", None)
        else:
            os.environ["AETHER_CONTEXT_BROKER"] = old


def _call_provider(*, system: str, prompt: str, max_tokens: int, timeout: float) -> dict[str, Any]:
    """Chama o provedor LLM configurado para o broker."""
    provider = _provider()
    if provider.name == "claude" and provider.transport in {"claude_code", "cli"}:
        return call_claude_code_result(
            system=system, prompt=prompt, max_tokens=max_tokens,
            timeout=max(timeout, 120.0), provider=provider, role="context_broker",
        )
    if provider.name == "claude":
        return call_claude_api_result(
            system=system, prompt=prompt, max_tokens=max_tokens,
            timeout=timeout, provider=provider,
        )
    if provider.transport in {"codex_cli", "cli"}:
        return call_codex_cli_result(
            system=system, prompt=prompt, max_tokens=max_tokens,
            timeout=max(timeout, 120.0), provider=provider,
        )
    return call_openai_codex_result(
        system=system, prompt=prompt, max_tokens=max_tokens,
        timeout=timeout, provider=provider,
    )


def dispatch(role: str, *, system: str, prompt: str, max_tokens: int = 700, timeout: float = 60.0) -> dict[str, Any]:
    """Roteia tarefa de `role` via sessao unica do broker.

    Compativel com call_llm_agent (mesmo retorno: {ok, text, provider, model, ...}).
    Fallback automatico para call_llm_agent direto se o broker nao puder ser usado.
    """
    if not is_enabled():
        return _direct_call_llm_agent(role, system=system, prompt=prompt, max_tokens=max_tokens, timeout=timeout)

    try:
        conn = sqlite3.connect(str(db_path()), timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            from .campaigns import get_active_campaign_id
            campaign_id = get_active_campaign_id(conn)
            with _LOCK:
                snapshot = _refresh_snapshot_if_stale(conn, campaign_id)
                primed = bool(_STATE.get("primed"))
                calls = int(_STATE.get("calls_in_session") or 0)
                if calls >= _max_calls_per_session():
                    primed = False
                    _STATE["primed"] = False
                    _STATE["session_id"] = None
                    _STATE["calls_in_session"] = 0
        finally:
            conn.close()
    except Exception:
        return _direct_call_llm_agent(role, system=system, prompt=prompt, max_tokens=max_tokens, timeout=timeout)

    provider = _provider()
    if not _provider_supports_session(provider):
        task_prompt = (
            "SNAPSHOT DA ARVORE (use como contexto desta tarefa):\n\n"
            f"{snapshot}\n\n"
            f"[ROLE={role}] [TASK]\n"
            f"Especificacao do papel:\n{system}\n\n"
            f"Tarefa concreta:\n{prompt}\n\n"
            "Responda apenas no formato pedido para o papel."
        )
        try:
            result = _call_provider(
                system=_BROKER_SYSTEM,
                prompt=task_prompt,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except Exception as exc:
            fallback = _direct_call_llm_agent(role, system=system, prompt=prompt, max_tokens=max_tokens, timeout=timeout)
            meta = fallback.get("metadata") or {}
            meta["broker_fallback_reason"] = str(exc)[:200]
            fallback["metadata"] = meta
            return fallback
        return {
            "ok": True,
            "provider": provider.name,
            "model": provider.model,
            "effort": provider.effort,
            "transport": provider.transport,
            "text": result.get("text") or "",
            "error": None,
            "metadata": {
                **(result.get("metadata") or {}),
                "via": "context_broker",
                "broker_role": role,
                "broker_stateless_snapshot": True,
            },
        }

    # Turno 1: prime com manual + snapshot. Turnos seguintes: so a tarefa.
    # System prompt do role original eh agregado ao manual no turno 1
    # para que cada papel preserve sua expectativa especifica de output.
    if not primed:
        prime_system = _BROKER_SYSTEM + "\n\n--- SUBROLE INSTRUCTIONS (" + role + ") ---\n" + system
        prime_prompt = (
            "SNAPSHOT DA ARVORE (memorize, sera referencia para todas as tarefas desta sessao):\n\n"
            f"{snapshot}\n\n"
            "Confirme com OK quando estiver pronto e aguarde a primeira tarefa."
        )
        try:
            _ = _call_provider(
                system=prime_system, prompt=prime_prompt,
                max_tokens=128, timeout=timeout,
            )
            with _LOCK:
                _STATE["primed"] = True
                _STATE["calls_in_session"] = 1
        except Exception:
            return _direct_call_llm_agent(role, system=system, prompt=prompt, max_tokens=max_tokens, timeout=timeout)

    # Turno N: comando curto referenciando snapshot ja conhecido.
    task_prompt = (
        f"[ROLE={role}] [TASK]\n"
        f"Especificacao do papel:\n{system}\n\n"
        f"Tarefa concreta:\n{prompt}\n\n"
        f"Use o snapshot ja enviado. Responda apenas no formato pedido para o papel."
    )
    try:
        result = _call_provider(
            system=_BROKER_SYSTEM, prompt=task_prompt,
            max_tokens=max_tokens, timeout=timeout,
        )
    except Exception as exc:
        with _LOCK:
            _STATE["primed"] = False
            _STATE["session_id"] = None
        fallback = _direct_call_llm_agent(role, system=system, prompt=prompt, max_tokens=max_tokens, timeout=timeout)
        meta = fallback.get("metadata") or {}
        meta["broker_fallback_reason"] = str(exc)[:200]
        fallback["metadata"] = meta
        return fallback

    with _LOCK:
        _STATE["calls_in_session"] = int(_STATE.get("calls_in_session") or 0) + 1
        sid = (result.get("metadata") or {}).get("session_id")
        if sid:
            _STATE["session_id"] = sid

    provider = _provider()
    return {
        "ok": True,
        "provider": provider.name,
        "model": provider.model,
        "effort": provider.effort,
        "transport": provider.transport,
        "text": result.get("text") or "",
        "error": None,
        "metadata": {
            **(result.get("metadata") or {}),
            "via": "context_broker",
            "broker_role": role,
            "broker_calls_in_session": _STATE.get("calls_in_session"),
        },
    }


def reset_session() -> None:
    """Forca reset da sessao (uso em testes ou apos mudanca grande na arvore)."""
    with _LOCK:
        _STATE["primed"] = False
        _STATE["session_id"] = None
        _STATE["calls_in_session"] = 0
        _STATE["snapshot_ts"] = 0.0
        _STATE["snapshot"] = ""


def status() -> dict[str, Any]:
    with _LOCK:
        return {
            "enabled": is_enabled(),
            "primed": bool(_STATE.get("primed")),
            "session_id": _STATE.get("session_id"),
            "calls_in_session": _STATE.get("calls_in_session"),
            "snapshot_age_seconds": (_time.time() - float(_STATE.get("snapshot_ts") or 0.0)) if _STATE.get("snapshot_ts") else None,
            "snapshot_chars": len(_STATE.get("snapshot") or ""),
        }

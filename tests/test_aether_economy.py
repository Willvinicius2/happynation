from __future__ import annotations

import json
from types import SimpleNamespace

from src.research.aether.campaign_constants import CURRENT_CAMPAIGN_ID
from src.research.aether.db import connect, init_db
from src.research.aether.llm_hypothesis import _collect_exploration_context, create_llm_hypothesis_batch
from src.research.aether.llm_runtime import call_llm_agent, role_provider
from src.research.aether.repository import upsert_node


def test_economy_mode_uses_cheaper_default_roles(monkeypatch):
    monkeypatch.setenv("AETHER_ECONOMY_MODE", "1")
    monkeypatch.setenv("AETHER_CLAUDE_TRANSPORT", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    hypothesis = role_provider("hypothesis_generator")
    validator = role_provider("validation_agent")

    assert "opus" not in hypothesis.model
    assert hypothesis.effort == "low"
    assert "opus" not in validator.model
    assert validator.effort == "low"


def test_call_llm_agent_caps_max_tokens_in_economy(monkeypatch):
    from src.research.aether import llm_runtime

    captured = {}
    monkeypatch.setenv("AETHER_ECONOMY_MODE", "1")
    monkeypatch.setenv("AETHER_LLM_MAX_TOKENS", "321")
    monkeypatch.setenv("AETHER_CLAUDE_TRANSPORT", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    def fake_call(**kwargs):
        captured["max_tokens"] = kwargs["max_tokens"]
        return {"text": '{"hypotheses":[]}', "metadata": {}}

    monkeypatch.setattr(llm_runtime, "call_claude_result", fake_call)

    response = llm_runtime.call_llm_agent("hypothesis_generator", system="s", prompt="p", max_tokens=1800)

    assert response["ok"] is True
    assert captured["max_tokens"] == 321


def test_llm_hypothesis_context_is_compact_and_limited(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHER_LLM_CONTEXT_ROWS", "10")
    monkeypatch.setenv("AETHER_LLM_CONTEXT_PRUNED", "3")
    monkeypatch.setenv("AETHER_LLM_CONTEXT_VALIDATED", "2")
    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        for idx in range(20):
            status = "pruned" if idx % 2 == 0 else "validated"
            upsert_node(
                conn,
                id=f"n-{idx}",
                campaign_id=CURRENT_CAMPAIGN_ID,
                code=f"N{idx}",
                name=f"Nome muito longo {idx} " * 10,
                hypothesis="h",
                status=status,
                score=0.5 + idx / 100,
                priority=0.5,
                prune_reason=("motivo muito longo " * 20) if status == "pruned" else None,
                metrics={"probe_specs": [{"engine": "rsi", "params": {"period": 14}}]},
                tags=["engine:rsi"],
            )

        context, _dominant, _pct, _active, _validated = _collect_exploration_context(conn, CURRENT_CAMPAIGN_ID)

    assert context.count("- ") <= 5
    assert len(context) < 900


def test_llm_hypothesis_respects_cooldown_and_does_not_call_model(tmp_path, monkeypatch):
    db = init_db(tmp_path / "aether.sqlite3")
    called = {"count": 0}

    def caller(*args, **kwargs):
        called["count"] += 1
        return {"ok": True, "provider": "test", "model": "test", "text": json.dumps({"hypotheses": []})}

    with connect(db) as conn:
        conn.execute(
            """
            INSERT INTO llm_traces(
                campaign_id, ts, agent_role, action, provider, model, transport, status,
                node_id, session_id, cost_usd, prompt_excerpt, output_excerpt, summary, error, metadata_json
            )
            VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'hypothesis_generator',
                    'hypothesis_generation', 'test', 'test', 'api', 'ok',
                    NULL, NULL, NULL, '', '', '', NULL, '{}')
            """,
            (CURRENT_CAMPAIGN_ID,),
        )

        result = create_llm_hypothesis_batch(conn, caller=caller)

    assert result["skipped"] == "cooldown"
    assert called["count"] == 0


def test_claude_code_reuses_session_for_same_role_and_system(tmp_path, monkeypatch):
    from src.research.aether import llm_runtime

    calls = []
    monkeypatch.setenv("AETHER_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("AETHER_CLAUDE_SESSION_REUSE", "1")
    monkeypatch.setenv("AETHER_CLAUDE_TRANSPORT", "claude_code")
    monkeypatch.setattr(llm_runtime.shutil, "which", lambda name: "claude")

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"result": '{"ok": true}', "session_id": "11111111-1111-1111-1111-111111111111"}),
            stderr="",
        )

    monkeypatch.setattr(llm_runtime.subprocess, "run", fake_run)

    first = llm_runtime.call_claude_code_result(
        role="hypothesis_generator",
        system="system-a",
        prompt="primeiro",
        max_tokens=50,
    )
    second = llm_runtime.call_claude_code_result(
        role="hypothesis_generator",
        system="system-a",
        prompt="segundo",
        max_tokens=50,
    )

    assert "--resume" not in calls[0]
    assert "--append-system-prompt-file" in calls[0]
    assert "--resume" in calls[1]
    assert "11111111-1111-1111-1111-111111111111" in calls[1]
    assert first["metadata"]["session_reused"] is False
    assert second["metadata"]["session_reused"] is True

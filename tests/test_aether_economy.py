from __future__ import annotations

import json
from types import SimpleNamespace

from src.research.aether.campaign_constants import CURRENT_CAMPAIGN_ID
from src.research.aether.db import connect, init_db, set_setting
from src.research.aether.llm_hypothesis import _collect_exploration_context, create_llm_hypothesis_batch
from src.research.aether.llm_runtime import call_llm_agent, role_provider
from src.research.aether.repository import upsert_node


def test_economy_mode_uses_cheaper_default_roles(monkeypatch):
    monkeypatch.setenv("AETHER_ECONOMY_MODE", "1")
    monkeypatch.setenv("AETHER_PRIMARY_LLM_PROVIDER", "openai")
    monkeypatch.setenv("AETHER_OPENAI_TRANSPORT", "api")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.delenv("AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_MODEL", raising=False)
    monkeypatch.delenv("AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_EFFORT", raising=False)

    hypothesis = role_provider("hypothesis_generator")
    validator = role_provider("validation_agent")

    assert hypothesis.name == "openai"
    assert hypothesis.model == "gpt-5.4-mini"
    assert hypothesis.effort == "high"
    assert validator.name == "openai"
    assert validator.model == "gpt-5.4-mini"
    assert validator.effort == "medium"


def test_call_llm_agent_caps_max_tokens_in_economy(monkeypatch):
    from src.research.aether import llm_runtime

    llm_runtime._CB_STATE.clear()
    captured = {}
    monkeypatch.setenv("AETHER_ECONOMY_MODE", "1")
    monkeypatch.setenv("AETHER_ROLE_HYPOTHESIS_GENERATOR_MAX_TOKENS", "321")
    monkeypatch.setenv("AETHER_PRIMARY_LLM_PROVIDER", "openai")
    monkeypatch.setenv("AETHER_OPENAI_TRANSPORT", "api")
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    def fake_call(**kwargs):
        captured["max_tokens"] = kwargs["max_tokens"]
        return {"text": '{"hypotheses":[]}', "metadata": {}}

    monkeypatch.setattr(llm_runtime, "call_openai_codex_result", fake_call)

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


def test_llm_hypothesis_cooldown_ignores_failed_traces(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHER_LLM_HYPOTHESIS_COOLDOWN_MINUTES", "45")
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
                    'hypothesis_generation', 'test', 'test', 'codex_cli', 'error',
                    NULL, NULL, NULL, '', '', '', 'boom', '{}')
            """,
            (CURRENT_CAMPAIGN_ID,),
        )

        result = create_llm_hypothesis_batch(conn, caller=caller)

    assert result.get("skipped") != "cooldown"
    assert called["count"] == 1


def test_web_research_empty_results_enter_backoff(tmp_path, monkeypatch):
    from src.research.aether.orchestrator import _record_web_research_result, _web_research_backoff_state

    monkeypatch.setenv("AETHER_WEB_RESEARCH_ZERO_THRESHOLD", "2")
    monkeypatch.setenv("AETHER_WEB_RESEARCH_EMPTY_COOLDOWN_MINUTES", "15")
    db = init_db(tmp_path / "aether.sqlite3")

    with connect(db) as conn:
        _record_web_research_result(conn, {"created_hypotheses": 0})
        assert _web_research_backoff_state(conn)["active"] is False

        _record_web_research_result(conn, {"created_hypotheses": 0})
        state = _web_research_backoff_state(conn)

    assert state["active"] is True
    assert state["zero_count"] == 2
    assert state["until"]


def test_web_research_reopens_source_after_ranking_reset(tmp_path, monkeypatch):
    from src.research.aether import web_research

    source = {
        "source": "arxiv",
        "query": "crypto trend following",
        "title": "Systematic Trend-Following with Adaptive Portfolio Construction",
        "url": "http://arxiv.org/abs/test",
        "snippet": "Momentum effects, regime-dependent volatility, trailing stop and rolling Sharpe adaptive long-short allocation.",
    }
    monkeypatch.setattr(web_research, "collect_web_sources", lambda **_kwargs: [source])
    db = init_db(tmp_path / "aether.sqlite3")

    with connect(db) as conn:
        first = web_research.create_web_hypothesis_batch(conn, max_new=1)
        conn.execute(
            """
            UPDATE nodes
            SET created_at='2026-05-01T00:00:00Z',
                updated_at='2026-05-01T00:00:00Z'
            WHERE source_type='web_research'
            """
        )
        set_setting(
            conn,
            f"ranking_hidden_node_ids:{CURRENT_CAMPAIGN_ID}",
            {"reset_at": "2026-05-18T20:14:03Z", "hidden_node_ids": []},
        )
        second = web_research.create_web_hypothesis_batch(conn, max_new=1)
        parent_ids = [
            row["id"]
            for row in conn.execute(
                """
                SELECT id
                FROM nodes
                WHERE source_type='web_research' AND parent_id='root'
                ORDER BY id
                """
            )
        ]

    assert first["created_hypotheses"] == 1
    assert second["created_hypotheses"] == 1
    assert len(parent_ids) == 2
    assert any(":r:" in node_id for node_id in parent_ids)


def test_web_research_commits_before_external_collection(tmp_path, monkeypatch):
    from src.research.aether import web_research

    db = init_db(tmp_path / "aether.sqlite3")
    observed = {"in_transaction": None}

    with connect(db) as conn:
        def collect_without_lock(**_kwargs):
            observed["in_transaction"] = conn.in_transaction
            return []

        monkeypatch.setattr(web_research, "collect_web_sources", collect_without_lock)
        result = web_research.create_web_hypothesis_batch(conn, max_new=1)

    assert observed["in_transaction"] is False
    assert result["created_hypotheses"] == 0


def test_web_research_maps_microstructure_sources_to_proxy_specs():
    from src.research.aether.web_research import strategy_specs_from_sources

    specs = strategy_specs_from_sources(
        [
            {
                "source": "arxiv",
                "query": "market microstructure cryptocurrency",
                "title": "Explainable Patterns in Cryptocurrency Microstructure",
                "url": "http://arxiv.org/abs/micro",
                "snippet": "Order book and trade features show stable predictive SHAP shapes across assets, tied to order flow imbalance and spread.",
            }
        ],
        max_specs=1,
    )

    assert specs[0]["slug"] == "web-microstructure-flow-proxy"
    assert set(specs[0]["engines"]) == {"obv_trend", "vwap_dev", "structure_break"}


def test_llm_hypothesis_skips_cleanly_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHER_LLM_ENABLED", "0")
    monkeypatch.setenv("AETHER_LLM_DISABLED_REASON", "credencial invalida")
    db = init_db(tmp_path / "aether.sqlite3")

    def caller(*args, **kwargs):
        raise AssertionError("LLM caller should not run when disabled")

    with connect(db) as conn:
        result = create_llm_hypothesis_batch(conn, caller=caller)

    assert result["skipped"] == "llm_disabled"
    assert result["reason"] == "credencial invalida"
    assert result["created_nodes"] == 0


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

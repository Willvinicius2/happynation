"""Caps por familia no Aether: limita quantos manifests/hipoteses uma
familia (engine) pode acumular para impedir que o sistema fique acomodado
em ideias ja maduras."""
from __future__ import annotations

import json
from pathlib import Path

from src.research.aether import manifest as manifest_module
from src.research.aether.campaign_constants import CURRENT_CAMPAIGN_ID
from src.research.aether.db import connect, init_db
from src.research.aether.llm_hypothesis import create_llm_hypothesis_batch
from src.research.aether.repository import upsert_node


def _make_validated(conn, node_id: str, engine: str, *, score: float = 0.6) -> None:
    upsert_node(
        conn,
        id=node_id,
        campaign_id=CURRENT_CAMPAIGN_ID,
        code=node_id.upper(),
        name=node_id,
        hypothesis="h",
        causal_mechanism="c",
        status="validated",
        score=score,
        priority=score,
        depth=2,
        metrics={
            "engines": [engine],
            "probe_specs": [{"engine": engine, "params": {"period": 14}}],
            "best_probe": {"engine": engine, "params": {"period": 14}, "symbol": "BTCUSDT"},
            "validation_passed": True,
        },
        tags=["hypothesis", f"engine:{engine}"],
        source_type="llm_agent",
    )


def _set_runtime(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("AETHER_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("AETHER_DB_PATH", str(tmp_path / "aether.sqlite3"))
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setattr(manifest_module, "artifact_dir", lambda: artifact_root)
    return artifact_root


def test_manifest_skipped_when_family_cap_reached(tmp_path, monkeypatch):
    artifact_root = _set_runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("AETHER_MANIFEST_PER_FAMILY_CAP", "3")

    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        for idx in range(4):
            _make_validated(conn, f"rsi-{idx}", "rsi", score=0.5 + idx / 100)

    # As primeiras 3 chamadas devem gerar manifest; a 4a deve ser bloqueada
    # pelo cap por familia.
    paths_created = []
    for idx in range(3):
        result = manifest_module.generate_manifest(f"rsi-{idx}")
        assert "skipped" not in result
        assert result["path"]
        paths_created.append(result["path"])

    blocked = manifest_module.generate_manifest("rsi-3")
    assert blocked.get("skipped") == "family_cap"
    assert blocked.get("family") == "rsi"
    assert blocked.get("count") == 3
    assert blocked.get("cap") == 3
    # Nenhum arquivo extra foi criado.
    assert len(list((artifact_root / "manifests").iterdir())) == 3


def test_manifest_force_bypasses_family_cap(tmp_path, monkeypatch):
    artifact_root = _set_runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("AETHER_MANIFEST_PER_FAMILY_CAP", "1")

    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        _make_validated(conn, "rsi-a", "rsi")
        _make_validated(conn, "rsi-b", "rsi")

    first = manifest_module.generate_manifest("rsi-a")
    assert first.get("path")

    blocked = manifest_module.generate_manifest("rsi-b")
    assert blocked.get("skipped") == "family_cap"

    forced = manifest_module.generate_manifest("rsi-b", force=True)
    assert "skipped" not in forced
    assert forced.get("path")
    assert len(list((artifact_root / "manifests").iterdir())) == 2


def test_hypothesis_rejected_when_family_validated_cap_reached(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHER_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("AETHER_DB_PATH", str(tmp_path / "aether.sqlite3"))
    monkeypatch.setenv("AETHER_FAMILY_VALIDATED_CAP", "2")
    monkeypatch.setenv("AETHER_FAMILY_NODE_CAP", "0")  # desliga cap de ativos
    monkeypatch.setenv("AETHER_LLM_HYPOTHESIS_COOLDOWN_MINUTES", "0")

    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        # Familia rsi ja saturou: 3 validados (cap=2). Outras familias com
        # validados/ativos para a dominancia ficar abaixo do diversity gate.
        for idx in range(3):
            _make_validated(conn, f"rsi-validated-{idx}", "rsi", score=0.5 + idx / 100)
        for idx in range(5):
            _make_validated(conn, f"bollinger-validated-{idx}", "bollinger", score=0.5 + idx / 100)
        for idx in range(5):
            _make_validated(conn, f"donchian-validated-{idx}", "donchian", score=0.5 + idx / 100)

        def caller(*_args, **_kwargs):
            return {
                "ok": True,
                "provider": "test",
                "model": "test",
                "text": json.dumps(
                    {
                        "hypotheses": [
                            {
                                "slug": "novo-rsi",
                                "name": "Novo RSI puro",
                                "hypothesis": "Tese rsi",
                                "causal_mechanism": "mr",
                                "engines": ["rsi"],
                                "probe_specs": [
                                    {"engine": "rsi", "params": {"period": 21, "oversold": 25, "overbought": 75}}
                                ],
                                "base_score": 0.55,
                            },
                            {
                                "slug": "novo-supertrend",
                                "name": "Novo Supertrend",
                                "hypothesis": "tese st",
                                "causal_mechanism": "mr",
                                "engines": ["supertrend"],
                                "probe_specs": [
                                    {"engine": "supertrend", "params": {"atr_period": 14, "atr_mult": 3.0, "long_only": True}}
                                ],
                                "base_score": 0.55,
                            },
                        ]
                    }
                ),
            }

        result = create_llm_hypothesis_batch(conn, max_new=2, caller=caller)

    assert result["created_hypotheses"] == 1
    assert result["rejected_family_cap"] == 1
    # Diversity gate nao deve ter atuado (dominance < 60% com bollinger=5/13).
    assert result.get("rejected_diversity", 0) == 0


def test_hypothesis_rejected_when_family_node_cap_reached(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHER_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("AETHER_DB_PATH", str(tmp_path / "aether.sqlite3"))
    monkeypatch.setenv("AETHER_FAMILY_VALIDATED_CAP", "0")  # desliga cap de validados
    monkeypatch.setenv("AETHER_FAMILY_NODE_CAP", "3")
    monkeypatch.setenv("AETHER_LLM_HYPOTHESIS_COOLDOWN_MINUTES", "0")

    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        # 4 nos ativos da familia bollinger (cap=3) e outras familias para
        # manter dominance abaixo do gate de diversidade.
        for idx in range(4):
            upsert_node(
                conn,
                id=f"bb-{idx}",
                campaign_id=CURRENT_CAMPAIGN_ID,
                code=f"BB{idx}",
                name=f"bb-{idx}",
                hypothesis="h",
                status="candidate",
                score=0.5,
                priority=0.5,
                depth=2,
                metrics={
                    "engines": ["bollinger"],
                    "probe_specs": [{"engine": "bollinger", "params": {"window": 20}}],
                    "best_probe": {"engine": "bollinger", "params": {"window": 20}},
                },
                tags=["engine:bollinger"],
                source_type="llm_agent",
            )
        for idx in range(6):
            upsert_node(
                conn,
                id=f"rsi-active-{idx}",
                campaign_id=CURRENT_CAMPAIGN_ID,
                code=f"R{idx}",
                name=f"rsi-{idx}",
                hypothesis="h",
                status="candidate",
                score=0.5,
                priority=0.5,
                depth=2,
                metrics={
                    "engines": ["rsi"],
                    "best_probe": {"engine": "rsi", "params": {"period": 14}},
                },
                tags=["engine:rsi"],
                source_type="llm_agent",
            )

        def caller(*_args, **_kwargs):
            return {
                "ok": True,
                "provider": "test",
                "model": "test",
                "text": json.dumps(
                    {
                        "hypotheses": [
                            {
                                "slug": "bb-novo",
                                "name": "Novo Bollinger",
                                "hypothesis": "tese",
                                "causal_mechanism": "mr",
                                "engines": ["bollinger"],
                                "probe_specs": [
                                    {"engine": "bollinger", "params": {"window": 30, "num_std": 2.5, "long_only": True}}
                                ],
                                "base_score": 0.55,
                            },
                        ]
                    }
                ),
            }

        result = create_llm_hypothesis_batch(conn, max_new=1, caller=caller)

    assert result["created_hypotheses"] == 0
    assert result["rejected_family_cap"] == 1


def test_hypothesis_multi_engine_bypasses_family_cap(tmp_path, monkeypatch):
    """Hipoteses com mais de uma engine nao sao bloqueadas pelo cap por
    familia, ja que representam combinacoes novas."""
    monkeypatch.setenv("AETHER_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("AETHER_DB_PATH", str(tmp_path / "aether.sqlite3"))
    monkeypatch.setenv("AETHER_FAMILY_VALIDATED_CAP", "1")
    monkeypatch.setenv("AETHER_FAMILY_NODE_CAP", "1")
    monkeypatch.setenv("AETHER_LLM_HYPOTHESIS_COOLDOWN_MINUTES", "0")

    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        for idx in range(3):
            _make_validated(conn, f"rsi-{idx}", "rsi", score=0.5 + idx / 100)

        def caller(*_args, **_kwargs):
            return {
                "ok": True,
                "provider": "test",
                "model": "test",
                "text": json.dumps(
                    {
                        "hypotheses": [
                            {
                                "slug": "combo-rsi-bb",
                                "name": "Combo rsi+bollinger",
                                "hypothesis": "h",
                                "causal_mechanism": "c",
                                "engines": ["rsi", "bollinger"],
                                "probe_specs": [
                                    {"engine": "rsi", "params": {"period": 14}},
                                    {"engine": "bollinger", "params": {"window": 20}},
                                ],
                                "base_score": 0.55,
                            }
                        ]
                    }
                ),
            }

        result = create_llm_hypothesis_batch(conn, max_new=1, caller=caller, force_exploration=True)

    assert result["created_hypotheses"] == 1
    assert result["rejected_family_cap"] == 0

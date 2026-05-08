"""Smoke tests do servidor Aether.

Garante que cada endpoint sobe e responde sem 5xx num runtime isolado
(banco SQLite efemero por teste, nada sai pro %LOCALAPPDATA% nem para o
projeto). Esses testes nao validam logica de negocio do Aether; eles
validam apenas que o pacote integrado ao HAPPYNATIONBOT continua
bootavel apos refactors do Codex ou meus.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def aether_client(monkeypatch):
    """Sobe o app FastAPI do Aether com runtime isolado."""
    tmp = Path(tempfile.mkdtemp(prefix="aether_smoke_"))
    monkeypatch.setenv("AETHER_RUNTIME_DIR", str(tmp))

    # Aether usa caches em modulo a partir do .env do processo;
    # forcar reimportacao garante que todos os modulos peguem o tmp.
    for name in list(sys.modules):
        if name.startswith("src.research.aether"):
            del sys.modules[name]

    from fastapi.testclient import TestClient
    from src.research.aether.server import app

    with TestClient(app) as client:
        yield client

    shutil.rmtree(tmp, ignore_errors=True)


def test_get_state_returns_payload(aether_client):
    response = aether_client.get("/api/state")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, dict)


def test_get_ranking_returns_payload(aether_client):
    response = aether_client.get("/api/ranking")
    assert response.status_code == 200


def test_get_campaigns_returns_payload(aether_client):
    response = aether_client.get("/api/campaigns")
    assert response.status_code == 200


def test_get_llm_agents_status(aether_client):
    response = aether_client.get("/api/agents/llm")
    assert response.status_code == 200
    body = response.json()
    assert "providers" in body or "ok" in body


def test_get_llm_traces(aether_client):
    response = aether_client.get("/api/agents/traces")
    assert response.status_code == 200


def test_post_create_campaign(aether_client):
    response = aether_client.post("/api/campaigns/new")
    assert response.status_code == 200


def test_post_seed_tree(aether_client):
    response = aether_client.post("/api/tree/seed")
    assert response.status_code == 200


def test_post_queue_scouts(aether_client):
    response = aether_client.post("/api/tasks/queue-scouts")
    assert response.status_code == 200


def test_post_research_web(aether_client):
    response = aether_client.post("/api/research/web")
    assert response.status_code == 200


def test_post_research_llm(aether_client):
    response = aether_client.post("/api/research/llm")
    assert response.status_code == 200


def test_post_cycle_stop_when_idle(aether_client):
    response = aether_client.post("/api/cycle/stop")
    assert response.status_code == 200
    body = response.json()
    assert body.get("stopping") is False


def test_post_import_legacy_handles_missing_root(aether_client):
    """Se LEGACY_ROOT nao existir, endpoint nao silencia o erro."""
    response = aether_client.post("/api/import/legacy")
    assert response.status_code == 200
    body = response.json()
    if not Path(body.get("legacy_root", "")).exists():
        assert body.get("ok") is False
        assert body.get("reason") == "legacy_root_missing"


def test_get_index_serves_static(aether_client):
    response = aether_client.get("/")
    assert response.status_code == 200
    assert "html" in response.headers.get("content-type", "").lower()


def test_uvicorn_module_path_resolves():
    """Garante que o caminho de modulo passado para uvicorn.run em main()
    realmente existe — bug que aparece so quando o servidor sobe via CLI
    (`python -m src.research.aether.server`), nao via TestClient."""
    import importlib

    from src.research.aether import server as server_module

    source = Path(server_module.__file__).read_text(encoding="utf-8")
    import re

    match = re.search(r'uvicorn\.run\(\s*"([^"]+):app"', source)
    assert match, "uvicorn.run sem string de app explicita"
    module_path = match.group(1)
    importlib.import_module(module_path)


def test_aether_main_does_not_silently_bump_port(monkeypatch):
    from src.research.aether import server as server_module

    monkeypatch.delenv("AETHER_ALLOW_PORT_FALLBACK", raising=False)
    monkeypatch.setattr(server_module, "_port_available", lambda port: False if port == 8765 else True)

    with pytest.raises(RuntimeError, match="porta 8765"):
        server_module._select_port(8765)


def test_aether_port_fallback_is_explicit(monkeypatch):
    from src.research.aether import server as server_module

    monkeypatch.setenv("AETHER_ALLOW_PORT_FALLBACK", "1")
    monkeypatch.setattr(server_module, "_port_available", lambda port: port == 8766)

    assert server_module._select_port(8765) == 8766

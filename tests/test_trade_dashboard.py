from fastapi.testclient import TestClient
import pytest

from src.dashboard import _configured_loop_assets, app


def test_trade_dashboard_monitor_payload():
    response = TestClient(app).get("/api/monitor")

    assert response.status_code == 200
    body = response.json()
    assert "overview" in body
    assert "paper" in body
    assert "active_strategies" in body
    assert "trades" in body


def test_trade_dashboard_reads_current_main_loop_assets():
    assets = _configured_loop_assets()

    assert {"BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT"}.issubset(assets)
    assert "XRPUSDT" not in assets


def test_trade_dashboard_does_not_silently_bump_port(monkeypatch):
    import src.dashboard as dashboard_module

    monkeypatch.delenv("TRADE_DASHBOARD_ALLOW_PORT_FALLBACK", raising=False)
    monkeypatch.setattr(dashboard_module, "_port_available", lambda port: False if port == 8000 else True)

    with pytest.raises(RuntimeError, match="porta 8000"):
        dashboard_module._select_port(8000)


def test_trade_dashboard_port_fallback_is_explicit(monkeypatch):
    import src.dashboard as dashboard_module

    monkeypatch.setenv("TRADE_DASHBOARD_ALLOW_PORT_FALLBACK", "1")
    monkeypatch.setattr(dashboard_module, "_port_available", lambda port: port == 8001)

    assert dashboard_module._select_port(8000) == 8001

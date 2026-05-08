"""Testes para guards de seguranca: piso de votos, kill switch, ATR stop, lock."""
from __future__ import annotations

import os
import sys
import json
from pathlib import Path

import pandas as pd
import pytest

# Garante que cwd nao polua arquivos de paper_balance reais.
@pytest.fixture(autouse=True)
def _isolated_paper_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "logs" / "paper_balance").mkdir(parents=True, exist_ok=True)
    yield


# ---------------------------------------------------------------------------
# Piso global de votos: nem entry_mode nem kwargs podem burlar.
# ---------------------------------------------------------------------------

from src.strategies.multi_strategy import MultiStrategy, VoteSpec, HARD_FLOOR_BUY_VOTES, HARD_FLOOR_BUY_WEIGHT  # noqa: E402


class DummyTrader:
    actual_trade_position = False
    last_buy_price = 0
    last_buy_time = None
    operation_code = "TESTUSDT"


def _buy(stock_data, verbose=False):
    return True


def _neutral(stock_data, verbose=False):
    return None


def _raise_if_called(stock_data, verbose=False):
    raise AssertionError("legacy strategy should not run in Aether-only mode")


def test_floor_blocks_single_vote_buy_even_with_kwargs():
    """Mesmo passando min_buy_votes=1, o piso global forca >=2."""
    data = pd.DataFrame({"close_price": [100.0, 101.0]})
    strategies = [VoteSpec("only", _buy), VoteSpec("n", _neutral)]
    decision = MultiStrategy(
        DummyTrader(),
        stock_data=data,
        verbose=False,
        min_buy_votes=1,  # tentativa de burlar
        min_buy_weight=1.0,
        strategies=strategies,
    )
    assert decision is None, "Voto isolado nao pode comprar"


def test_floor_blocks_aggressive_entry_mode():
    """entry_mode='aggressive' foi removido: cai pra normal e exige >=2 votos."""
    data = pd.DataFrame({"close_price": [100.0, 101.0]})
    strategies = [VoteSpec("only", _buy), VoteSpec("n", _neutral)]
    decision = MultiStrategy(
        DummyTrader(),
        stock_data=data,
        verbose=False,
        entry_mode="aggressive",  # deprecated
        strategies=strategies,
    )
    assert decision is None


def test_buys_with_two_votes():
    data = pd.DataFrame({"close_price": [100.0, 101.0]})
    strategies = [VoteSpec("a", _buy), VoteSpec("b", _buy), VoteSpec("n", _neutral)]
    decision = MultiStrategy(DummyTrader(), stock_data=data, verbose=False, strategies=strategies)
    assert decision is True


def test_aether_only_ignores_legacy_votes_when_active_specs(monkeypatch):
    monkeypatch.setenv("AETHER_STRATEGIES_ONLY", "1")
    from src.modules.StrategyResearchEngine import StrategySpec

    data = pd.DataFrame({"close_price": [100.0, 101.0]})
    legacy = [VoteSpec("legacy-a", _buy), VoteSpec("legacy-b", _buy)]
    active = [StrategySpec("Aether:neutral#ok", _neutral, {})]

    decision = MultiStrategy(
        DummyTrader(),
        stock_data=data,
        verbose=False,
        strategies=legacy,
        active_strategy_specs=active,
    )

    assert decision is None


def test_strategy_runner_aether_only_blocks_legacy_when_asset_has_no_active(monkeypatch):
    monkeypatch.setenv("AETHER_STRATEGIES_ONLY", "1")
    from src.modules.StrategyRunner import StrategyRunner
    from src.modules.StrategyResearchEngine import StrategySpec
    import src.research.active as active

    monkeypatch.setattr(active, "active_manifest_enabled", lambda: True)
    monkeypatch.setattr(
        active,
        "get_active_specs",
        lambda asset=None: [] if asset == "SOLUSDT" else [StrategySpec("Aether:other#ok", _buy, {})],
    )

    trader = DummyTrader()
    trader.operation_code = "SOLUSDT"
    trader.fallback_activated = False
    data = pd.DataFrame({"close_price": [100.0, 101.0]})

    decision = StrategyRunner.execute(
        trader,
        _raise_if_called,
        _raise_if_called,
        data,
        main_strategy_args={},
        fallback_strategy_args={},
        verbose=False,
    )

    assert decision is None


def test_constants_meet_minimum():
    assert HARD_FLOOR_BUY_VOTES >= 2
    assert HARD_FLOOR_BUY_WEIGHT >= 2.0


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

def test_kill_switch_blocks_buy(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "KILL"))
    # reimporta com novo env
    import importlib
    import src.modules.PaperBalanceLogger as pbl
    importlib.reload(pbl)

    (tmp_path / "KILL").write_text("test", encoding="utf-8")
    allowed, reason = pbl.can_open_new_position("BTCUSDT")
    assert allowed is False
    assert "KILL" in reason


def test_kill_switch_off_when_file_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "NOPE"))
    monkeypatch.setenv("RISK_GUARD_ENABLED", "false")  # ignora outros guards
    import importlib
    import src.modules.PaperBalanceLogger as pbl
    importlib.reload(pbl)

    allowed, _ = pbl.can_open_new_position("BTCUSDT")
    assert allowed is True


def test_asset_kill_switch_blocks_only_that_asset(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "NOPE"))
    monkeypatch.setenv("KILL_SWITCH_PER_ASSET_DIR", str(tmp_path / "kills"))
    monkeypatch.setenv("RISK_GUARD_ENABLED", "false")  # ignora outros guards
    import importlib
    import src.modules.PaperBalanceLogger as pbl
    importlib.reload(pbl)

    kills_dir = tmp_path / "kills"
    kills_dir.mkdir()
    (kills_dir / "BTCUSDT.json").write_text(
        '{"asset":"BTCUSDT","mode":"graceful","reason":"btc pausado"}',
        encoding="utf-8",
    )

    blocked, reason = pbl.can_open_new_position("BTCUSDT")
    allowed, _ = pbl.can_open_new_position("ETHUSDT")
    assert blocked is False
    assert "btc pausado" in reason
    assert allowed is True


def test_real_order_requires_explicit_confirmation(monkeypatch):
    monkeypatch.delenv("REAL_TRADING_CONFIRM", raising=False)
    from src.modules.BinanceTraderBot import BinanceTraderBot

    trader = BinanceTraderBot.__new__(BinanceTraderBot)
    trader.operation_code = "BTCUSDT"
    assert trader._real_order_allowed("BUY") is False

    monkeypatch.setenv("REAL_TRADING_CONFIRM", "true")
    assert trader._real_order_allowed("BUY") is True


def test_paper_trading_defaults_to_safe_true(monkeypatch):
    monkeypatch.delenv("PAPER_TRADING", raising=False)
    from src.modules.BinanceTraderBot import _env_truthy

    assert _env_truthy("PAPER_TRADING", True) is True


def test_active_manifest_requires_catalog_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRE_ACTIVE_STRATEGY_APPROVAL", "true")
    active_path = tmp_path / "active_strategies.json"
    catalog_path = tmp_path / "strategy_catalog.json"
    active_path.write_text(
        """
        {
          "strategies": [
            {"id": "ok", "template": "RSI_Reversal", "params": {"rsi_period": 14, "low": 30, "high": 70, "confirm": 2}},
            {"id": "blocked", "template": "RSI_Reversal", "params": {"rsi_period": 14, "low": 30, "high": 70, "confirm": 2}}
          ]
        }
        """,
        encoding="utf-8",
    )
    catalog_path.write_text(
        """
        {
          "entries": {
            "ok": {"id": "ok", "status": "approved"},
            "blocked": {"id": "blocked", "status": "rejected"}
          }
        }
        """,
        encoding="utf-8",
    )

    from src.research.active import get_active_specs

    specs = get_active_specs(path=active_path, catalog_path=catalog_path)
    assert [spec.name for spec in specs] == ["RSI_Reversal#ok"]


def test_aether_active_combo_can_bypass_catalog_only_when_validated_and_critic_approved(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRE_ACTIVE_STRATEGY_APPROVAL", "true")
    active_path = tmp_path / "active_strategies.json"
    catalog_path = tmp_path / "strategy_catalog.json"
    active_path.write_text(
        json.dumps(
            {
                "strategies": [
                    {
                        "id": "aether-combo",
                        "source": "aether_auto_deploy",
                        "engine": "combo",
                        "params": {
                            "mode": "consensus",
                            "specs": [
                                {"engine": "ma_crossover", "params": {"short_window": 2, "long_window": 4, "long_only": True}},
                                {"engine": "ma_crossover", "params": {"short_window": 3, "long_window": 5, "long_only": True}},
                            ],
                        },
                        "probe_specs": [
                            {"engine": "ma_crossover", "params": {"short_window": 2, "long_window": 4, "long_only": True}},
                            {"engine": "ma_crossover", "params": {"short_window": 3, "long_window": 5, "long_only": True}},
                        ],
                        "asset": "TESTUSDT",
                        "summary": {"validation_passed": True, "critic_status": "approved"},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    catalog_path.write_text('{"entries": {}}', encoding="utf-8")

    from src.research.active import get_active_specs

    specs = get_active_specs(path=active_path, asset="TESTUSDT", catalog_path=catalog_path)
    assert [spec.name for spec in specs] == ["Aether:combo#er-combo"]

    stock_data = pd.DataFrame(
        {
            "open_price": range(1, 12),
            "high_price": range(2, 13),
            "low_price": range(0, 11),
            "close_price": range(1, 12),
            "volume": [100] * 11,
        }
    )
    assert specs[0].function(stock_data=stock_data) is True


def test_deploy_record_is_idempotent_for_same_payload(tmp_path):
    from src.research.active import deploy_record

    active_path = tmp_path / "active_strategies.json"
    record = {
        "id": "aether-test",
        "engine": "rsi",
        "params": {"period": 14, "oversold": 30, "overbought": 70},
        "asset": "BTCUSDT",
        "timeframe": "15m",
        "score": 0.7,
        "source": "aether_auto_deploy",
    }

    deploy_record(record, path=active_path, max_per_family=None)
    first = json.loads(active_path.read_text(encoding="utf-8"))
    deploy_record(record, path=active_path, max_per_family=None)
    second = json.loads(active_path.read_text(encoding="utf-8"))

    assert second["updated_at"] == first["updated_at"]
    assert second["strategies"][0]["deployed_at"] == first["strategies"][0]["deployed_at"]
    assert len(second["strategies"]) == 1


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def test_atomic_write_creates_bak(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "logs" / "paper_balance").mkdir(parents=True, exist_ok=True)
    import importlib
    import src.modules.PaperBalanceLogger as pbl
    importlib.reload(pbl)

    pbl._save_state({"a": 1})
    pbl._save_state({"a": 2})
    assert pbl.STATE_JSON.exists()
    bak = pbl.STATE_JSON.with_suffix(pbl.STATE_JSON.suffix + ".bak")
    assert bak.exists(), "segundo save deve gerar .bak da versao anterior"


def test_load_state_falls_back_to_bak_on_corruption(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "logs" / "paper_balance").mkdir(parents=True, exist_ok=True)
    import importlib
    import src.modules.PaperBalanceLogger as pbl
    importlib.reload(pbl)

    pbl._save_state({"a": 42})
    pbl._save_state({"a": 99})
    pbl.STATE_JSON.write_text("not json {", encoding="utf-8")
    state = pbl._load_state()
    assert state == {"a": 42}, "deve recuperar do .bak"


# ---------------------------------------------------------------------------
# Lockfile
# ---------------------------------------------------------------------------

def test_instance_lock_blocks_second(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BOT_LOCK_FILE", str(tmp_path / "src" / "logs" / ".bot.lock"))
    import importlib
    import src.modules.InstanceLock as il
    importlib.reload(il)

    # Simula outra instancia viva: usa PID de processo conhecido (parent)
    other_pid = os.getppid() if os.getppid() != os.getpid() else 4  # System process no Windows
    if not il._is_pid_alive(other_pid) or other_pid == os.getpid():
        pytest.skip("Sem PID alternativo vivo para teste")
    il.LOCK_PATH.write_text(str(other_pid), encoding="utf-8")
    with pytest.raises(SystemExit):
        il.acquire_or_die()


def test_instance_lock_releases_stale(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BOT_LOCK_FILE", str(tmp_path / "src" / "logs" / ".bot.lock"))
    import importlib
    import src.modules.InstanceLock as il
    importlib.reload(il)

    # PID gigantesco que provavelmente nao existe
    il.LOCK_PATH.write_text("99999999", encoding="utf-8")
    il.acquire_or_die()
    assert il.LOCK_PATH.read_text(encoding="utf-8") == str(os.getpid())


# ---------------------------------------------------------------------------
# ATR dynamic stop
# ---------------------------------------------------------------------------

def test_atr_pct_returns_none_with_short_data(monkeypatch):
    from src.modules.BinanceTraderBot import BinanceTraderBot
    bot = BinanceTraderBot.__new__(BinanceTraderBot)
    bot.stock_data = pd.DataFrame({
        "high_price": [100.0, 101.0],
        "low_price": [99.0, 100.0],
        "close_price": [100.0, 100.5],
    })
    assert bot._atr_pct(period=14) is None


def test_atr_pct_with_synthetic_data():
    from src.modules.BinanceTraderBot import BinanceTraderBot
    bot = BinanceTraderBot.__new__(BinanceTraderBot)
    n = 30
    bot.stock_data = pd.DataFrame({
        "high_price": [100 + i + 1 for i in range(n)],
        "low_price": [100 + i - 1 for i in range(n)],
        "close_price": [100 + i for i in range(n)],
    })
    pct = bot._atr_pct(period=14)
    assert pct is not None
    assert 0 < pct < 50


def test_dynamic_stop_falls_back_when_no_atr(monkeypatch):
    monkeypatch.setenv("ATR_STOP_MULT", "2.0")
    from src.modules.BinanceTraderBot import BinanceTraderBot
    bot = BinanceTraderBot.__new__(BinanceTraderBot)
    bot.stock_data = None
    bot.stop_loss_percentage = 0.02  # 2%
    assert bot.getDynamicStopPct() == pytest.approx(0.02)


def test_dynamic_stop_respects_floor_and_cap(monkeypatch):
    monkeypatch.setenv("ATR_STOP_MULT", "100.0")  # forcaria stop absurdo
    monkeypatch.setenv("ATR_STOP_FLOOR_PCT", "0.5")
    monkeypatch.setenv("ATR_STOP_CAP_PCT", "4.0")
    from src.modules.BinanceTraderBot import BinanceTraderBot
    bot = BinanceTraderBot.__new__(BinanceTraderBot)
    n = 30
    bot.stock_data = pd.DataFrame({
        "high_price": [100 + i * 2 for i in range(n)],
        "low_price": [100 + i for i in range(n)],
        "close_price": [100 + i * 1.5 for i in range(n)],
    })
    bot.stop_loss_percentage = 0.005
    pct = bot.getDynamicStopPct()
    assert 0.005 <= pct <= 0.04, f"stop fora dos limites: {pct}"

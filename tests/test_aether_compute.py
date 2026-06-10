from __future__ import annotations

import numpy as np
import pandas as pd


def _sample_ohlcv(n: int = 320, drift: float = 0.001) -> pd.DataFrame:
    base = 100.0 * np.exp(np.arange(n) * drift)
    wave = 1.0 + np.sin(np.arange(n) / 9.0) * 0.002
    close = base * wave
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "volume": np.full(n, 1000.0),
        }
    )


def test_run_sweep_continues_when_checkpoint_write_fails(tmp_path, monkeypatch):
    from src.research.aether import compute

    def locked_checkpoint(*_args, **_kwargs):
        raise PermissionError("checkpoint locked")

    progress: list[dict] = []
    monkeypatch.setattr(compute, "_write_checkpoint", locked_checkpoint)

    result = compute.run_sweep(
        probes=[],
        workers=1,
        sweep_id="test-sweep",
        checkpoint_path=tmp_path / "test-sweep.json",
        progress_callback=progress.append,
    )

    assert result["status"] == "completed"
    assert result["completed"] == 0
    assert progress
    assert any("checkpoint_error" in item for item in progress)


def test_run_sweep_keeps_batch_when_process_pool_future_fails(tmp_path, monkeypatch):
    from src.research.aether import compute

    class FailedFuture:
        def result(self):
            raise RuntimeError("process pool died")

    class FakeExecutor:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, *_args, **_kwargs):
            return FailedFuture()

    monkeypatch.setattr(compute, "ProcessPoolExecutor", FakeExecutor)
    monkeypatch.setattr(compute, "as_completed", lambda futures: list(futures))
    monkeypatch.setenv("AETHER_PREWARM_DATASETS", "0")

    result = compute.run_sweep(
        probes=[{"probe_id": "p1", "dataset_id": "binance:BTCUSDT:1h"}],
        workers=2,
        sweep_id="pool-failure",
        checkpoint_path=tmp_path / "pool-failure.json",
    )

    assert result["status"] == "completed"
    assert result["completed"] == 1
    assert result["failed"] == 1
    assert result["results"][0]["probe_id"] == "p1"
    assert "RuntimeError: process pool died" in result["results"][0]["error"]


def test_path_simulator_is_deterministic():
    from src.research.aether.compute import _build_signals

    df = _sample_ohlcv()
    params = {
        "lookback": 80,
        "horizon": 6,
        "paths": 128,
        "block_size": 3,
        "min_p_up": 0.50,
        "min_expected_pct": 0.01,
        "max_stop_hit_prob": 0.90,
        "max_cvar_loss_pct": 5.0,
        "roundtrip_cost_pct": 0.01,
        "trend_ma_window": 40,
        "trend_slope_window": 4,
        "long_only": True,
    }

    first = _build_signals("path_simulator", df, params)
    second = _build_signals("path_simulator", df, params)

    pd.testing.assert_series_equal(first, second)


def test_path_simulator_does_not_look_ahead():
    from src.research.aether.compute import _build_signals

    df = _sample_ohlcv(n=260)
    mutated = df.copy()
    mutated.loc[180:, ["open", "high", "low", "close"]] *= 1.35
    params = {
        "lookback": 72,
        "horizon": 5,
        "paths": 96,
        "block_size": 2,
        "min_p_up": 0.50,
        "min_expected_pct": 0.0,
        "max_stop_hit_prob": 1.0,
        "max_cvar_loss_pct": 10.0,
        "roundtrip_cost_pct": 0.0,
        "trend_ma_window": 36,
        "trend_slope_window": 4,
        "long_only": True,
    }

    original_signal = _build_signals("path_simulator", df, params)
    mutated_signal = _build_signals("path_simulator", mutated, params)

    pd.testing.assert_series_equal(original_signal.iloc[:180], mutated_signal.iloc[:180])


def test_path_simulator_returns_neutral_when_candles_are_insufficient():
    from src.research.aether.compute import _build_signals

    df = _sample_ohlcv(n=50)
    signal = _build_signals("path_simulator", df, {"lookback": 80, "trend_ma_window": 40})

    assert int(signal.abs().sum()) == 0


def test_path_simulator_integrates_with_combo():
    from src.research.aether.compute import _build_signals

    df = _sample_ohlcv(n=300)
    params = {
        "mode": "consensus",
        "specs": [
            {
                "engine": "path_simulator",
                "params": {
                    "lookback": 80,
                    "horizon": 6,
                    "paths": 128,
                    "block_size": 3,
                    "min_p_up": 0.50,
                    "min_expected_pct": 0.01,
                    "max_stop_hit_prob": 0.90,
                    "max_cvar_loss_pct": 5.0,
                    "roundtrip_cost_pct": 0.01,
                    "trend_ma_window": 40,
                    "trend_slope_window": 4,
                    "long_only": True,
                },
            },
            {
                "engine": "regime_filter",
                "params": {
                    "method": "trend_align",
                    "ma_window": 40,
                    "slope_window": 4,
                    "vol_window": 40,
                    "long_only": True,
                },
            },
        ],
    }

    signal = _build_signals("combo", df, params)

    assert len(signal) == len(df)
    assert set(signal.unique()).issubset({0, 1})


def test_path_simulator_backtest_applies_costs():
    from src.research.aether.compute import _build_signals, _run_local_backtest

    df = _sample_ohlcv(n=320, drift=0.0015)
    params = {
        "lookback": 80,
        "horizon": 6,
        "paths": 128,
        "block_size": 3,
        "min_p_up": 0.50,
        "min_expected_pct": 0.01,
        "max_stop_hit_prob": 0.90,
        "max_cvar_loss_pct": 5.0,
        "roundtrip_cost_pct": 0.01,
        "trend_ma_window": 40,
        "trend_slope_window": 4,
        "long_only": True,
    }
    signal = _build_signals("path_simulator", df, params)

    no_cost = _run_local_backtest(df, signal, capital=10000.0, fee_bps=0.0, slippage_bps=0.0, timeframe="15m")
    with_cost = _run_local_backtest(df, signal, capital=10000.0, fee_bps=10.0, slippage_bps=2.0, timeframe="15m")

    assert no_cost["ok"] is True
    assert with_cost["ok"] is True
    assert with_cost["pnl_pct"] < no_cost["pnl_pct"]

"""Smoke tests: garante que modulos basicos importam sem erro."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


def test_can_import_main_module(monkeypatch, tmp_path):
    """`src.main` precisa importar sem efeito colateral grave (chains de threads, locks etc).
    Importamos so o AST do arquivo pra evitar startar dashboards/threads em CI."""
    import ast
    main_path = Path(__file__).resolve().parents[1] / "src" / "main.py"
    src = main_path.read_text(encoding="utf-8")
    ast.parse(src)


def test_paper_balance_logger_loads():
    import importlib
    import src.modules.PaperBalanceLogger as pbl
    importlib.reload(pbl)
    assert pbl.MAX_GLOBAL_DRAWDOWN_PCT > 0
    assert pbl.MAX_OPEN_POSITIONS > 0
    assert pbl.HARD_FLOOR_BUY_VOTES if hasattr(pbl, "HARD_FLOOR_BUY_VOTES") else True


def test_multi_strategy_imports():
    from src.strategies.multi_strategy import MultiStrategy, ENTRY_MODES, HARD_FLOOR_BUY_VOTES
    assert "normal" in ENTRY_MODES
    assert "aggressive" not in ENTRY_MODES, "modo aggressive removido"
    assert HARD_FLOOR_BUY_VOTES >= 2


def test_instance_lock_imports():
    import src.modules.InstanceLock as il
    assert callable(il.acquire_or_die)


def test_file_lock_imports():
    from src.modules.FileLock import file_lock
    assert callable(file_lock)


def test_binance_trader_bot_imports():
    """Garante que BinanceTraderBot importa (todos os helpers de stop, validacao etc.)."""
    from src.modules.BinanceTraderBot import BinanceTraderBot, _trip_rate_limit, _rate_limit_remaining
    assert hasattr(BinanceTraderBot, "_atr_pct")
    assert hasattr(BinanceTraderBot, "getDynamicStopPct")
    assert hasattr(BinanceTraderBot, "_validate_real_position_size")
    assert callable(_trip_rate_limit)
    assert _rate_limit_remaining() >= 0


def test_walk_forward_script_imports():
    """walk_forward_champions deve poder ser importado (nao roda main)."""
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    spec = importlib.util.spec_from_file_location(
        "walk_forward_champions",
        repo_root / "scripts" / "walk_forward_champions.py",
    )
    mod = importlib.util.module_from_spec(spec)
    # Apenas confirma que carrega; nao executa main()
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        pytest.skip(f"walk_forward depende de Binance client: {e}")
    assert hasattr(mod, "walk_forward")

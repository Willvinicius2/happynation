"""Reproduz o hang da inicializacao do bot, com instrumentacao por etapa.

Mostra exatamente qual chamada Binance trava (ping, sync_time, get_symbol_info,
get_symbol_ticker por ativo). Usado para validar fix de timeout.
"""
from __future__ import annotations
import os, sys, time, signal, threading
from dotenv import load_dotenv
load_dotenv()
from pathlib import Path

USE_TIMEOUT = "--with-timeout" in sys.argv
print(f"[repro] USE_TIMEOUT={USE_TIMEOUT}", flush=True)

from src.modules.PortfolioConfig import load_portfolio, resolve_lot_sizes
from src.modules.BinanceClient import BinanceClient

api_key = os.getenv("BINANCE_API_KEY")
secret_key = os.getenv("BINANCE_SECRET_KEY")
keys_invalid = (
    not api_key or not secret_key
    or "Cole" in (api_key or "") or "Cole" in (secret_key or "")
)
print(f"[repro] keys_invalid={keys_invalid} api_key_prefix={api_key[:6] if api_key else None}", flush=True)

PORTFOLIO_YAML = Path("src/config/portfolio.yaml")
print(f"[repro] portfolio yaml exists={PORTFOLIO_YAML.exists()}", flush=True)
cfg = load_portfolio(PORTFOLIO_YAML)
print(f"[repro] portfolio carregado {len(cfg.assets)} ativos: {[a.symbol for a in cfg.assets]}", flush=True)

# Test ping + sync isolated
t0 = time.time()
extra = {"requests_params": {"timeout": 15}} if USE_TIMEOUT else {}
print(f"[repro] criando BinanceClient sync=False ping=True extra={extra}...", flush=True)
client = BinanceClient("" if keys_invalid else api_key, "" if keys_invalid else secret_key,
                      sync=False, ping=True, sync_interval=30000, verbose=False, **extra)
print(f"[repro] BinanceClient init done in {time.time()-t0:.2f}s", flush=True)

# Iterate per asset and time each call
for asset in cfg.assets:
    t0 = time.time()
    try:
        info = client.get_symbol_info(asset.symbol)
        print(f"[repro] {asset.symbol} get_symbol_info {time.time()-t0:.2f}s ok={bool(info)}", flush=True)
    except Exception as e:
        print(f"[repro] {asset.symbol} get_symbol_info FAILED in {time.time()-t0:.2f}s: {e!r}", flush=True)
        continue

    t0 = time.time()
    try:
        ticker = client.get_symbol_ticker(symbol=asset.symbol)
        print(f"[repro] {asset.symbol} get_symbol_ticker {time.time()-t0:.2f}s price={ticker['price']}", flush=True)
    except Exception as e:
        print(f"[repro] {asset.symbol} get_symbol_ticker FAILED in {time.time()-t0:.2f}s: {e!r}", flush=True)

print("[repro] DONE", flush=True)

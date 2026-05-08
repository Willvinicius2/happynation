"""Smoke test do MarketDataHub: backfill REST + stream WebSocket multiplex.

Conecta na Binance (publico, sem API key), faz backfill de 6 TFs em 3 ativos,
abre stream e mostra updates em tempo real por ~30s. Reporta saude no final.

Uso:
    python -u scripts/smoke_market_data_hub.py
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

# Garante import do projeto
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from src.modules.BinanceClient import BinanceClient
from src.modules.MarketDataHub import MarketDataHub


def main():
    print("[SMOKE] subindo cliente Binance publico...", flush=True)
    client = BinanceClient("", "", sync=False, ping=True, sync_interval=30000, verbose=False)

    db_path = Path("src/logs/market_data_smoke.db")
    print(f"[SMOKE] criando hub (db={db_path})...", flush=True)
    hub = MarketDataHub(client=client, db_path=db_path, buffer_size=1000)

    # Subset reduzido pra smoke
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    tfs = ["1m", "5m", "15m", "1h", "4h", "1d"]

    closed_events: list[tuple[str, str]] = []

    def on_close(symbol, tf, df):
        closed_events.append((symbol, tf))
        last = df.iloc[-1]
        print(f"[CLOSE] {symbol}@{tf} close={last['close_price']:.4f} candles={len(df)}", flush=True)

    hub.on_candle_close(on_close)

    print(f"[SMOKE] subscribe {len(symbols)} ativos x {len(tfs)} TFs = {len(symbols) * len(tfs)} streams...", flush=True)
    for s in symbols:
        hub.subscribe(s, tfs)

    print("[SMOKE] start hub...", flush=True)
    hub.start()

    # Mostra snapshot dos buffers
    print("\n[SMOKE] backfill snapshot:", flush=True)
    for s in symbols:
        for tf in tfs:
            df = hub.get_dataframe(s, tf, include_live=True)
            n = len(df) if df is not None else 0
            last_p = float(df["close_price"].iloc[-1]) if df is not None and not df.empty else None
            print(f"  {s}@{tf}: {n} candles, last close={last_p}", flush=True)

    # Roda stream por 30s
    print("\n[SMOKE] streaming por 30s...", flush=True)
    end = time.time() + 30
    while time.time() < end:
        time.sleep(2)
        h = hub.stream_health()
        print(f"  [HEALTH] active={h['active_buffers']} stale={len(h['stale_buffers'])} streams={h['total_streams']}", flush=True)

    print(f"\n[SMOKE] eventos de candle fechado: {len(closed_events)}", flush=True)
    print(f"[SMOKE] health final: {hub.stream_health()}", flush=True)

    # Verifica SQLite
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("SELECT symbol, tf, COUNT(*) FROM klines GROUP BY symbol, tf ORDER BY symbol, tf")
        print("\n[SMOKE] persist SQLite:", flush=True)
        for row in cur.fetchall():
            print(f"  {row[0]:10s} {row[1]:5s} -> {row[2]} candles", flush=True)

    hub.stop()
    print("\n[SMOKE] OK", flush=True)


if __name__ == "__main__":
    main()

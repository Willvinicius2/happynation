"""MarketDataHub: camada unica de dados live multi-timeframe.

Mantem buffers em RAM por (symbol, tf) alimentados por:
  - backfill REST inicial (1000 candles por par/TF);
  - stream WebSocket multiplexado (Binance kline streams);

Ao fechar um candle, persiste em SQLite e dispara callbacks registrados.

Por que existe:
  - elimina polling REST por thread (reduz 60s -> ~1s de latencia);
  - permite estrategias rodarem cada uma no seu TF nativo (1m..1d);
  - garante paridade backtest<->live (mesmas candles, mesmo timestamps).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException


logger = logging.getLogger(__name__)


# Mapeamento canonico de timeframes -> intervalo Binance
TF_TO_INTERVAL = {
    "1m": Client.KLINE_INTERVAL_1MINUTE,
    "3m": Client.KLINE_INTERVAL_3MINUTE,
    "5m": Client.KLINE_INTERVAL_5MINUTE,
    "15m": Client.KLINE_INTERVAL_15MINUTE,
    "30m": Client.KLINE_INTERVAL_30MINUTE,
    "1h": Client.KLINE_INTERVAL_1HOUR,
    "2h": Client.KLINE_INTERVAL_2HOUR,
    "4h": Client.KLINE_INTERVAL_4HOUR,
    "6h": Client.KLINE_INTERVAL_6HOUR,
    "8h": Client.KLINE_INTERVAL_8HOUR,
    "12h": Client.KLINE_INTERVAL_12HOUR,
    "1d": Client.KLINE_INTERVAL_1DAY,
    "3d": Client.KLINE_INTERVAL_3DAY,
    "1w": Client.KLINE_INTERVAL_1WEEK,
}

# Inverso (interval string Binance -> tf canonico)
INTERVAL_TO_TF = {v: k for k, v in TF_TO_INTERVAL.items()}

# Quantos candles manter em RAM por (symbol, tf)
DEFAULT_BUFFER_SIZE = 1000

# Timeout para considerar stream travado (sem update no candle vivo)
STREAM_STALE_SECONDS = 120


@dataclass
class CandleBuffer:
    """Buffer rolling de candles para um par (symbol, tf)."""

    symbol: str
    tf: str
    capacity: int = DEFAULT_BUFFER_SIZE
    df: pd.DataFrame = field(default_factory=lambda: _empty_kline_df())
    last_close_time_ms: int = 0  # ultimo candle fechado conhecido
    last_update_at: float = 0.0  # ultimo update do stream (epoch s)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def append_closed(self, candle: dict[str, Any]) -> bool:
        """Adiciona um candle ja fechado. Retorna True se foi novo (idempotente por open_time)."""
        with self.lock:
            ot = int(candle["open_time_ms"])
            if not self.df.empty:
                last_ot_ms = int(self.df["open_time_ms"].iloc[-1])
                if ot <= last_ot_ms:
                    return False
            row = {
                "open_time": pd.to_datetime(ot, unit="ms", utc=True),
                "open_time_ms": ot,
                "open_price": float(candle["open"]),
                "high_price": float(candle["high"]),
                "low_price": float(candle["low"]),
                "close_price": float(candle["close"]),
                "volume": float(candle["volume"]),
                "is_closed": True,
            }
            self.df = pd.concat([self.df, pd.DataFrame([row])], ignore_index=True)
            if len(self.df) > self.capacity:
                self.df = self.df.iloc[-self.capacity:].reset_index(drop=True)
            self.last_close_time_ms = ot
            self.last_update_at = time.time()
            return True

    def update_live(self, candle: dict[str, Any]) -> None:
        """Atualiza o candle vivo (in-progress) na ultima posicao."""
        with self.lock:
            ot = int(candle["open_time_ms"])
            row = {
                "open_time": pd.to_datetime(ot, unit="ms", utc=True),
                "open_time_ms": ot,
                "open_price": float(candle["open"]),
                "high_price": float(candle["high"]),
                "low_price": float(candle["low"]),
                "close_price": float(candle["close"]),
                "volume": float(candle["volume"]),
                "is_closed": False,
            }
            if self.df.empty:
                self.df = pd.DataFrame([row])
                self.last_update_at = time.time()
                return
            last_ot = int(self.df["open_time_ms"].iloc[-1])
            if ot == last_ot:
                # mesmo candle, atualiza in-place
                for k, v in row.items():
                    self.df.at[self.df.index[-1], k] = v
            elif ot > last_ot:
                # novo candle vivo (anterior ja deveria ter sido fechado por append_closed)
                self.df = pd.concat([self.df, pd.DataFrame([row])], ignore_index=True)
                if len(self.df) > self.capacity:
                    self.df = self.df.iloc[-self.capacity:].reset_index(drop=True)
            # ot < last_ot: update tardio, ignora
            self.last_update_at = time.time()

    def get_df(self, include_live: bool = True) -> pd.DataFrame:
        """Retorna copia do dataframe. include_live=False filtra o candle vivo."""
        with self.lock:
            if include_live:
                return self.df.copy(deep=True)
            return self.df[self.df["is_closed"]].copy(deep=True).reset_index(drop=True)

    def latest_price(self) -> float | None:
        with self.lock:
            if self.df.empty:
                return None
            return float(self.df["close_price"].iloc[-1])


def _empty_kline_df() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "open_time", "open_time_ms",
        "open_price", "high_price", "low_price", "close_price", "volume",
        "is_closed",
    ])


# Tipo do callback: (symbol, tf, df_closed_only)
CandleCloseCallback = Callable[[str, str, pd.DataFrame], None]


class MarketDataHub:
    """Hub central de candles multi-timeframe."""

    def __init__(
        self,
        client: Client,
        api_key: str | None = None,
        api_secret: str | None = None,
        db_path: Path | None = None,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        testnet: bool = False,
    ) -> None:
        self.client = client
        self._api_key = api_key
        self._api_secret = api_secret
        self.buffer_size = buffer_size
        self.testnet = testnet

        # buffers[symbol][tf] -> CandleBuffer
        self._buffers: dict[str, dict[str, CandleBuffer]] = defaultdict(dict)
        self._buffers_lock = threading.RLock()

        # callbacks por (symbol, tf) e globais
        self._callbacks_per_tf: dict[tuple[str, str], list[CandleCloseCallback]] = defaultdict(list)
        self._callbacks_global: list[CandleCloseCallback] = []
        self._callbacks_lock = threading.RLock()

        # stream state
        self._twm = None  # ThreadedWebsocketManager
        self._socket_key = None
        self._streams: list[str] = []
        self._started = False
        self._start_lock = threading.RLock()
        self._reconnect_thread: threading.Thread | None = None

        # SQLite
        self._db_path = db_path
        self._db_lock = threading.RLock()
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_sqlite()

    # ------------------------------------------------------------
    # API publica

    def subscribe(self, symbol: str, tfs: list[str]) -> None:
        """Registra (symbol, tfs) para backfill+stream. Idempotente.

        Se chamado depois de start(), forca reinicializacao do socket.
        """
        symbol_u = symbol.upper()
        new_pairs: list[tuple[str, str]] = []
        with self._buffers_lock:
            for tf in tfs:
                tf_l = tf.lower()
                if tf_l not in TF_TO_INTERVAL:
                    logger.warning(f"[HUB] TF '{tf}' nao suportado, ignorando ({symbol_u})")
                    continue
                if tf_l not in self._buffers[symbol_u]:
                    self._buffers[symbol_u][tf_l] = CandleBuffer(
                        symbol=symbol_u, tf=tf_l, capacity=self.buffer_size,
                    )
                    new_pairs.append((symbol_u, tf_l))
        if not new_pairs:
            return
        # Backfill imediato (REST) — bloqueante so para os novos pares
        for sym, tf in new_pairs:
            try:
                self._backfill(sym, tf)
            except Exception as exc:
                logger.error(f"[HUB] backfill {sym}@{tf} falhou: {exc}", exc_info=True)
        # Reconfigura streams se ja estiver rodando
        with self._start_lock:
            if self._started:
                self._restart_socket()

    def get_dataframe(
        self, symbol: str, tf: str, include_live: bool = True,
    ) -> pd.DataFrame | None:
        symbol_u = symbol.upper()
        tf_l = tf.lower()
        buf = self._buffers.get(symbol_u, {}).get(tf_l)
        if buf is None:
            return None
        df = buf.get_df(include_live=include_live)
        return df if not df.empty else None

    def get_dataframes(
        self, symbol: str, include_live: bool = True,
    ) -> dict[str, pd.DataFrame]:
        symbol_u = symbol.upper()
        out: dict[str, pd.DataFrame] = {}
        for tf, buf in self._buffers.get(symbol_u, {}).items():
            df = buf.get_df(include_live=include_live)
            if not df.empty:
                out[tf] = df
        return out

    def latest_price(self, symbol: str, tf: str | None = None) -> float | None:
        """Ultimo preco conhecido. Se tf=None, pega o TF mais rapido disponivel."""
        symbol_u = symbol.upper()
        if tf is not None:
            buf = self._buffers.get(symbol_u, {}).get(tf.lower())
            return buf.latest_price() if buf else None
        # Tf mais rapido (menor minutos)
        bufs = self._buffers.get(symbol_u, {})
        if not bufs:
            return None
        from src.strategies.multi_strategy import _tf_minutes
        sorted_tfs = sorted(bufs.keys(), key=lambda t: _tf_minutes(t) or 999999)
        for tf_name in sorted_tfs:
            p = bufs[tf_name].latest_price()
            if p is not None:
                return p
        return None

    def on_candle_close(
        self,
        callback: CandleCloseCallback,
        symbol: str | None = None,
        tf: str | None = None,
    ) -> None:
        """Registra callback. symbol/tf=None = callback global."""
        with self._callbacks_lock:
            if symbol is None or tf is None:
                self._callbacks_global.append(callback)
            else:
                self._callbacks_per_tf[(symbol.upper(), tf.lower())].append(callback)

    def stream_health(self) -> dict[str, Any]:
        """Diagnostico: quantos buffers vivos, quanto ha sem update, etc."""
        now = time.time()
        stale: list[tuple[str, str, float]] = []
        active = 0
        empty = 0
        with self._buffers_lock:
            for sym, by_tf in self._buffers.items():
                for tf, buf in by_tf.items():
                    if buf.df.empty:
                        empty += 1
                        continue
                    age = now - buf.last_update_at if buf.last_update_at else None
                    if age is not None and age > STREAM_STALE_SECONDS:
                        stale.append((sym, tf, age))
                    else:
                        active += 1
        return {
            "started": self._started,
            "active_buffers": active,
            "empty_buffers": empty,
            "stale_buffers": stale,
            "total_streams": len(self._streams),
        }

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._restart_socket()
            self._started = True
            logger.info(f"[HUB] iniciado com {len(self._streams)} streams")

    def stop(self) -> None:
        with self._start_lock:
            self._started = False
            try:
                if self._twm is not None:
                    self._twm.stop()
            except Exception as exc:
                logger.warning(f"[HUB] erro ao parar TWM: {exc}")
            self._twm = None
            self._socket_key = None

    # ------------------------------------------------------------
    # Backfill REST

    def _backfill(self, symbol: str, tf: str) -> None:
        interval = TF_TO_INTERVAL[tf]
        buf = self._buffers[symbol][tf]
        # Tenta com retry simples
        last_exc = None
        for attempt in range(3):
            try:
                klines = self.client.get_klines(
                    symbol=symbol, interval=interval, limit=self.buffer_size,
                )
                break
            except BinanceAPIException as exc:
                last_exc = exc
                code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                if code in (429, 418, -1003):
                    backoff = 60 * (attempt + 1)
                    logger.warning(f"[HUB] rate limit no backfill {symbol}@{tf}, dorme {backoff}s")
                    time.sleep(backoff)
                else:
                    time.sleep(2 ** attempt)
            except Exception as exc:
                last_exc = exc
                time.sleep(2 ** attempt)
        else:
            logger.error(f"[HUB] backfill {symbol}@{tf} falhou: {last_exc}")
            return

        if not klines:
            return

        rows = []
        for k in klines:
            ot = int(k[0])
            rows.append({
                "open_time": pd.to_datetime(ot, unit="ms", utc=True),
                "open_time_ms": ot,
                "open_price": float(k[1]),
                "high_price": float(k[2]),
                "low_price": float(k[3]),
                "close_price": float(k[4]),
                "volume": float(k[5]),
                "is_closed": True,  # Klines historicos sempre fechados
            })
        with buf.lock:
            buf.df = pd.DataFrame(rows)
            buf.last_close_time_ms = rows[-1]["open_time_ms"]
            buf.last_update_at = time.time()
        # Persiste todos no SQLite
        if self._db_path is not None:
            self._persist_batch(symbol, tf, rows)
        logger.info(f"[HUB] backfill {symbol}@{tf}: {len(rows)} candles")

    # ------------------------------------------------------------
    # WebSocket

    def _build_streams(self) -> list[str]:
        streams = []
        with self._buffers_lock:
            for sym, by_tf in self._buffers.items():
                for tf in by_tf:
                    streams.append(f"{sym.lower()}@kline_{tf}")
        return streams

    def _restart_socket(self) -> None:
        """Para socket atual (se houver) e abre um novo com todos os streams."""
        try:
            from binance import ThreadedWebsocketManager
        except ImportError:
            logger.error("[HUB] python-binance ThreadedWebsocketManager indisponivel")
            return

        self._streams = self._build_streams()
        if not self._streams:
            logger.warning("[HUB] sem streams para abrir")
            return

        # Para socket anterior
        if self._twm is not None:
            try:
                self._twm.stop()
            except Exception:
                pass
            self._twm = None
            self._socket_key = None

        self._twm = ThreadedWebsocketManager(
            api_key=self._api_key or "",
            api_secret=self._api_secret or "",
            testnet=self.testnet,
        )
        self._twm.daemon = True
        self._twm.start()
        # Multiplex socket: recebe mensagem com prefixo "stream"
        self._socket_key = self._twm.start_multiplex_socket(
            callback=self._on_ws_message,
            streams=self._streams,
        )
        logger.info(f"[HUB] WS aberto com {len(self._streams)} streams")
        # Watcher de stale streams
        if self._reconnect_thread is None or not self._reconnect_thread.is_alive():
            self._reconnect_thread = threading.Thread(
                target=self._stale_watcher, daemon=True,
            )
            self._reconnect_thread.start()

    def _on_ws_message(self, msg: dict[str, Any]) -> None:
        try:
            # Multiplex format: {"stream": "btcusdt@kline_1m", "data": {...}}
            data = msg.get("data") or msg
            ev = data.get("e") or data.get("event")
            if ev != "kline":
                return
            k = data.get("k") or {}
            symbol = (data.get("s") or k.get("s") or "").upper()
            interval = k.get("i")
            tf = INTERVAL_TO_TF.get(interval)
            if not symbol or not tf:
                return
            buf = self._buffers.get(symbol, {}).get(tf)
            if buf is None:
                return
            candle = {
                "open_time_ms": int(k["t"]),
                "open": k["o"],
                "high": k["h"],
                "low": k["l"],
                "close": k["c"],
                "volume": k["v"],
            }
            is_closed = bool(k.get("x", False))
            if is_closed:
                appended = buf.append_closed(candle)
                if appended:
                    if self._db_path is not None:
                        self._persist_batch(symbol, tf, [{
                            "open_time_ms": candle["open_time_ms"],
                            "open_price": float(candle["open"]),
                            "high_price": float(candle["high"]),
                            "low_price": float(candle["low"]),
                            "close_price": float(candle["close"]),
                            "volume": float(candle["volume"]),
                        }])
                    self._fire_callbacks(symbol, tf)
            else:
                buf.update_live(candle)
        except Exception as exc:
            logger.error(f"[HUB] erro processando msg WS: {exc}", exc_info=True)

    def _fire_callbacks(self, symbol: str, tf: str) -> None:
        """Dispara callbacks fora do lock pra nao bloquear o WS thread."""
        with self._callbacks_lock:
            cbs_specific = list(self._callbacks_per_tf.get((symbol, tf), []))
            cbs_global = list(self._callbacks_global)
        if not cbs_specific and not cbs_global:
            return
        df_closed = self.get_dataframe(symbol, tf, include_live=False)
        if df_closed is None:
            return
        for cb in cbs_specific + cbs_global:
            try:
                cb(symbol, tf, df_closed)
            except Exception as exc:
                logger.error(f"[HUB] callback {cb.__name__} falhou: {exc}", exc_info=True)

    def _stale_watcher(self) -> None:
        """Detecta streams travados e refaz socket. Roda em thread daemon."""
        while True:
            time.sleep(60)
            if not self._started:
                return
            now = time.time()
            stale_count = 0
            with self._buffers_lock:
                for sym, by_tf in self._buffers.items():
                    for tf, buf in by_tf.items():
                        if not buf.last_update_at:
                            continue
                        age = now - buf.last_update_at
                        if age > STREAM_STALE_SECONDS:
                            stale_count += 1
            if stale_count >= max(3, len(self._streams) // 5):
                logger.warning(f"[HUB] {stale_count} streams stale, reconectando WS")
                with self._start_lock:
                    if self._started:
                        try:
                            self._restart_socket()
                        except Exception as exc:
                            logger.error(f"[HUB] reconnect falhou: {exc}", exc_info=True)

    # ------------------------------------------------------------
    # SQLite persistence

    def _init_sqlite(self) -> None:
        with self._db_lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS klines (
                        symbol TEXT NOT NULL,
                        tf TEXT NOT NULL,
                        open_time_ms INTEGER NOT NULL,
                        open_price REAL NOT NULL,
                        high_price REAL NOT NULL,
                        low_price REAL NOT NULL,
                        close_price REAL NOT NULL,
                        volume REAL NOT NULL,
                        PRIMARY KEY(symbol, tf, open_time_ms)
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_klines_symbol_tf_time
                    ON klines(symbol, tf, open_time_ms)
                """)
                conn.commit()

    def _persist_batch(self, symbol: str, tf: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        try:
            with self._db_lock:
                with sqlite3.connect(self._db_path) as conn:
                    conn.executemany(
                        """INSERT OR IGNORE INTO klines
                           (symbol, tf, open_time_ms, open_price, high_price, low_price, close_price, volume)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        [
                            (
                                symbol, tf,
                                int(r["open_time_ms"]),
                                float(r["open_price"]),
                                float(r["high_price"]),
                                float(r["low_price"]),
                                float(r["close_price"]),
                                float(r["volume"]),
                            )
                            for r in rows
                        ],
                    )
                    conn.commit()
        except Exception as exc:
            logger.warning(f"[HUB] persist {symbol}@{tf} falhou: {exc}")


# ----------------------------------------------------------------
# Singleton acessivel globalmente

_GLOBAL_HUB: MarketDataHub | None = None
_GLOBAL_HUB_LOCK = threading.Lock()


def get_hub() -> MarketDataHub | None:
    return _GLOBAL_HUB


def set_hub(hub: MarketDataHub) -> None:
    global _GLOBAL_HUB
    with _GLOBAL_HUB_LOCK:
        _GLOBAL_HUB = hub

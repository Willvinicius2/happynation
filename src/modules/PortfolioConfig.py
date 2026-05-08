"""Carrega configuracao de portfolio multi-ativo do YAML.

Resolve `traded_quantity` automaticamente quando declarado como `auto`,
usando exchangeInfo (lotSize) e o `notional_usd` alvo declarado por ativo.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from binance.client import Client


logger = logging.getLogger(__name__)


DEFAULT_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]
DEFAULT_NOTIONAL_USD = 100.0
DEFAULT_BUFFER_SIZE = 1000


@dataclass
class AssetConfig:
    symbol: str
    base: str
    quote: str
    timeframes: list[str] = field(default_factory=lambda: list(DEFAULT_TIMEFRAMES))
    notional_usd: float = DEFAULT_NOTIONAL_USD
    traded_quantity: float = 0.0  # resolvido em runtime via exchangeInfo
    step_size: float = 0.0
    tick_size: float = 0.0
    min_notional: float = 0.0


@dataclass
class PortfolioConfig:
    assets: list[AssetConfig] = field(default_factory=list)
    default_timeframes: list[str] = field(default_factory=lambda: list(DEFAULT_TIMEFRAMES))
    default_notional_usd: float = DEFAULT_NOTIONAL_USD
    buffer_size: int = DEFAULT_BUFFER_SIZE

    def all_timeframes(self) -> set[str]:
        out: set[str] = set()
        for a in self.assets:
            out.update(a.timeframes)
        return out


def _split_symbol(symbol: str) -> tuple[str, str]:
    """Split BTCUSDT -> (BTC, USDT). Tenta quotes comuns."""
    sym = symbol.upper().strip()
    for quote in ("USDT", "BUSD", "FDUSD", "USDC", "BTC", "ETH", "BNB"):
        if sym.endswith(quote):
            base = sym[: -len(quote)]
            if base:
                return base, quote
    raise ValueError(f"nao consigo separar base/quote de {symbol!r}")


def load_portfolio(yaml_path: Path | str) -> PortfolioConfig:
    """Carrega config sem resolver lotSize. Para resolver, chame resolve_lot_sizes()."""
    path = Path(yaml_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    g = raw.get("global", {}) or {}
    cfg = PortfolioConfig(
        default_timeframes=list(g.get("default_timeframes") or DEFAULT_TIMEFRAMES),
        default_notional_usd=float(g.get("default_notional_usd") or DEFAULT_NOTIONAL_USD),
        buffer_size=int(g.get("buffer_size") or DEFAULT_BUFFER_SIZE),
    )
    for entry in raw.get("assets", []) or []:
        symbol = str(entry["symbol"]).upper()
        base, quote = _split_symbol(symbol)
        tfs = entry.get("timeframes") or cfg.default_timeframes
        notional = float(entry.get("notional_usd", cfg.default_notional_usd))
        # `traded_quantity` opcional explicito (override de auto-resolve)
        qty_raw = entry.get("traded_quantity", "auto")
        qty = 0.0 if qty_raw == "auto" else float(qty_raw)
        cfg.assets.append(AssetConfig(
            symbol=symbol,
            base=base,
            quote=quote,
            timeframes=[t.lower() for t in tfs],
            notional_usd=notional,
            traded_quantity=qty,
        ))
    return cfg


def resolve_lot_sizes(cfg: PortfolioConfig, client: Client) -> None:
    """Para cada ativo: busca exchangeInfo, extrai stepSize/tickSize/minNotional
    e calcula traded_quantity quando ainda nao foi setado.

    Idempotente: se ja resolvido (step_size > 0), nao refaz.
    """
    for asset in cfg.assets:
        if asset.step_size > 0 and asset.traded_quantity > 0:
            continue
        try:
            info = client.get_symbol_info(asset.symbol)
        except Exception as exc:
            logger.warning(f"[PORTFOLIO] {asset.symbol}: get_symbol_info falhou: {exc}")
            continue
        if not info:
            logger.warning(f"[PORTFOLIO] {asset.symbol}: symbol nao encontrado")
            continue
        for f in info.get("filters", []):
            ft = f.get("filterType")
            if ft == "LOT_SIZE":
                asset.step_size = float(f["stepSize"])
            elif ft == "PRICE_FILTER":
                asset.tick_size = float(f["tickSize"])
            elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
                asset.min_notional = float(f.get("minNotional") or f.get("notional") or 0)
        # Estima qty a partir de notional + preco corrente
        if asset.traded_quantity <= 0:
            try:
                ticker = client.get_symbol_ticker(symbol=asset.symbol)
                price = float(ticker["price"])
            except Exception as exc:
                logger.warning(f"[PORTFOLIO] {asset.symbol}: ticker falhou: {exc}")
                continue
            target_usd = max(asset.notional_usd, asset.min_notional or 0.0)
            raw_qty = target_usd / price if price > 0 else 0.0
            # Arredonda para baixo no stepSize
            qty = _floor_to_step(raw_qty, asset.step_size) if asset.step_size > 0 else raw_qty
            asset.traded_quantity = qty
            logger.info(
                f"[PORTFOLIO] {asset.symbol}: notional ${target_usd:.2f} @ {price:.4f} -> qty {qty}"
            )


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    n = math.floor(value / step)
    out = n * step
    # Corrige floats: arredonda casas decimais para o step
    decimals = max(0, _decimals(step))
    return round(out, decimals)


def _decimals(step: float) -> int:
    s = f"{step:.10f}".rstrip("0")
    if "." in s:
        return len(s.split(".")[1])
    return 0

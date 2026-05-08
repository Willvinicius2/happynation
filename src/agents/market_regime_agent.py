from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Iterable

from src.agents.base import AgentFinding, AgentSeverity
from src.agents.strategy_catalog_state import load_json
from src.research.aether.compute import load_dataset_fast


def _classify_regime(closes: list[float]) -> tuple[str, dict[str, float]] | None:
    if len(closes) < 30:
        return None
    last = closes[-1]
    first = closes[0]
    if first <= 0:
        return None

    change_pct = (last - first) / first * 100
    returns = []
    for prev, curr in zip(closes, closes[1:]):
        if prev > 0:
            returns.append((curr - prev) / prev)
    if len(returns) < 5:
        return None
    vol_pct = statistics.pstdev(returns) * 100
    metrics = {"change_pct": change_pct, "vol_pct": vol_pct}

    if change_pct >= 5:
        regime = "bull"
    elif change_pct <= -5:
        regime = "bear"
    elif vol_pct >= 3.0:
        regime = "vol_alta"
    elif vol_pct <= 0.6:
        regime = "vol_baixa"
    else:
        regime = "lateral"
    return regime, metrics


class MarketRegimeAgent:
    name = "market_regime_agent"
    display_name = "Meteorologista"

    def __init__(
        self,
        active_path: Path | str = "src/logs/research/active_strategies.json",
        candles_dir: Path | str | None = None,
        candle_lookback: int = 200,
    ) -> None:
        self.active_path = Path(active_path)
        self.candles_dir = Path(candles_dir) if candles_dir is not None else None
        self.candle_lookback = candle_lookback

    def run(self) -> list[AgentFinding]:
        if self.candles_dir is not None and not self.candles_dir.exists():
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Sem cache de candles",
                    message=f"Nao encontrei {self.candles_dir}.",
                    action="Gerar ou apontar o cache de candles antes de validar regime por arquivo.",
                    metadata={"candles_dir": str(self.candles_dir)},
                )
            ]

        active = load_json(self.active_path, {"strategies": []})
        active_entries = self._extract_active(active)

        findings: list[AgentFinding] = []
        any_classified = False

        for entry in active_entries:
            asset = entry.get("asset") or entry.get("symbol")
            timeframe = entry.get("timeframe", "1h")
            if not isinstance(asset, str):
                continue
            classification = self._classify_asset(asset, timeframe)
            if classification is None:
                continue
            any_classified = True
            current_regime, metrics = classification
            validated_regime = entry.get("validated_regime") if isinstance(entry, dict) else None
            if isinstance(validated_regime, str) and validated_regime != current_regime:
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.WARNING,
                        title="Regime atual diverge do validado",
                        message=(
                            f"{asset} validado em {validated_regime!r} mas mercado atual e {current_regime!r}."
                        ),
                        asset=asset,
                        action="Revalidar estrategia no regime atual antes de continuar.",
                        metadata={
                            "asset": asset,
                            "current_regime": current_regime,
                            "validated_regime": validated_regime,
                            **metrics,
                        },
                    )
                )
            else:
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.INFO,
                        title="Regime classificado",
                        message=f"{asset} esta em regime {current_regime!r}.",
                        asset=asset,
                        metadata={"asset": asset, "current_regime": current_regime, **metrics},
                    )
                )

        if not any_classified and not findings:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Sem ativos para classificar",
                    message="Nenhum ativo ativo com cache de candles disponivel.",
                )
            )
        return findings

    def _extract_active(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            entries = payload.get("strategies", [])
        else:
            entries = payload
        if not isinstance(entries, Iterable):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    def _classify_asset(self, asset: str, timeframe: str) -> tuple[str, dict[str, float]] | None:
        if self.candles_dir is not None:
            return self._classify_asset_from_cache(asset, timeframe)
        try:
            dataset_id = f"binance:{asset}:{timeframe}"
            df = load_dataset_fast(dataset_id)
            if df is None or df.empty:
                return None
            
            closes = df["close"].dropna().tolist()
            if len(closes) > self.candle_lookback:
                closes = closes[-self.candle_lookback :]
            
            return _classify_regime(closes)
        except Exception:
            return None

    def _classify_asset_from_cache(self, asset: str, timeframe: str) -> tuple[str, dict[str, float]] | None:
        if self.candles_dir is None:
            return None
        candidates = (
            self.candles_dir / f"{asset.upper()}_{timeframe}.json",
            self.candles_dir / f"{asset.upper()}.json",
            self.candles_dir / f"{asset}.json",
        )
        path = next((candidate for candidate in candidates if candidate.exists()), None)
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        candles = payload.get("candles") if isinstance(payload, dict) else payload
        if not isinstance(candles, list):
            return None
        closes: list[float] = []
        for candle in candles:
            raw_close = candle.get("close") if isinstance(candle, dict) else candle
            try:
                closes.append(float(raw_close))
            except (TypeError, ValueError):
                continue
        if len(closes) > self.candle_lookback:
            closes = closes[-self.candle_lookback :]
        return _classify_regime(closes)

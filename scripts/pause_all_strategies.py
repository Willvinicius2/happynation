"""Pausa TODAS as estrategias votantes (manifest Aether + cesta legada).

Usado apos validacao 1A.5 confirmar que nem manifest nem Champions tem
alpha em walk-forward 2 anos.

Mecanismo: edita src/logs/paper_balance/state.json adicionando entradas
em _meta.paused_strategies. O MultiStrategy ja consulta esse dict e
pula qualquer estrategia listada (multi_strategy.py:217).

Reversivel: rode `python scripts/pause_all_strategies.py --unpause`
para limpar.

Saida:
  src/logs/agents/strategies_paused.json (relatorio do que foi pausado)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.modules import PaperBalanceLogger as pbl  # noqa: E402

MANIFEST_PATH = ROOT / "src" / "logs" / "research" / "active_strategies.json"

# Nomes da cesta legada (multi_strategy.py:60-110)
LEGACY_STRATEGIES = [
    "Champions",
    "RSI",
    "MovingAverage",
    "MA_RSI_Volume",
    "Vortex",
    "ATR",
    "TEMA",
    "HullMA",
    "HighWinRateScalp",
]

PAUSE_REASON = (
    "manifest validation 1A.5: 0/20 top-strategies passou >=4/6 gates do "
    "Critic v4 com 2 anos. Champions tambem reprovado (walk-fwd OOS 0%). "
    "Pausa precaucional ate Aether descobrir alpha real."
)


def _aether_name(record: dict) -> str:
    """Replica a logica de active.py:248-251 para gerar o name esperado."""
    sid = str(record.get("id", ""))
    suffix = sid[-8:]
    engine = record.get("engine") or ""
    probe_specs = record.get("probe_specs")
    if engine == "combo" or (isinstance(probe_specs, list) and len(probe_specs) > 1):
        return f"Aether:combo#{suffix}"
    if engine:
        return f"Aether:{engine}#{suffix}"
    template = record.get("template") or "?"
    return f"{template}#{sid[:8]}"


def collect_aether_names() -> list[tuple[str, str]]:
    """Le manifest, retorna lista de (name, asset)."""
    if not MANIFEST_PATH.exists():
        print(f"[pause] manifest nao existe em {MANIFEST_PATH}", flush=True)
        return []
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    out = []
    for s in data.get("strategies", []):
        out.append((_aether_name(s), s.get("asset", "?")))
    return out


def apply_pause(names: list[str], reason: str) -> dict:
    """Adiciona names em _meta.paused_strategies do state.json (com lock)."""
    with pbl._lock:
        state = pbl._load_state()
        meta = pbl._ensure_meta_state(state)
        paused = meta.setdefault("paused_strategies", {})
        already = sum(1 for n in names if n in paused)
        for n in names:
            paused[n] = reason
        pbl._save_state(state)
    return {"requested": len(names), "already_paused": already, "now_paused": len(names)}


def apply_unpause(names: list[str]) -> dict:
    with pbl._lock:
        state = pbl._load_state()
        meta = pbl._ensure_meta_state(state)
        paused = meta.setdefault("paused_strategies", {})
        removed = 0
        for n in names:
            if paused.pop(n, None) is not None:
                removed += 1
        pbl._save_state(state)
    return {"requested": len(names), "removed": removed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unpause", action="store_true", help="reverter pausa")
    ap.add_argument("--legacy-only", action="store_true", help="so cesta legada")
    ap.add_argument("--aether-only", action="store_true", help="so manifest Aether")
    args = ap.parse_args()

    aether_pairs = collect_aether_names()
    aether_names = [n for n, _ in aether_pairs]
    legacy = list(LEGACY_STRATEGIES)

    if args.legacy_only:
        names = legacy
    elif args.aether_only:
        names = aether_names
    else:
        names = aether_names + legacy

    print(f"[pause] manifest Aether: {len(aether_names)} estrategias", flush=True)
    print(f"[pause] cesta legada: {len(legacy)} estrategias", flush=True)
    print(f"[pause] alvo: {len(names)} nomes ({'UNPAUSE' if args.unpause else 'PAUSE'})\n", flush=True)

    if args.unpause:
        res = apply_unpause(names)
        print(f"[pause] {res['removed']}/{res['requested']} despausadas", flush=True)
    else:
        res = apply_pause(names, PAUSE_REASON)
        print(f"[pause] {res['now_paused']}/{res['requested']} pausadas (ja eram: {res['already_paused']})", flush=True)

    # Relatorio
    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": "unpause" if args.unpause else "pause",
        "reason": PAUSE_REASON if not args.unpause else None,
        "aether_count": len(aether_names),
        "legacy_count": len(legacy),
        "applied": res,
        "aether_names": aether_names,
        "legacy_names": legacy,
        "by_asset": {},
    }
    by_asset: dict[str, list[str]] = {}
    for name, asset in aether_pairs:
        by_asset.setdefault(asset, []).append(name)
    report["by_asset"] = by_asset

    out = ROOT / "src/logs/agents/strategies_paused.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nrelatorio: {out}", flush=True)

    # Mostra preview
    state = pbl._load_state()
    paused_now = state.get("_meta", {}).get("paused_strategies", {})
    print(f"\nestado atual: {len(paused_now)} estrategias pausadas no state.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

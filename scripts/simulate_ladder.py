"""Simula o ladder de protecao escalonado para cada Tier."""
from src.modules.StrategyTiers import (
    PROFIT_LOCK_LADDER,
    TIER_CONFIG,
    compute_ladder_floor_usd,
)


def simulate(tier: int, peaks_pct: list[float]) -> None:
    cfg = TIER_CONFIG[tier]
    pos = cfg["position_usd"]
    activation = cfg["trail_activation_usd"]
    floor_min = cfg["trail_floor_usd"]
    print(f"\n=== TIER {tier} (posicao ${pos:.0f}) ===")
    print(f"Trail ativa: peak >= ${activation:.2f} ({activation/pos*100:.1f}% da posicao)")
    print(f"Floor minimo: ${floor_min:.2f} ({floor_min/pos*100:.1f}%)")
    print(f"\n{'Peak %':>8} {'Peak USD':>10} {'Ladder lock %':>15} {'Ladder USD':>12} {'Trail efetivo':>16}")
    for pct in peaks_pct:
        peak_usd = pos * (pct / 100.0)
        ladder_usd, locked_pct = compute_ladder_floor_usd(peak_usd, pos)
        # Combinacao com floor minimo (sem giveback adaptativo, base case)
        effective = max(floor_min, ladder_usd)
        print(f"{pct:>7.1f}% ${peak_usd:>+9.2f} {locked_pct:>14.1f}% ${ladder_usd:>+11.2f} ${effective:>+15.2f}")


print("=" * 70)
print("LADDER de protecao escalonada (peak % -> piso travado %)")
print("=" * 70)
for threshold, lock in PROFIT_LOCK_LADDER:
    print(f"  peak >= {threshold:>5.1f}%  ->  trava no minimo {lock:>5.1f}%  (giveback fixo {threshold-lock:.0f}pp)")

# Simulacao por tier
for tier in (1, 2, 3):
    simulate(tier, [3, 5, 7, 10, 15, 20, 30, 50])

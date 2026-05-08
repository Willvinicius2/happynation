"""One-shot: rastreia por que sobram so 14 deployed quando ha 36 paper_ready."""
import json, os, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.research.aether.auto_deploy import (
    _connect, _validated_nodes, _passes_deploy_gates, _record_for,
    _family_for, _asset_for, _asset_family_tf_key,
    MIN_SCORE, MIN_TRADES, MAX_MDD_PCT, MIN_PNL_PCT, CRITIC_GATE,
    MAX_PER_FAMILY, MAX_PER_ASSET, MAX_PER_ASSET_FAMILY_TF,
    REQUIRE_COMBO_FOR_MULTI, ALLOWED_ASSETS, DEPLOY_TOP_N,
)

print("Caps em uso:")
print(f"  MIN_SCORE={MIN_SCORE} MIN_TRADES={MIN_TRADES} MAX_MDD={MAX_MDD_PCT} MIN_PNL={MIN_PNL_PCT} CRITIC={CRITIC_GATE}")
print(f"  MAX_PER_FAMILY={MAX_PER_FAMILY} MAX_PER_ASSET={MAX_PER_ASSET} MAX_PER_ASSET_FAMILY_TF={MAX_PER_ASSET_FAMILY_TF}")
print(f"  ALLOWED_ASSETS={sorted(ALLOWED_ASSETS)} TOP_N={DEPLOY_TOP_N} REQUIRE_COMBO_FOR_MULTI={REQUIRE_COMBO_FOR_MULTI}")
print()

with _connect() as conn:
    nodes = _validated_nodes(conn)

passed_gates = []
for row, metrics in nodes:
    if _passes_deploy_gates(metrics, float(row["score"])):
        rec = _record_for(row, metrics)
        if rec is not None:
            passed_gates.append((float(row["score"]), rec, row, metrics))

print(f"Validated nodes:                                {len(nodes)}")
print(f"Passou _passes_deploy_gates + tem _record_for:  {len(passed_gates)}")

# dedupe
by_key = {}
for score, rec, row, metrics in passed_gates:
    key = json.dumps({"e": rec["engine"], "p": rec["params"], "a": rec["asset"], "r": rec["risk"]}, sort_keys=True)
    if key not in by_key or score > by_key[key][0]:
        by_key[key] = (score, rec, row, metrics)

print(f"Apos dedupe (engine,params,asset,risk):         {len(by_key)}")

ranked = sorted(by_key.values(), key=lambda x: -x[0])

# aplica caps com tracking de motivos
selected = []
fam = Counter(); ast = Counter(); aft = Counter()
rejected = defaultdict(list)
for score, rec, row, metrics in ranked:
    family = _family_for(rec); asset = _asset_for(rec); aftk = _asset_family_tf_key(rec)
    why = None
    if MAX_PER_FAMILY > 0 and fam[family] >= MAX_PER_FAMILY:
        why = f"family cap ({family} >= {MAX_PER_FAMILY})"
    elif MAX_PER_ASSET > 0 and ast[asset] >= MAX_PER_ASSET:
        why = f"asset cap ({asset} >= {MAX_PER_ASSET})"
    elif MAX_PER_ASSET_FAMILY_TF > 0 and aft[aftk] >= MAX_PER_ASSET_FAMILY_TF:
        why = f"asset+fam+tf cap ({aftk} >= {MAX_PER_ASSET_FAMILY_TF})"
    elif len(selected) >= DEPLOY_TOP_N:
        why = f"top_n cap ({DEPLOY_TOP_N})"
    if why:
        rejected[why].append(f"{rec['asset']}/{rec['engine']}/{rec['timeframe']} score={score:.3f}")
        continue
    selected.append((score, rec))
    fam[family] += 1; ast[asset] += 1; aft[aftk] += 1

print(f"Selecionado apos caps:                          {len(selected)}")
print()
print("Distribuicao final:")
print(f"  Por familia: {dict(fam)}")
print(f"  Por ativo:   {dict(ast)}")
print()
if rejected:
    print("Rejeitados pelos caps:")
    for why, items in sorted(rejected.items(), key=lambda x: -len(x[1])):
        print(f"  [{len(items)}x] {why}")
        for it in items[:5]:
            print(f"      - {it}")
        if len(items) > 5:
            print(f"      ... +{len(items)-5}")

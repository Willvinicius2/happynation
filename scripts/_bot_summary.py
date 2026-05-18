"""Resumo rapido do estado do bot a partir de src/logs/trading_bot.log."""
import re
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "src" / "logs" / "trading_bot.log"
lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()

# ultimo PnL por simbolo
pnls: dict[str, float] = {}
cur_sym = None
for ln in lines[-5000:]:
    m = re.search(r"Balan.o atual: [\d.]+ \((\w+)\)", ln)
    if m:
        cur_sym = m.group(1)
        continue
    m = re.search(r"PnL USD: \$([+-][\d.]+)", ln)
    if m and cur_sym:
        pnls[cur_sym] = float(m.group(1))
        cur_sym = None

print("Posicoes abertas (ultimo PnL nao-realizado por simbolo):")
tot = 0.0
for s, p in sorted(pnls.items()):
    print(f"  {s:<8} ${p:+.2f}")
    tot += p
print(f"  TOTAL nao-realizado: ${tot:+.2f}  ({len(pnls)} posicoes)")

# eventos de fechamento
closes = [ln for ln in lines if re.search(r"fechou|realizad|STOP\s*LOSS|TAKE\s*PROFIT|stop atingido|trail.*sa[ií]|sell order|ordem de venda", ln, re.I)]
print(f"\nEventos fechamento/SL/TP no log: {len(closes)}")
for c in closes[-10:]:
    print("  ", c[-120:])

# total de linhas / span temporal
ts_first = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", lines[0]) if lines else None
ts_last = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", lines[-1]) if lines else None
print(f"\nLog span: {ts_first.group(1) if ts_first else '?'} -> {ts_last.group(1) if ts_last else '?'} ({len(lines)} linhas)")

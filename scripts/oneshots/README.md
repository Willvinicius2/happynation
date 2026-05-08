# scripts/oneshots

Scripts one-shot exploratorios movidos de `src/` em 2026-05-05.

Sao executados manualmente da **raiz do projeto** (cwd = HAPPYNATIONBOT/), ex:

```powershell
python -u scripts/oneshots/backtests.py
```

Eles esperam encontrar `src/...` no PYTHONPATH (cwd raiz resolve isso) e
escrevem em paths relativos como `src/logs/...`. Nao foram refatorados; estao
aqui pra reduzir ruido em `src/`.

Sem garantia de manutencao — pode quebrar a qualquer momento. Se um deles virar
util de verdade, promove pra `scripts/` raiz e adiciona descricao.

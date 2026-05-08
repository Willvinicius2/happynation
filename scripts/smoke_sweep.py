from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aether_app.compute import default_probes_for_timeframe, run_sweep
from aether_app.config import DEFAULT_WORKERS, LEGACY_ROOT, checkpoint_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a small Aether compute farm smoke test.")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--sweep-id", default="smoke-one-probe")
    args = parser.parse_args()

    probes = default_probes_for_timeframe(LEGACY_ROOT, args.timeframe, limit=args.limit)
    print({"probe_count": len(probes), "workers_available": DEFAULT_WORKERS})
    if not probes:
        raise SystemExit(f"no eligible {args.timeframe} probes found")

    result = run_sweep(
        probes=probes,
        legacy_root=LEGACY_ROOT,
        workers=args.workers,
        sweep_id=args.sweep_id,
        checkpoint_path=checkpoint_dir() / f"{args.sweep_id}.json",
        resume=False,
    )
    print(
        {
            "status": result["status"],
            "workers": result["workers"],
            "completed": result["completed"],
            "failed": result["failed"],
            "summary": result["summary"],
            "checkpoint_path": result["checkpoint_path"],
        }
    )


if __name__ == "__main__":
    main()

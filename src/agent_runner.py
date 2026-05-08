from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.agents.runner import DEFAULT_REPORT_PATH, run_default_agents


def main() -> None:
    parser = argparse.ArgumentParser(description="Executa agentes locais de monitoramento do HAPPYNATIONBOT.")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_REPORT_PATH),
        help="Arquivo JSON de saida do relatorio dos agentes.",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Mostra o relatorio no terminal sem gravar arquivo.",
    )
    args = parser.parse_args()

    report = run_default_agents(report_path=Path(args.out), write_report=not args.no_write)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

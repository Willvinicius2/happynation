from __future__ import annotations

import argparse
import json
import os
import warnings
from pathlib import Path
from html import escape

import pandas as pd
from binance.client import Client

from src.modules.StrategyResearchEngine import (
    ResearchRules,
    discover_extra_strategy_specs,
    get_default_strategy_specs,
    research_strategies_parallel,
    results_to_dataframe,
)


OUT_DIR = Path("src/logs/research")
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)


def fetch_klines(client: Client, symbol: str, interval: str, start_str: str) -> pd.DataFrame:
    raw = client.get_historical_klines(symbol, interval, start_str)
    columns = [
        "open_time",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "close_time",
        "qav",
        "trades",
        "tbbav",
        "tbqav",
        "_",
    ]
    data = pd.DataFrame(raw, columns=columns)
    for column in ["open_price", "high_price", "low_price", "close_price", "volume"]:
        data.loc[:, column] = pd.to_numeric(data[column], errors="coerce")
    data.loc[:, "open_time"] = pd.to_datetime(data["open_time"], unit="ms", utc=True)
    return data[["open_time", "open_price", "high_price", "low_price", "close_price", "volume"]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pesquisa estrategias em paralelo e aprova apenas as que passam lucro, trades e winrate por janela."
    )
    parser.add_argument("--assets", default="BTCUSDT,ETHUSDT,SOLUSDT,JUPUSDT", help="Lista separada por virgula.")
    parser.add_argument("--interval", default=Client.KLINE_INTERVAL_1HOUR)
    parser.add_argument("--history", default="8 months ago UTC")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--include-extras", action="store_true")
    parser.add_argument("--window-days", type=int, default=60)
    parser.add_argument("--min-window-profit", type=float, default=50.0)
    parser.add_argument("--min-window-trades", type=int, default=6)
    parser.add_argument("--min-total-trades", type=int, default=20)
    parser.add_argument("--min-winrate", type=float, default=50.0)
    parser.add_argument("--max-drawdown", type=float, default=25.0)
    parser.add_argument("--min-profit-factor", type=float, default=1.15)
    parser.add_argument("--max-loss-streak", type=int, default=4)
    parser.add_argument("--min-consistency-score", type=float, default=55.0)
    parser.add_argument("--fee", type=float, default=0.1, help="Taxa por lado em porcentagem.")
    return parser.parse_args()


def _fmt(value: object, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, bool):
        return "sim" if value else "nao"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


def write_research_tree_report(results, out_path: Path, top_n: int = 24) -> None:
    ranked = sorted(results, key=lambda item: item.score, reverse=True)[:top_n]
    if not ranked:
        body_nodes = '<div class="empty">Nenhuma memoria de pesquisa para exibir.</div>'
        memory_items = ""
        best = None
    else:
        best = ranked[0]
        groups: dict[str, list] = {}
        for result in ranked:
            groups.setdefault(result.strategy, []).append(result)

        body_nodes_parts = []
        for group_index, (strategy, items) in enumerate(groups.items()):
            body_nodes_parts.append(
                f'<section class="neuron-cluster" style="--delay:{group_index * 70}ms">'
                f'<div class="synapse"></div><button class="neuron strategy-neuron">'
                f'<span>{escape(strategy)}</span><small>{len(items)} sinais</small></button>'
                '<div class="axon-stack">'
            )
            for item_index, item in enumerate(items[:4]):
                quality = "accepted" if item.passed else "inhibited"
                body_nodes_parts.append(
                    f'<article class="memory-node {quality}" style="--delay:{(group_index * 70) + (item_index * 45)}ms">'
                    f'<strong>{escape(item.asset)}</strong>'
                    f'<span>score {_fmt(item.score, 1)}</span>'
                    f'<small>cons {_fmt(item.consistency_score, 1)} | dd {_fmt(item.max_drawdown_pct, 1)}% | pf {_fmt(item.profit_factor, 2)}</small>'
                    '</article>'
                )
            body_nodes_parts.append('</div></section>')
        body_nodes = '<div class="tree-flow">' + "".join(body_nodes_parts) + '</div>'

        memory_items = "".join(
            '<li>'
            f'<span class="status {"ok" if item.passed else "cut"}">{"Memoria aceita" if item.passed else "Sinal inibido"}</span>'
            f'<strong>{escape(item.strategy)} {escape(item.asset)}</strong>'
            f'<small>score {_fmt(item.score, 1)} · win {_fmt(item.winrate_pct, 1)}% · pf {_fmt(item.profit_factor, 2)}</small>'
            '</li>'
            for item in ranked[:12]
        )

    best_title = escape(f"{best.strategy} / {best.asset}") if best else "-"
    best_score = _fmt(best.score, 2) if best else "-"
    best_consistency = _fmt(best.consistency_score, 2) if best else "-"
    best_drawdown = _fmt(best.max_drawdown_pct, 2) if best else "-"
    best_pf = _fmt(best.profit_factor, 2) if best else "-"
    approved_count = sum(1 for result in results if result.passed)
    pruned_count = len(results) - approved_count

    html = f"""<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HAPPYNATIONBOT Strategy Search</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #06100f;
      --panel: #0b1718;
      --line: #164145;
      --muted: #7fa1a0;
      --text: #e9fff4;
      --amber: #f2c66d;
      --blue: #59d8ff;
      --red: #ff6b6b;
      --green: #4dffab;
      --violet: #b98cff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 22% 16%, rgba(77,255,171,.12), transparent 28%),
        radial-gradient(circle at 74% 62%, rgba(89,216,255,.10), transparent 32%),
        var(--bg);
      color: var(--text);
      font-family: Consolas, "Segoe UI", monospace;
      letter-spacing: 0;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 48px 300px 48px 0;
      background:
        linear-gradient(var(--line) 1px, transparent 1px),
        linear-gradient(90deg, var(--line) 1px, transparent 1px),
        radial-gradient(circle, rgba(77,255,171,.35) 1px, transparent 1px);
      background-size: 58px 58px, 58px 58px, 116px 116px;
      opacity: .22;
      pointer-events: none;
    }}
    header {{
      height: 48px;
      display: flex;
      align-items: center;
      gap: 18px;
      padding: 0 18px;
      border-bottom: 1px solid #1d2330;
      background: rgba(3, 12, 12, .96);
    }}
    .brand {{ font-weight: 800; font-size: 18px; }}
    .brand span {{ color: var(--green); font-weight: 500; }}
    .tag {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 4px; }}
    .actions {{ margin-left: auto; display: flex; gap: 8px; align-items: center; }}
    .btn {{ border: 1px solid #2a3242; padding: 7px 14px; font-size: 11px; color: var(--text); background: #11141b; }}
    .btn.primary {{ background: linear-gradient(135deg, var(--green), var(--blue)); color: #03100f; border-color: transparent; font-weight: 700; }}
    main {{ display: grid; grid-template-columns: minmax(0, 1fr) 300px; min-height: calc(100vh - 48px); }}
    .canvas {{ position: relative; padding: 28px 28px 86px; overflow: auto; }}
    .caption {{ color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 5px; }}
    .master {{
      position: relative;
      width: min(760px, 100%);
      margin: 28px auto 0;
      display: grid;
      justify-items: center;
      gap: 10px;
      z-index: 2;
    }}
    .master strong {{ font-family: Georgia, serif; font-size: 17px; }}
    .core {{
      width: 42px;
      height: 42px;
      border-radius: 50%;
      border: 1px solid #6e5a35;
      background: #d9b65f;
      box-shadow: 0 0 20px rgba(216,168,79,.55);
    }}
    .branch {{
      position: absolute;
      top: 238px;
      left: var(--x);
      width: 210px;
      min-height: 380px;
      transform: translateX(-50%);
      animation: fade .45s ease both;
      animation-delay: var(--delay);
    }}
    .branch-line {{
      position: absolute;
      top: -112px;
      left: 50%;
      width: 1px;
      height: 116px;
      background: var(--line);
      transform-origin: top;
      opacity: .8;
    }}
    .strategy-node {{
      width: 100%;
      border: 0;
      background: transparent;
      color: var(--text);
      display: grid;
      justify-items: center;
      gap: 4px;
      font-family: Georgia, serif;
      font-size: 14px;
    }}
    .strategy-node::before {{
      content: "";
      width: 18px;
      height: 18px;
      border-radius: 50%;
      border: 2px solid #344052;
      background: #0c0f15;
      box-shadow: inset 0 0 0 4px #111724;
    }}
    .strategy-node small, .leaf small {{ color: var(--muted); font-family: Consolas, monospace; font-size: 10px; }}
    .leaf-row {{ margin-top: 62px; display: grid; gap: 24px; }}
    .leaf {{
      border: 1px solid #202839;
      background: rgba(14, 17, 24, .82);
      padding: 10px;
      min-height: 68px;
      display: grid;
      gap: 4px;
      animation: fade .45s ease both;
      animation-delay: var(--delay);
    }}
    .leaf strong {{ color: var(--text); font-size: 12px; }}
    .leaf span {{ color: var(--amber); font-size: 14px; }}
    .leaf.approved {{ border-color: rgba(104,217,154,.45); box-shadow: inset 3px 0 0 var(--green); }}
    .leaf.pruned {{ border-color: rgba(162,79,73,.35); box-shadow: inset 3px 0 0 var(--red); opacity: .74; }}
    .tree-flow {{
      width: min(980px, 100%);
      margin: 92px auto 0;
      display: grid;
      gap: 26px;
      padding-bottom: 44px;
    }}
    .neuron-cluster {{
      position: relative;
      width: 100%;
      min-height: 0;
      display: grid;
      grid-template-columns: 230px minmax(0, 1fr);
      gap: 22px;
      align-items: start;
      animation: fade .45s ease both;
      animation-delay: var(--delay);
    }}
    .synapse {{
      position: absolute;
      top: -56px;
      left: 114px;
      width: 2px;
      height: 58px;
      background: linear-gradient(var(--green), rgba(89,216,255,.08));
      box-shadow: 0 0 18px rgba(77,255,171,.34);
      transform-origin: top;
      opacity: .75;
    }}
    .strategy-neuron {{
      width: 100%;
      border: 1px solid rgba(77,255,171,.30);
      background: linear-gradient(180deg, rgba(15,39,37,.92), rgba(9,15,22,.92));
      color: var(--text);
      display: grid;
      justify-items: center;
      gap: 5px;
      font-size: 13px;
      font-weight: 800;
      min-height: 96px;
      padding: 14px 12px;
    }}
    .strategy-neuron::before {{
      content: "";
      width: 28px;
      height: 28px;
      border-radius: 50%;
      background:
        radial-gradient(circle at 42% 38%, #ffffff 0 10%, var(--green) 11% 34%, rgba(89,216,255,.42) 35% 58%, rgba(11,23,24,.92) 60%);
      border: 1px solid rgba(77,255,171,.65);
      box-shadow: 0 0 24px rgba(77,255,171,.38);
    }}
    .strategy-neuron small, .memory-node small {{ color: var(--muted); font-family: Consolas, monospace; font-size: 10px; }}
    .axon-stack {{
      margin-top: 0;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 12px;
    }}
    .memory-node {{
      border: 1px solid rgba(89,216,255,.18);
      background: linear-gradient(135deg, rgba(11, 28, 29, .88), rgba(9, 14, 24, .74));
      padding: 11px;
      min-height: 86px;
      display: grid;
      gap: 5px;
      animation: fade .45s ease both;
      animation-delay: var(--delay);
      position: relative;
      overflow: hidden;
    }}
    .memory-node::before {{
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 3px;
      background: var(--blue);
      box-shadow: 0 0 14px currentColor;
    }}
    .memory-node strong {{ color: var(--text); font-size: 12px; overflow-wrap: anywhere; }}
    .memory-node span {{ color: var(--green); font-size: 15px; font-weight: 800; }}
    .memory-node small {{ line-height: 1.45; }}
    .memory-node.accepted {{ border-color: rgba(77,255,171,.48); }}
    .memory-node.accepted::before {{ background: var(--green); }}
    .memory-node.inhibited {{ border-color: rgba(255,107,107,.30); opacity: .76; }}
    .memory-node.inhibited::before {{ background: var(--red); }}
    aside {{
      border-left: 1px solid #1d2330;
      background:
        linear-gradient(180deg, rgba(13,30,31,.96), rgba(6,12,17,.98));
      padding: 22px;
      display: grid;
      grid-template-rows: auto auto 1fr;
      gap: 24px;
    }}
    .panel-title {{ color: var(--muted); font-size: 10px; letter-spacing: 4px; text-transform: uppercase; }}
    .best h1 {{ margin: 12px 0 18px; font-size: 22px; color: var(--green); }}
    .metric {{ display: flex; justify-content: space-between; border-bottom: 1px solid #202532; padding: 9px 0; font-size: 12px; }}
    .metric span {{ color: var(--muted); }}
    .metric strong {{ color: var(--blue); }}
    .log {{ list-style: none; padding: 0; margin: 0; overflow: auto; }}
    .log li {{ border-bottom: 1px solid #171c26; padding: 10px 0; display: grid; gap: 4px; }}
    .log strong {{ font-size: 11px; }}
    .log small {{ color: var(--muted); font-size: 10px; }}
    .status {{ font-size: 10px; }}
    .status.ok {{ color: var(--green); }}
    .status.cut {{ color: var(--red); }}
    .footer {{
      position: absolute;
      left: 0;
      right: 300px;
      bottom: 0;
      height: 54px;
      border-top: 1px solid #1d2330;
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      background: rgba(8,10,14,.92);
    }}
    .footer div {{ padding: 10px 18px; border-right: 1px solid #1d2330; color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 3px; }}
    .footer strong {{ float: right; color: var(--text); font-size: 16px; letter-spacing: 0; }}
    @keyframes fade {{ from {{ opacity: 0; transform: translateY(8px); }} to {{ opacity: 1; transform: translateY(0); }} }}
  </style>
</head>
<body>
  <header>
    <div class="brand">Happy<span>Mind</span></div>
    <div class="tag">Neural Strategy Map · memoria operacional</div>
    <div class="actions">
      <div class="tag">Concluido</div>
      <button class="btn">Pausar</button>
      <button class="btn primary">Nova memoria</button>
      <button class="btn">1x</button>
    </div>
  </header>
  <main>
    <section class="canvas">
      <div class="caption">Mapa neural - sinais, podas e reforcos</div>
      <div class="master"><strong>Nucleo de decisao</strong><div class="core"></div></div>
      {body_nodes}
      <div class="footer">
        <div>Memorias <strong>{len(results)}</strong></div>
        <div>Reforcos <strong>{approved_count}</strong></div>
        <div>Inibidos <strong>{pruned_count}</strong></div>
        <div>Melhor score <strong>{best_score}</strong></div>
      </div>
    </section>
    <aside>
      <section class="best">
        <div class="panel-title">Memoria dominante</div>
        <h1>{best_title}</h1>
        <div class="metric"><span>Pulso robusto</span><strong>{best_score}</strong></div>
        <div class="metric"><span>Consistencia</span><strong>{best_consistency}</strong></div>
        <div class="metric"><span>Drawdown</span><strong>{best_drawdown}%</strong></div>
        <div class="metric"><span>Profit factor</span><strong>{best_pf}</strong></div>
      </section>
      <section>
        <div class="panel-title">Historico de acoes e decisoes</div>
      </section>
      <ol class="log">{memory_items}</ol>
    </aside>
  </main>
</body>
</html>"""
    out_path.write_text(html, encoding="utf-8")


def main() -> None:
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    assets = [asset.strip().upper() for asset in args.assets.split(",") if asset.strip()]
    client = Client(os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_SECRET_KEY", ""))

    print(f"Baixando candles de {len(assets)} ativos desde {args.history}...")
    data_by_asset = {}
    for asset in assets:
        data_by_asset[asset] = fetch_klines(client, asset, args.interval, args.history)
        print(f"  {asset}: {len(data_by_asset[asset])} candles")

    strategies = get_default_strategy_specs()
    if args.include_extras:
        strategies.extend(discover_extra_strategy_specs())

    rules = ResearchRules(
        window_days=args.window_days,
        min_window_profit_pct=args.min_window_profit,
        min_window_trades=args.min_window_trades,
        min_total_trades=args.min_total_trades,
        min_winrate_pct=args.min_winrate,
        max_drawdown_pct=args.max_drawdown,
        min_profit_factor=args.min_profit_factor,
        max_loss_streak=args.max_loss_streak,
        min_consistency_score=args.min_consistency_score,
        trade_fee_pct=args.fee,
    )

    print(f"Rodando {len(strategies) * len(data_by_asset)} testes em paralelo...")
    results = research_strategies_parallel(
        data_by_asset=data_by_asset,
        strategies=strategies,
        rules=rules,
        max_workers=args.workers,
    )

    dataframe = results_to_dataframe(results)
    dataframe.to_csv(OUT_DIR / "strategy_research_results.csv", index=False, encoding="utf-8")
    with open(OUT_DIR / "strategy_research_results.json", "w", encoding="utf-8") as file:
        json.dump([result.to_dict() for result in results], file, ensure_ascii=False, indent=2)

    approved = dataframe[dataframe["passed"] == True].copy()
    approved.to_csv(OUT_DIR / "approved_strategies.csv", index=False, encoding="utf-8")
    write_research_tree_report(results, OUT_DIR / "strategy_research_tree.html")

    print("\nAprovadas:")
    if approved.empty:
        print("  Nenhuma estrategia passou em todos os criterios.")
    else:
        columns = [
            "asset",
            "strategy",
            "params",
            "total_profit_pct",
            "total_trades",
            "winrate_pct",
            "max_drawdown_pct",
            "profit_factor",
            "max_loss_streak",
            "consistency_score",
            "score",
            "min_window_profit_pct",
            "windows_passed",
            "windows_total",
        ]
        print(approved[columns].head(20).to_string(index=False))

    print(f"\nArquivos salvos em: {OUT_DIR}")
    print(f"Relatorio visual: {OUT_DIR / 'strategy_research_tree.html'}")


if __name__ == "__main__":
    main()

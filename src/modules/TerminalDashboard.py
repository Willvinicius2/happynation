import threading
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich import box

_state: dict = {}
_lock = threading.Lock()
_live: Live = None
_enabled = False


def update(symbol: str, data: dict):
    with _lock:
        if symbol not in _state:
            _state[symbol] = {}
        _state[symbol].update(data)
        _state[symbol]["updated_at"] = datetime.now().strftime("%H:%M:%S")


def _build_table() -> Table:
    table = Table(
        box=box.ROUNDED,
        border_style="bright_black",
        header_style="bold cyan",
        show_lines=True,
        expand=True,
    )

    table.add_column("Par",       style="bold white",  min_width=10)
    table.add_column("Posicao",   justify="center",    min_width=10)
    table.add_column("Preco",     justify="right",     min_width=12)
    table.add_column("RSI",       justify="right",     min_width=7)
    table.add_column("Decisao",   justify="center",    min_width=10)
    table.add_column("Compra em", justify="right",     min_width=12)
    table.add_column("P&L",       justify="right",     min_width=9)
    table.add_column("Stop Loss", justify="right",     min_width=12)
    table.add_column("Ciclo",     justify="center",    min_width=6)
    table.add_column("Horario",   justify="center",    min_width=9)

    with _lock:
        items = [(k, v) for k, v in _state.items() if not k.startswith("_")]

    if not items:
        table.add_row(*["—"] * 10)
        return table

    for symbol, d in items:
        bought = d.get("position") is True

        # Posicao
        if bought:
            pos = Text("Comprado", style="bold green")
        else:
            pos = Text("Vendido", style="dim white")

        # Preco
        price = d.get("price")
        price_str = f"{price:.6f}" if price else "—"

        # RSI
        rsi = d.get("rsi")
        if rsi is None:
            rsi_text = Text("—", style="white")
        elif rsi >= 70:
            rsi_text = Text(f"{rsi:.1f}", style="bold red")
        elif rsi <= 30:
            rsi_text = Text(f"{rsi:.1f}", style="bold green")
        else:
            rsi_text = Text(f"{rsi:.1f}", style="white")

        # Decisao
        decision = d.get("decision", "—")
        if decision == "Comprar":
            dec_text = Text("Comprar", style="bold green")
        elif decision == "Vender":
            dec_text = Text("Vender", style="bold red")
        else:
            dec_text = Text(decision or "—", style="dim white")

        # Preco de compra
        buy_price = d.get("buy_price")
        buy_str = f"{buy_price:.6f}" if buy_price else "—"

        # P&L
        pnl = d.get("pnl_pct")
        if pnl is None or not bought:
            pnl_text = Text("—", style="dim white")
        elif pnl >= 0:
            pnl_text = Text(f"+{pnl:.2f}%", style="bold green")
        else:
            pnl_text = Text(f"{pnl:.2f}%", style="bold red")

        # Stop Loss
        sl = d.get("stop_loss")
        sl_str = f"{sl:.6f}" if sl else "—"

        # Ciclo e horario
        cycle = str(d.get("cycle", "—"))
        updated = d.get("updated_at", "—")

        table.add_row(symbol, pos, price_str, rsi_text, dec_text, buy_str, pnl_text, sl_str, cycle, updated)

    return table


def _build_panel() -> Panel:
    import os
    modes = []
    if os.getenv("PAPER_TRADING", "false").lower() == "true":
        modes.append("[bold cyan]PAPER TRADING[/]")
    if os.getenv("OFFLINE_MODE", "false").lower() == "true":
        modes.append("[bold red]OFFLINE[/]")
    mode_str = "  |  ".join(modes) if modes else "[green]LIVE[/]"

    title = f"[bold white]HAPPYNATIONBOT[/]  {mode_str}"
    return Panel(_build_table(), title=title, border_style="bright_black", padding=(0, 1))


def start():
    global _live, _enabled
    import os
    import sys

    force = os.getenv("TERMINAL_DASHBOARD_FORCE", "").strip().lower() in {"1", "true", "yes", "on"}
    disabled = os.getenv("TERMINAL_DASHBOARD", "1").strip().lower() in {"0", "false", "no", "off"}
    if disabled or (not force and not sys.__stdout__.isatty()):
        _enabled = False
        return

    console = Console(file=sys.__stdout__, force_terminal=True, legacy_windows=False)
    _live = Live(
        _build_panel(),
        console=console,
        refresh_per_second=1,
        screen=False,
        redirect_stdout=False,
        redirect_stderr=False,
    )
    _live.start()
    _enabled = True


def render():
    if _enabled and _live:
        _live.update(_build_panel())


def stop():
    global _enabled
    if _live:
        _live.stop()
    _enabled = False

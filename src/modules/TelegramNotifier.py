from __future__ import annotations

import html
import logging
import os
from datetime import datetime
from typing import Any

import requests
from dotenv import load_dotenv

# The desktop/session environment may contain stale Telegram credentials.
# In this project, .env is the runtime source of truth.
load_dotenv(override=True)


def _credentials() -> tuple[str | None, str | None]:
    load_dotenv(override=True)
    return os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")


TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID = _credentials()

_PLACEHOLDER_HINTS = (
    "your_",
    "seu_",
    "sua_",
    "cole sua",
    "changeme",
    "placeholder",
    "example",
)


def _mask(value: str | None) -> str:
    if not value:
        return "<missing>"
    if len(value) <= 12:
        return value[:3] + "..."
    return value[:8] + "..." + value[-5:]


def _configured(value: str | None) -> bool:
    cleaned = (value or "").strip().strip('"').strip("'")
    if not cleaned:
        return False
    return not any(hint in cleaned.lower() for hint in _PLACEHOLDER_HINTS)


def send_telegram_message(token: str | None, chat_id: str | None, message: str) -> requests.Response | None:
    if not _configured(token) or not _configured(chat_id):
        logging.warning(
            "[TELEGRAM] envio ignorado: token=%s chat_id=%s",
            _mask(token),
            _mask(chat_id),
        )
        return None

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, Any] = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, data=payload, timeout=10)
    except requests.exceptions.RequestException as exc:
        logging.error("[TELEGRAM] falha de rede no envio: %s", exc)
        return None

    if response.ok:
        logging.info("[TELEGRAM] mensagem enviada chat_id=%s status=%s", chat_id, response.status_code)
    else:
        logging.error(
            "[TELEGRAM] envio rejeitado chat_id=%s status=%s resposta=%s",
            chat_id,
            response.status_code,
            response.text[:500],
        )
    return response


def _send(message: str) -> bool:
    token, chat_id = _credentials()
    response = send_telegram_message(token, chat_id, message)
    return bool(response is not None and response.ok)


def _horario() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def _fmt(value: float, places: int = 6) -> str:
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return "-"


def _safe(text: object) -> str:
    return html.escape(str(text), quote=False)


def _pnl_info(preco_atual: float, preco_compra: float, quantidade: float) -> tuple[str, str, bool]:
    if not preco_compra or preco_compra == 0:
        return "-", "-", True
    pnl_pct = (preco_atual - preco_compra) / preco_compra * 100
    pnl_usdt = (preco_atual - preco_compra) * quantidade
    positivo = pnl_pct >= 0
    sinal = "+" if positivo else ""
    return f"{sinal}{pnl_pct:.2f}%", f"{sinal}{pnl_usdt:.4f} USDT", positivo


def _order_message(title: str, ativo: str, quantidade: float, preco: float, tipo: str, extra: list[str] | None = None) -> str:
    valor_total = float(quantidade or 0) * float(preco or 0)
    lines = [
        f"<b>{_safe(title)}</b>",
        f"Par: <b>{_safe(ativo)}</b>",
        f"Quantidade: <b>{_fmt(quantidade, 8)}</b>",
        f"Preco: <b>{_fmt(preco, 8)} USDT</b>",
        f"Total: <b>{_fmt(valor_total, 4)} USDT</b>",
        f"Tipo: <code>{_safe(tipo)}</code>",
    ]
    if extra:
        lines.extend(extra)
    lines.append(f"<i>{_horario()}</i>")
    return "\n".join(lines)


def notificar_compra(ativo: str, quantidade: float, preco: float, tipo: str = "LIMIT") -> bool:
    return _send(_order_message("HAPPYNATION COMPRA EXECUTADA", ativo, quantidade, preco, tipo))


def notificar_venda(ativo: str, quantidade: float, preco: float, preco_compra: float, tipo: str = "MARKET") -> bool:
    pnl_pct, pnl_usdt, positivo = _pnl_info(preco, preco_compra, quantidade)
    resultado = "LUCRO" if positivo else "PREJUIZO"
    return _send(
        _order_message(
            f"HAPPYNATION VENDA EXECUTADA - {resultado}",
            ativo,
            quantidade,
            preco,
            tipo,
            [f"Comprado em: <code>{_fmt(preco_compra, 8)} USDT</code>", f"P&L: <b>{pnl_pct} ({pnl_usdt})</b>"],
        )
    )


def notificar_take_profit(ativo: str, quantidade: float, preco: float, preco_compra: float, nivel: int, pct_meta: float) -> bool:
    pnl_pct, pnl_usdt, _ = _pnl_info(preco, preco_compra, quantidade)
    return _send(
        _order_message(
            f"HAPPYNATION TAKE PROFIT #{nivel} +{pct_meta:.0f}%",
            ativo,
            quantidade,
            preco,
            "TAKE_PROFIT",
            [f"Comprado em: <code>{_fmt(preco_compra, 8)} USDT</code>", f"Lucro: <b>{pnl_pct} ({pnl_usdt})</b>"],
        )
    )


def notificar_stop_loss(ativo: str, quantidade: float, preco: float, preco_compra: float) -> bool:
    pnl_pct, pnl_usdt, _ = _pnl_info(preco, preco_compra, quantidade)
    return _send(
        _order_message(
            "HAPPYNATION STOP LOSS ATIVADO",
            ativo,
            quantidade,
            preco,
            "STOP_LOSS",
            [f"Comprado em: <code>{_fmt(preco_compra, 8)} USDT</code>", f"Prejuizo: <b>{pnl_pct} ({pnl_usdt})</b>"],
        )
    )


def notificar_trailing_stop(ativo: str, quantidade: float, preco: float, preco_compra: float) -> bool:
    pnl_pct, pnl_usdt, positivo = _pnl_info(preco, preco_compra, quantidade)
    resultado = "LUCRO" if positivo else "PREJUIZO"
    return _send(
        _order_message(
            f"HAPPYNATION TRAILING STOP - {resultado}",
            ativo,
            quantidade,
            preco,
            "TRAILING_STOP",
            [f"Comprado em: <code>{_fmt(preco_compra, 8)} USDT</code>", f"P&L: <b>{pnl_pct} ({pnl_usdt})</b>"],
        )
    )


def notificar_ordem(ativo: str, tipo: str, quantidade: float, preco: float) -> bool:
    return _send(_order_message("HAPPYNATION ORDEM EXECUTADA", ativo, quantidade, preco, tipo))

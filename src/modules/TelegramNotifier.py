from dotenv import load_dotenv
import os
import requests
from datetime import datetime

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram_message(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, data=payload, timeout=10)
        return response
    except requests.exceptions.RequestException:
        return None


def _send(message: str):
    send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, message)


def _horario() -> str:
    return datetime.now().strftime("%d/%m/%Y  %H:%M:%S")


def _pnl_info(preco_atual: float, preco_compra: float, quantidade: float):
    if not preco_compra or preco_compra == 0:
        return "—", "—", True
    pnl_pct = (preco_atual - preco_compra) / preco_compra * 100
    pnl_usdt = (preco_atual - preco_compra) * quantidade
    positivo = pnl_pct >= 0
    sinal = "+" if positivo else ""
    return f"{sinal}{pnl_pct:.2f}%", f"{sinal}{pnl_usdt:.4f} USDT", positivo


def notificar_compra(ativo: str, quantidade: float, preco: float, tipo: str = "LIMIT"):
    valor_total = quantidade * preco
    _send(
        f"🟢 <b>COMPRA EXECUTADA</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Par:         <b>{ativo}</b>\n"
        f"🔢 Quantidade:  <b>{quantidade:.4f}</b>\n"
        f"💵 Preço:       <b>{preco:.6f} USDT</b>\n"
        f"💰 Total:       <b>{valor_total:.4f} USDT</b>\n"
        f"📋 Tipo:        <code>{tipo}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 <i>{_horario()}</i>"
    )


def notificar_venda(ativo: str, quantidade: float, preco: float, preco_compra: float, tipo: str = "MARKET"):
    valor_total = quantidade * preco
    pnl_pct, pnl_usdt, positivo = _pnl_info(preco, preco_compra, quantidade)
    emoji_res = "✅ LUCRO" if positivo else "❌ PREJUIZO"
    emoji_pnl = "📈" if positivo else "📉"

    _send(
        f"🔴 <b>VENDA EXECUTADA  —  {emoji_res}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Par:         <b>{ativo}</b>\n"
        f"🔢 Quantidade:  <b>{quantidade:.4f}</b>\n"
        f"💵 Preço:       <b>{preco:.6f} USDT</b>\n"
        f"💰 Total:       <b>{valor_total:.4f} USDT</b>\n"
        f"📋 Tipo:        <code>{tipo}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷️ Comprado em: <code>{preco_compra:.6f} USDT</code>\n"
        f"{emoji_pnl} P&amp;L:        <b>{pnl_pct}  ({pnl_usdt})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 <i>{_horario()}</i>"
    )


def notificar_take_profit(ativo: str, quantidade: float, preco: float, preco_compra: float, nivel: int, pct_meta: float):
    valor_total = quantidade * preco
    pnl_pct, pnl_usdt, _ = _pnl_info(preco, preco_compra, quantidade)
    estrelas = "⭐" * min(nivel, 3)

    _send(
        f"🎯 <b>TAKE PROFIT #{nivel}  +{pct_meta:.0f}%  {estrelas}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Par:         <b>{ativo}</b>\n"
        f"🔢 Quantidade:  <b>{quantidade:.4f}</b>\n"
        f"💵 Preço:       <b>{preco:.6f} USDT</b>\n"
        f"💰 Total:       <b>{valor_total:.4f} USDT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷️ Comprado em: <code>{preco_compra:.6f} USDT</code>\n"
        f"📈 Lucro:       <b>{pnl_pct}  ({pnl_usdt})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 <i>{_horario()}</i>"
    )


def notificar_stop_loss(ativo: str, quantidade: float, preco: float, preco_compra: float):
    valor_total = quantidade * preco
    pnl_pct, pnl_usdt, _ = _pnl_info(preco, preco_compra, quantidade)

    _send(
        f"🚨 <b>STOP LOSS ATIVADO</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Par:         <b>{ativo}</b>\n"
        f"🔢 Quantidade:  <b>{quantidade:.4f}</b>\n"
        f"💵 Preço:       <b>{preco:.6f} USDT</b>\n"
        f"💰 Total:       <b>{valor_total:.4f} USDT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷️ Comprado em: <code>{preco_compra:.6f} USDT</code>\n"
        f"📉 Prejuizo:    <b>{pnl_pct}  ({pnl_usdt})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 <i>{_horario()}</i>"
    )


def notificar_trailing_stop(ativo: str, quantidade: float, preco: float, preco_compra: float):
    valor_total = quantidade * preco
    pnl_pct, pnl_usdt, positivo = _pnl_info(preco, preco_compra, quantidade)
    emoji_res = "✅ LUCRO" if positivo else "❌ PREJUIZO"
    emoji_pnl = "📈" if positivo else "📉"

    _send(
        f"🔶 <b>TRAILING STOP  —  {emoji_res}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Par:         <b>{ativo}</b>\n"
        f"🔢 Quantidade:  <b>{quantidade:.4f}</b>\n"
        f"💵 Preço:       <b>{preco:.6f} USDT</b>\n"
        f"💰 Total:       <b>{valor_total:.4f} USDT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷️ Comprado em: <code>{preco_compra:.6f} USDT</code>\n"
        f"{emoji_pnl} P&amp;L:        <b>{pnl_pct}  ({pnl_usdt})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 <i>{_horario()}</i>"
    )


def notificar_ordem(ativo, tipo, quantidade, preco):
    _send(
        f"📊 <b>ORDEM EXECUTADA</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Ativo:       <b>{ativo}</b>\n"
        f"📋 Tipo:        <b>{tipo}</b>\n"
        f"🔢 Quantidade:  <b>{quantidade}</b>\n"
        f"💵 Preço:       <b>{preco}</b>\n"
        f"🕐 <i>{_horario()}</i>"
    )

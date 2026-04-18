"""
Notifier — sends alerts via Telegram.
Falls back to log-only if token/chat_id not configured.
"""

import logging
import requests
from modules.config_loader import CONFIG

logger = logging.getLogger("Notifier")

_TOKEN   = CONFIG['api'].get('telegram_bot_token', '')
_CHAT_ID = CONFIG['api'].get('telegram_chat_id', '')
_ENABLED = bool(_TOKEN and _CHAT_ID and 'YOUR_' not in _TOKEN)


def send(message: str):
    """Send a Telegram message (or just log if not configured)."""
    if not _ENABLED:
        logger.info(f"[NOTIFY] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": _CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


def send_reply(message: str, reply_to_message_id: int = None):
    """
    Send a new Telegram message, replying to a specific message if reply_to_message_id is given.
    This creates a proper threaded reply under the original signal message.
    """
    if not _ENABLED:
        logger.info(f"[NOTIFY-REPLY] {message}")
        return
    try:
        url     = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
        payload = {
            "chat_id":    _CHAT_ID,
            "text":       message,
            "parse_mode": "HTML",
        }
        if reply_to_message_id:
            payload["reply_to_message_id"]         = int(reply_to_message_id)
            payload["allow_sending_without_reply"] = True   # still sends even if original deleted
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.warning(f"Telegram send_reply failed: {e}")


def signal_alert(symbol: str, side: str, entry: float, sl: float, tp1: float, tp2: float, tp3: float,
                 pattern: str, score: int, mode: str):
    rr = round((tp1 - entry) / abs(entry - sl), 2) if entry != sl else 0
    emoji = "🟢" if side == "Long" else "🔴"
    tag = "📋 PAPER" if mode == "PAPER" else "💰 REAL"
    msg = (
        f"{emoji} <b>{tag} SIGNAL</b> — {symbol}\n"
        f"Side: <b>{side}</b> | Pattern: {pattern}\n"
        f"Score: {score} | R:R ≈ 1:{rr}\n\n"
        f"Entry: <code>{entry}</code>\n"
        f"SL:    <code>{sl}</code>\n"
        f"TP1:   <code>{tp1}</code>\n"
        f"TP2:   <code>{tp2}</code>\n"
        f"TP3:   <code>{tp3}</code>"
    )
    send(msg)


def trade_closed_alert(symbol: str, pnl: float, reason: str, mode: str, reply_to_message_id: int = None):
    emoji = "✅" if pnl >= 0 else "❌"
    tag = "📋 PAPER" if mode == "PAPER" else "💰 REAL"
    send_reply(
        f"{emoji} [{tag}] {symbol} CLOSED\nReason: {reason} | PnL: <b>${pnl:+.4f}</b>",
        reply_to_message_id=reply_to_message_id,
    )
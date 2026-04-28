"""
Notifier — sends alerts via Telegram.
Falls back to log-only if token/chat_id not configured.

Fix #5 (ops-hardening-2): semua HTTP request ke Telegram dirutekan via
`telegram_bot._tg()` agar berbagi rate-limit (429) handler + retry yang
sama dengan signal alert. Sebelumnya `notifier.send()` cuma `requests.post`
tanpa cek 429 — partial-fill / TP-hit notification silent-drop saat
Telegram throttle.
"""

import logging
from modules.config_loader import CONFIG
from modules.telegram_bot import _tg, normalize_chat_id

logger = logging.getLogger("Notifier")

_TOKEN   = CONFIG['api'].get('telegram_bot_token', '')
_CHAT_ID = CONFIG['api'].get('telegram_chat_id', '')
_ENABLED = bool(_TOKEN and _CHAT_ID and 'YOUR_' not in _TOKEN)


def _post(payload: dict):
    """Wrap _tg() so notifier failures degrade ke log, bukan crash caller."""
    try:
        data = _tg("sendMessage", _TOKEN, json=payload)
        if data is None:
            logger.warning("[Notifier] sendMessage returned None (network/timeout)")
        elif not data.get("ok", False):
            logger.warning(f"[Notifier] Telegram error: {data}")
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


def send(message: str):
    """Send a Telegram message (or just log if not configured)."""
    if not _ENABLED:
        logger.info(f"[NOTIFY] {message}")
        return
    _post({
        "chat_id":    normalize_chat_id(_CHAT_ID),
        "text":       message,
        "parse_mode": "HTML",
    })


def send_reply(message: str, reply_to_message_id: int = None):
    """
    Send a new Telegram message, replying to a specific message if
    reply_to_message_id is given. This creates a proper threaded reply
    under the original signal message.
    """
    if not _ENABLED:
        logger.info(f"[NOTIFY-REPLY] {message}")
        return

    payload = {
        "chat_id":    normalize_chat_id(_CHAT_ID),
        "text":       message,
        "parse_mode": "HTML",
    }
    if reply_to_message_id:
        payload["reply_to_message_id"]         = int(reply_to_message_id)
        payload["allow_sending_without_reply"] = True   # still sends even if original deleted
    _post(payload)


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

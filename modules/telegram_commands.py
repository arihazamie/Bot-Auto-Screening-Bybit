"""
telegram_commands.py — Telegram Bot Command Handler
=====================================================
Mendengarkan perintah dari user via long polling (getUpdates).
Berjalan sebagai daemon thread terpisah agar tidak block main loop.

Perintah yang tersedia:
  /start   — Salam + daftar perintah
  /help    — Daftar perintah
  /status  — Ringkasan posisi aktif + paper balance
  /trades  — Detail semua posisi aktif/pending
  /balance — Tampilkan paper balance saja
  /report  — Laporan 24 jam terakhir
  /pause   — Pause scanning (set flag)
  /resume  — Resume scanning
"""

import time
import logging
import threading
import requests
from datetime import datetime

from modules.config_loader import CONFIG
from modules.database import (
    get_paper_balance,
    get_active_trades_by_status,
    count_open_active_trades,
    get_closed_trades_last_24h,
    get_state,
    set_state,
)

logger = logging.getLogger("TelegramCmd")

_TOKEN   = CONFIG['api'].get('telegram_bot_token', '')
_CHAT_ID = str(CONFIG['api'].get('telegram_chat_id', '')).strip()
_BASE    = f"https://api.telegram.org/bot{_TOKEN}"
_ENABLED = bool(_TOKEN and _CHAT_ID and 'YOUR_' not in _TOKEN)

# Flag global untuk pause/resume scanning — dibaca oleh main.py
_paused = False


def is_paused() -> bool:
    """Dibaca oleh main.py untuk skip scan cycle jika paused."""
    return _paused


def _send(chat_id, text: str, reply_to: int = None):
    """Kirim balasan ke Telegram."""
    if not _ENABLED:
        return
    payload = {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "HTML",
    }
    if reply_to:
        payload["reply_to_message_id"]         = reply_to
        payload["allow_sending_without_reply"] = True
    try:
        requests.post(f"{_BASE}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        logger.warning(f"[TelegramCmd] Send failed: {e}")


def _authorized(chat_id) -> bool:
    """Hanya izinkan perintah dari chat_id yang dikonfigurasi."""
    return str(chat_id).strip() == _CHAT_ID.lstrip('-100').lstrip('-')  or \
           str(chat_id).strip() == _CHAT_ID


# ──────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ──────────────────────────────────────────────────────────────

def _cmd_start(chat_id, msg_id):
    text = (
        "🤖 <b>V8 Screening Bot — Aktif</b>\n\n"
        "Perintah yang tersedia:\n"
        "/status  — Posisi aktif + balance\n"
        "/trades  — Detail semua posisi\n"
        "/balance — Paper balance saja\n"
        "/report  — Laporan 24 jam terakhir\n"
        "/pause   — Pause scanning sinyal\n"
        "/resume  — Resume scanning\n"
        "/help    — Tampilkan pesan ini"
    )
    _send(chat_id, text, reply_to=msg_id)


def _cmd_balance(chat_id, msg_id):
    try:
        balance      = get_paper_balance()
        start_bal    = float(CONFIG['risk'].get('paper_balance', balance))
        risk_pct     = float(CONFIG['risk'].get('risk_percent', 0.01))
        open_trades  = get_active_trades_by_status(["OPEN", "OPEN_TPS_SET", "PENDING"])

        # Hitung total margin terpakai dari posisi aktif
        # Margin per trade = qty × entry_price / leverage
        used_margin = 0.0
        total_pos_value = 0.0
        for t in open_trades:
            try:
                qty   = float(t.get('quantity', 0))
                entry = float(t.get('entry_price', 0))
                lev   = float(t.get('leverage', 1))
                pos_val  = qty * entry
                margin   = pos_val / lev
                used_margin     += margin
                total_pos_value += pos_val
            except Exception:
                pass

        free_balance = balance - used_margin
        pnl_total    = balance - start_bal
        pnl_emoji    = "📈" if pnl_total >= 0 else "📉"
        margin_pct   = (used_margin / balance * 100) if balance > 0 else 0

        text = (
            f"💼 <b>Paper Balance — Live</b>\n\n"
            f"{'─' * 28}\n"
            f"💰 Total Balance   : <code>${balance:.2f}</code>\n"
            f"🔓 Free Balance    : <code>${free_balance:.2f}</code>\n"
            f"🔒 Used Margin     : <code>${used_margin:.2f}</code>  ({margin_pct:.1f}%)\n"
            f"📊 Position Value  : <code>${total_pos_value:.2f}</code>  (margin × lev)\n"
            f"{'─' * 28}\n"
            f"🎯 Risk/trade      : <code>{risk_pct*100:.1f}%</code> = "
            f"<code>${balance * risk_pct:.2f}</code> margin/posisi\n"
            f"📂 Posisi aktif    : <code>{len(open_trades)}</code>\n"
            f"{'─' * 28}\n"
            f"{pnl_emoji} Total PnL        : <code>${pnl_total:+.2f}</code>\n"
            f"💵 Balance awal    : <code>${start_bal:.2f}</code>"
        )
    except Exception as e:
        text = f"❌ Gagal ambil balance: {e}"
    _send(chat_id, text, reply_to=msg_id)


def _cmd_status(chat_id, msg_id):
    global _paused
    try:
        balance     = get_paper_balance()
        risk_pct    = float(CONFIG['risk'].get('risk_percent', 0.01))
        open_trades = get_active_trades_by_status(["OPEN", "OPEN_TPS_SET"])
        pending     = get_active_trades_by_status(["PENDING"])
        all_active  = open_trades + pending

        # ✅ Hitung margin terpakai & free balance
        used_margin     = 0.0
        total_pos_value = 0.0
        for t in all_active:
            try:
                qty    = float(t.get('quantity', 0))
                entry  = float(t.get('entry_price', 0))
                lev    = float(t.get('leverage', 1))
                pos_v  = qty * entry
                used_margin     += pos_v / lev
                total_pos_value += pos_v
            except Exception:
                pass

        free_balance = balance - used_margin
        margin_pct   = (used_margin / balance * 100) if balance > 0 else 0
        pause_tag    = "⏸ <b>SCANNING PAUSED</b>\n\n" if _paused else ""

        header = (
            f"{pause_tag}"
            f"📊 <b>Live Status</b>\n"
            f"{'─' * 26}\n"
            f"💰 Balance     : <code>${balance:.2f}</code>\n"
            f"🔓 Free        : <code>${free_balance:.2f}</code>\n"
            f"🔒 Margin used : <code>${used_margin:.2f}</code> ({margin_pct:.1f}%)\n"
            f"📊 Pos. Value  : <code>${total_pos_value:.2f}</code>\n"
            f"{'─' * 26}\n"
            f"Posisi aktif: <b>{len(all_active)}</b>\n\n"
        )

        lines = []
        for t in open_trades:
            side_icon = "🟢" if t['side'] == 'Long' else "🔴"
            lev       = float(t.get('leverage', 1))
            qty       = float(t.get('quantity', 0))
            entry     = float(t.get('entry_price', 0))
            pos_val   = qty * entry
            margin    = pos_val / lev
            sl_moved  = " 🔒BE" if t.get('is_sl_moved') else ""
            lines.append(
                f"{side_icon} <b>{t['symbol']}</b> {t['side']}{sl_moved}\n"
                f"  💵 Margin <code>${margin:.2f}</code> × {int(lev)}x = <code>${pos_val:.2f}</code>\n"
                f"  Entry: <code>{entry}</code> | SL: <code>{t['sl_price']}</code>"
            )

        for t in pending:
            lev     = float(t.get('leverage', 1))
            qty     = float(t.get('quantity', 0))
            entry   = float(t.get('entry_price', 0))
            pos_val = qty * entry
            margin  = pos_val / lev
            lines.append(
                f"⏳ <b>{t['symbol']}</b> {t['side']} — pending fill\n"
                f"  💵 Margin <code>${margin:.2f}</code> × {int(lev)}x = <code>${pos_val:.2f}</code>\n"
                f"  Target: <code>{entry}</code>"
            )

        body = "\n\n".join(lines) if lines else "Tidak ada posisi aktif saat ini."
        _send(chat_id, header + body, reply_to=msg_id)

    except Exception as e:
        _send(chat_id, f"❌ Error: {e}", reply_to=msg_id)


def _cmd_trades(chat_id, msg_id):
    try:
        statuses = ["PENDING", "OPEN", "OPEN_TPS_SET"]
        trades   = get_active_trades_by_status(statuses)

        if not trades:
            _send(chat_id, "📭 Tidak ada posisi aktif atau pending.", reply_to=msg_id)
            return

        msgs = []
        for t in trades:
            side_icon = "🟢" if t['side'] == 'Long' else "🔴"
            status    = t.get('status', '?')
            sl_tag    = " 🔒BE" if t.get('is_sl_moved') else ""
            lev       = t.get('leverage', '?')
            qty       = float(t.get('quantity', 0))
            entry     = float(t['entry_price'])
            sl        = float(t['sl_price'])

            try:
                ts = datetime.fromisoformat(str(t.get('created_at', ''))).strftime('%m/%d %H:%M')
            except Exception:
                ts = "???"

            msgs.append(
                f"{side_icon} <b>{t['symbol']}</b> {t['side']}{sl_tag} [{status}]\n"
                f"  📅 {ts} | ⚡ {lev}x | Qty: <code>{qty:.4f}</code>\n"
                f"  🎯 Entry: <code>{entry}</code>\n"
                f"  🛑 SL: <code>{sl}</code>\n"
                f"  TP1: <code>{t['tp1']}</code> | TP2: <code>{t['tp2']}</code> | TP3: <code>{t['tp3']}</code>"
            )

        header = f"📋 <b>Posisi Aktif ({len(trades)})</b>\n\n"
        _send(chat_id, header + "\n\n".join(msgs), reply_to=msg_id)
    except Exception as e:
        _send(chat_id, f"❌ Error: {e}", reply_to=msg_id)


def _cmd_report(chat_id, msg_id):
    try:
        trades  = get_closed_trades_last_24h()
        balance = get_paper_balance()

        if not trades:
            _send(chat_id, "📭 Tidak ada trade yang ditutup dalam 24 jam terakhir.", reply_to=msg_id)
            return

        pnls      = [float(t.get('pnl', 0)) for t in trades]
        total     = len(pnls)
        wins      = sum(1 for p in pnls if p > 0)
        losses    = total - wins
        pnl_sum   = sum(pnls)
        win_rate  = round((wins / total) * 100, 1) if total else 0
        best      = max(pnls)
        worst     = min(pnls)

        bar_wins   = "🟩" * wins
        bar_losses = "🟥" * losses

        text = (
            f"📊 <b>Laporan 24 Jam Terakhir</b>\n\n"
            f"Total Trade : <b>{total}</b>\n"
            f"W/L         : <b>{wins}W / {losses}L</b>  {bar_wins}{bar_losses}\n"
            f"Win Rate    : <code>{win_rate}%</code>\n\n"
            f"Total PnL   : <code>${pnl_sum:+.4f}</code>\n"
            f"Best Trade  : <code>${best:+.4f}</code>\n"
            f"Worst Trade : <code>${worst:+.4f}</code>\n\n"
            f"💼 Balance  : <code>${balance:.2f}</code>"
        )
    except Exception as e:
        text = f"❌ Error: {e}"
    _send(chat_id, text, reply_to=msg_id)


def _cmd_pause(chat_id, msg_id):
    global _paused
    _paused = True
    set_state('bot_paused', '1')
    logger.info("⏸ Bot scanning di-PAUSE via Telegram command")
    _send(chat_id, "⏸ <b>Scanning di-pause.</b>\nBot tidak akan proses sinyal baru.\nKirim /resume untuk lanjutkan.", reply_to=msg_id)


def _cmd_resume(chat_id, msg_id):
    global _paused
    _paused = False
    set_state('bot_paused', '0')
    logger.info("▶️ Bot scanning di-RESUME via Telegram command")
    _send(chat_id, "▶️ <b>Scanning dilanjutkan.</b>\nBot kembali aktif memproses sinyal.", reply_to=msg_id)


def _cmd_unknown(chat_id, msg_id, text):
    _send(
        chat_id,
        f"❓ Perintah <code>{text}</code> tidak dikenal.\nKirim /help untuk daftar perintah.",
        reply_to=msg_id,
    )


# ──────────────────────────────────────────────────────────────
# DISPATCHER
# ──────────────────────────────────────────────────────────────

_HANDLERS = {
    "/start":  _cmd_start,
    "/help":   _cmd_start,
    "/status": _cmd_status,
    "/trades": _cmd_trades,
    "/balance": _cmd_balance,
    "/report": _cmd_report,
    "/pause":  _cmd_pause,
    "/resume": _cmd_resume,
}


def _dispatch(update: dict):
    """Proses satu update dari Telegram."""
    msg = update.get('message') or update.get('edited_message')
    if not msg:
        return

    chat_id = msg.get('chat', {}).get('id')
    msg_id  = msg.get('message_id')
    text    = (msg.get('text') or '').strip()

    if not text.startswith('/'):
        return   # abaikan pesan biasa, hanya proses command

    if not _authorized(chat_id):
        logger.warning(f"[TelegramCmd] Unauthorized access from chat_id={chat_id}")
        _send(chat_id, "🚫 Akses tidak diizinkan.")
        return

    # Ambil command (ignore @botname suffix misal /status@MyBot)
    cmd = text.split()[0].split('@')[0].lower()
    logger.info(f"[TelegramCmd] Command: {cmd} dari chat_id={chat_id}")

    handler = _HANDLERS.get(cmd)
    if handler:
        try:
            handler(chat_id, msg_id)
        except Exception as e:
            logger.error(f"[TelegramCmd] Handler error [{cmd}]: {e}", exc_info=True)
            _send(chat_id, f"❌ Error saat proses perintah: {e}", reply_to=msg_id)
    else:
        _cmd_unknown(chat_id, msg_id, cmd)


# ──────────────────────────────────────────────────────────────
# POLLING LOOP
# ──────────────────────────────────────────────────────────────

def _poll_loop():
    """
    Long polling loop — terus minta update dari Telegram.
    Menggunakan offset agar update yang sudah diproses tidak diulang.
    """
    global _paused
    logger.info("🎧 TelegramCmd polling started — siap terima perintah")

    # Restore pause state dari DB (jika bot restart)
    if get_state('bot_paused') == '1':
        _paused = True
        logger.info("⏸ Bot dimulai dalam kondisi PAUSED (state dari DB)")

    offset = 0

    while True:
        try:
            resp = requests.get(
                f"{_BASE}/getUpdates",
                params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                timeout=40,
            )
            data = resp.json()

            if not data.get('ok'):
                logger.warning(f"[TelegramCmd] getUpdates error: {data.get('description')}")
                time.sleep(5)
                continue

            updates = data.get('result', [])
            for upd in updates:
                offset = upd['update_id'] + 1   # advance offset agar tidak diproses ulang
                try:
                    _dispatch(upd)
                except Exception as e:
                    logger.error(f"[TelegramCmd] Dispatch error: {e}", exc_info=True)

        except requests.exceptions.Timeout:
            # Normal — long poll expired, loop ulang
            continue
        except Exception as e:
            logger.error(f"[TelegramCmd] Poll error: {e}")
            time.sleep(5)


# ──────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ──────────────────────────────────────────────────────────────

def start_command_listener():
    """
    Jalankan command listener sebagai daemon thread.
    Panggil dari main.py saat startup.
    """
    if not _ENABLED:
        logger.warning("[TelegramCmd] Telegram tidak dikonfigurasi — command listener tidak dijalankan")
        return None

    t = threading.Thread(
        target=_poll_loop,
        name="TelegramCmd",
        daemon=True,
    )
    t.start()
    logger.info("🧵 TelegramCmd daemon thread launched")
    return t
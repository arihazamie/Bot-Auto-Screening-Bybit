"""
manual_notifier.py — Format & throttle Telegram notifications for the manual
position assistant. Dry-run capable: in dry mode prints to stdout/log instead
of calling Telegram.

Tugas utama:
  • Susun pesan posisi+advice yang ringkas tapi informatif (HTML).
  • De-dup: jangan kirim "hold" berulang-ulang. Kirim hanya kalau:
      - posisi baru terdeteksi
      - bias AI berubah dibanding notif sebelumnya
      - tp_recommendation.action atau sl_recommendation.action berubah
      - heartbeat: setiap N menit (default 30) walau tidak ada perubahan
  • Posisi closed → kirim summary PnL.

Tidak pakai DB. State terakhir per-posisi disimpan di memori process; cocok
untuk bot single-instance. Kalau process restart, notif "ulang" satu kali
yang ringan (acceptable trade-off — tidak crash apapun).
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("ManualNotifier")


# ────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ────────────────────────────────────────────────────────────────────────────

def _fmt_price(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f == 0:
        return "—"
    if f < 0.001:
        return f"{f:.8f}".rstrip("0").rstrip(".")
    if f < 1:
        return f"{f:.6f}".rstrip("0").rstrip(".")
    if f < 100:
        return f"{f:.4f}".rstrip("0").rstrip(".")
    return f"{f:,.2f}"


def _bias_icon(bias: str) -> str:
    return {
        "bullish_strong": "🟢🟢",
        "bullish":        "🟢",
        "neutral":        "⚪",
        "bearish":        "🔴",
        "bearish_strong": "🔴🔴",
    }.get((bias or "neutral").lower(), "⚪")


def _side_icon(side: str) -> str:
    return "🚀 LONG" if side == "Long" else "🔻 SHORT"


def _tp_action_label(action: str) -> str:
    return {
        "take_partial_now": "🎯 TAKE PARTIAL",
        "hold":             "⏸ HOLD",
        "scale_out_soon":   "🪜 SCALE OUT SOON",
    }.get(action, action)


def _sl_action_label(action: str) -> str:
    return {
        "move_to_be":     "🛡 MOVE TO BE",
        "move_to_price":  "🛡 MOVE SL",
        "tighten":        "🪤 TIGHTEN",
        "hold":           "⏸ HOLD",
    }.get(action, action)


def _regime_line(ctx: dict) -> str:
    # Label TF dibaca dari ctx supaya konsisten dengan tf_entry/tf_trend yang
    # user konfigurasi (tidak hardcode 15m/1h).
    tf_entry = ctx.get("tf_entry", "entry")
    tf_trend = ctx.get("tf_trend", "trend")
    r_entry = (ctx.get("regime_entry") or {})
    r_trend = (ctx.get("regime_trend") or {})
    pieces = []
    if r_trend.get("label"):
        pieces.append(
            f"{tf_trend} <code>{r_trend['label']}</code> ADX <code>{r_trend.get('adx', 0):.1f}</code>"
        )
    if r_entry.get("label"):
        pieces.append(
            f"{tf_entry} <code>{r_entry['label']}</code> ADX <code>{r_entry.get('adx', 0):.1f}</code>"
        )
    if "rsi_entry" in ctx:
        pieces.append(f"RSI({tf_entry}) <code>{ctx['rsi_entry']:.0f}</code>")
    if "funding_rate" in ctx:
        pieces.append(f"funding <code>{ctx['funding_rate']*100:+.4f}%</code>")
    return " · ".join(pieces) if pieces else "—"


def render_position_message(
    position,
    market_ctx: dict,
    advice: dict,
    dry_run: bool = False,
) -> str:
    """Susun HTML message untuk satu posisi + advice."""
    sym       = position.symbol
    side_lbl  = _side_icon(position.side)
    pnl_emoji = "📈" if position.unrealised_pnl_pct >= 0 else "📉"
    pnl_str   = f"{position.unrealised_pnl_pct:+.2f}%"

    bias = advice.get("bias", "neutral")

    tp = advice.get("tp_recommendation", {}) or {}
    sl = advice.get("sl_recommendation", {}) or {}

    SEP  = "━━━━━━━━━━━━━━━━━━━━━━━"
    SEP2 = "─────────────────────────"

    head_tag = "🧪 DRY-RUN" if dry_run else "🤖 AI ADVICE"

    tp_price_part = ""
    if float(tp.get("suggested_price", 0) or 0) > 0:
        tp_price_part = f" @ <code>{_fmt_price(tp['suggested_price'])}</code>"
    tp_pct_part = ""
    if int(tp.get("suggested_close_pct", 0) or 0) > 0:
        tp_pct_part = f" {int(tp['suggested_close_pct'])}%"

    sl_price_part = ""
    if float(sl.get("suggested_sl", 0) or 0) > 0:
        sl_price_part = f" → <code>{_fmt_price(sl['suggested_sl'])}</code>"

    src = advice.get("_source", "?")
    cached = " (cached)" if advice.get("_cached") else ""

    text = (
        f"{SEP}\n"
        f"{head_tag} · <b>{sym}</b>\n"
        f"{SEP}\n"
        f"{side_lbl} · qty <code>{position.size}</code> · lev <code>{position.leverage:.0f}x</code>\n"
        f"📍 Entry <code>{_fmt_price(position.entry_price)}</code>  "
        f"{pnl_emoji} Mark <code>{_fmt_price(position.mark_price)}</code> "
        f"<i>({pnl_str})</i>\n"
        f"💧 Liq <code>{_fmt_price(position.liq_price)}</code>\n"
        f"{SEP2}\n"
        f"📡 <b>Market</b>: {_regime_line(market_ctx)}\n"
        f"{_bias_icon(bias)} <b>AI Bias</b>: <code>{bias}</code>\n"
        f"{SEP2}\n"
        f"💡 <b>TP</b>: {_tp_action_label(tp.get('action','hold'))}"
        f"{tp_pct_part}{tp_price_part}\n"
        f"   <i>{tp.get('reason','—')}</i>\n"
        f"🛡 <b>SL</b>: {_sl_action_label(sl.get('action','hold'))}{sl_price_part}\n"
        f"   <i>{sl.get('reason','—')}</i>\n"
        f"{SEP2}\n"
        f"📝 {advice.get('overall','—')}\n"
        f"<i>source: {src}{cached}</i>"
    )
    return text


def render_closed_message(prev_position) -> str:
    """Posisi hilang dari Bybit. Kirim summary tipis."""
    SEP = "━━━━━━━━━━━━━━━━━━━━━━━"
    side_lbl = _side_icon(prev_position.side)
    return (
        f"{SEP}\n"
        f"🏁 <b>POSITION CLOSED</b> · <b>{prev_position.symbol}</b>\n"
        f"{SEP}\n"
        f"{side_lbl} · entry <code>{_fmt_price(prev_position.entry_price)}</code>\n"
        f"Last seen mark <code>{_fmt_price(prev_position.mark_price)}</code> "
        f"(<i>{prev_position.unrealised_pnl_pct:+.2f}%</i>)\n"
        f"<i>Detected via Bybit position diff (read-only).</i>"
    )


def render_size_change_message(old, new) -> str:
    SEP = "━━━━━━━━━━━━━━━━━━━━━━━"
    delta = new.size - old.size
    arrow = "📉 reduced" if delta < 0 else "📈 added"
    return (
        f"{SEP}\n"
        f"✏️ <b>POSITION SIZE CHANGED</b> · <b>{new.symbol}</b>\n"
        f"{SEP}\n"
        f"{_side_icon(new.side)} · {arrow} <code>{abs(delta):.6f}</code>\n"
        f"<code>{old.size}</code> → <code>{new.size}</code>\n"
        f"Mark <code>{_fmt_price(new.mark_price)}</code> "
        f"(<i>{new.unrealised_pnl_pct:+.2f}%</i>)"
    )


# ────────────────────────────────────────────────────────────────────────────
# Sender
# ────────────────────────────────────────────────────────────────────────────

class ManualNotifier:
    """
    Bungkus pengiriman + dedup. Tidak ada side-effect saat dry_run=True.
    """

    def __init__(self, dry_run: bool = True, heartbeat_minutes: int = 30):
        self._dry        = bool(dry_run)
        self._hb_seconds = max(60, int(heartbeat_minutes) * 60)
        # last sent state per position key
        # value: {"bias", "tp_action", "sl_action", "ts", "msg_id"}
        self._last: dict[str, dict[str, Any]] = {}

    # ────────────────────────────────────────────────────────────
    # Decision: should we send?
    # ────────────────────────────────────────────────────────────
    def _should_send(self, key: str, advice: dict) -> tuple[bool, str]:
        bias       = advice.get("bias", "neutral")
        tp_action  = (advice.get("tp_recommendation", {}) or {}).get("action", "hold")
        sl_action  = (advice.get("sl_recommendation", {}) or {}).get("action", "hold")
        prev = self._last.get(key)

        if prev is None:
            return True, "first_observation"
        if prev.get("bias") != bias:
            return True, f"bias_changed:{prev.get('bias')}->{bias}"
        if prev.get("tp_action") != tp_action:
            return True, f"tp_changed:{prev.get('tp_action')}->{tp_action}"
        if prev.get("sl_action") != sl_action:
            return True, f"sl_changed:{prev.get('sl_action')}->{sl_action}"
        if (time.time() - float(prev.get("ts", 0))) >= self._hb_seconds:
            return True, "heartbeat"
        return False, "no_change"

    def _record(self, key: str, advice: dict, msg_id: int | None = None) -> None:
        self._last[key] = {
            "bias":      advice.get("bias", "neutral"),
            "tp_action": (advice.get("tp_recommendation", {}) or {}).get("action", "hold"),
            "sl_action": (advice.get("sl_recommendation", {}) or {}).get("action", "hold"),
            "ts":        time.time(),
            "msg_id":    msg_id,
        }

    def forget(self, key: str) -> int | None:
        """Hapus state setelah posisi closed. Return msg_id lama (untuk reply_to)."""
        prev = self._last.pop(key, None)
        return (prev or {}).get("msg_id")

    # ────────────────────────────────────────────────────────────
    # Public send hooks
    # ────────────────────────────────────────────────────────────
    def notify_position_advice(self, position, market_ctx, advice) -> bool:
        """
        Return True kalau pesan benar-benar dikirim/ditampilkan, False kalau
        diskip karena dedup.
        """
        key = position.key()
        ok, why = self._should_send(key, advice)
        if not ok:
            logger.debug(f"notify skip [{key}] reason={why}")
            return False

        text = render_position_message(position, market_ctx, advice, dry_run=self._dry)
        msg_id = self._send_or_log(text, prefix=f"ADVICE [{key}] ({why})")
        self._record(key, advice, msg_id=msg_id)
        return True

    def notify_position_closed(self, prev_position) -> None:
        key = prev_position.key()
        reply_to = self.forget(key)
        text = render_closed_message(prev_position)
        self._send_or_log(text, prefix=f"CLOSED [{key}]", reply_to=reply_to)

    def notify_size_changed(self, old, new) -> None:
        key = new.key()
        reply_to = (self._last.get(key) or {}).get("msg_id")
        text = render_size_change_message(old, new)
        self._send_or_log(text, prefix=f"SIZE [{key}]", reply_to=reply_to)

    def notify_status(self, text: str) -> None:
        """Generic status (startup banner, errors, etc)."""
        self._send_or_log(text, prefix="STATUS")

    # ────────────────────────────────────────────────────────────
    # Transport
    # ────────────────────────────────────────────────────────────
    def _send_or_log(self, text: str, prefix: str, reply_to: int | None = None) -> int | None:
        if self._dry:
            # Strip simple HTML for terminal readability
            plain = _strip_html(text)
            for line in plain.splitlines():
                logger.info(f"🟡 [DRY {prefix}] {line}")
            return None

        # Real send via existing telegram_bot._tg() — same retry/rate-limit handling
        try:
            from modules.config_loader import CONFIG
            from modules.telegram_bot import _tg, normalize_chat_id

            token   = CONFIG["api"].get("telegram_bot_token", "")
            chat_id = normalize_chat_id(CONFIG["api"].get("telegram_chat_id", ""))
            if not token or not chat_id:
                logger.warning("telegram not configured — falling back to log")
                logger.info(f"[{prefix}] {_strip_html(text)}")
                return None

            payload = {
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_to:
                payload["reply_to_message_id"]         = int(reply_to)
                payload["allow_sending_without_reply"] = True

            resp = _tg("sendMessage", token, json=payload)
            if isinstance(resp, dict) and resp.get("ok"):
                return int((resp.get("result") or {}).get("message_id", 0)) or None
            logger.warning(f"telegram send failed: {resp}")
            return None
        except Exception as e:
            logger.warning(f"telegram send exception: {type(e).__name__}: {e}")
            return None


# ────────────────────────────────────────────────────────────────────────────

def _strip_html(s: str) -> str:
    """Very lightweight HTML strip for log output (not security-sensitive)."""
    import re
    return re.sub(r"<[^>]+>", "", s)

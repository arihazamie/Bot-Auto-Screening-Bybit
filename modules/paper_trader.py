"""
Paper Trader — simulates order fills, TP hits, SL hits, and PnL
without placing any real orders on Bybit.

PnL Formula (BENAR — seperti exchange asli):
    margin         = balance * risk%               ← uang yang dipertaruhkan
    position_value = margin × leverage             ← notional size
    quantity       = position_value / entry_price  ← jumlah koin (sudah bake-in leverage)
    pnl (Long)     = (exit - entry) × quantity     ← TIDAK perlu × leverage lagi!
    pnl (Short)    = (entry - exit) × quantity

Contoh — Balance $100, Risk 1%, Lev 50x, LINK entry 9.6084, SL 9.4868:
    margin   = $100 × 1%     = $1.00
    pos val  = $1 × 50       = $50.00
    qty      = $50 / 9.6084  = 5.203 LINK
    pnl (SL) = (9.4868 - 9.6084) × 5.203 = -$0.63  ✅ (bukan -$27.62!)
"""

import logging
from datetime import datetime
from modules.database import (
    get_paper_balance,
    update_paper_balance,
    update_active_trade,
    get_active_trades_by_status,
)
from modules.notifier import send_reply

logger = logging.getLogger("PaperTrader")

# ─── UI Helpers ───────────────────────────────────────────────────────────────

SEP  = "━━━━━━━━━━━━━━━━━━━━━━━"
SEP2 = "─────────────────────────"


def _fp(v):
    """Format price — strips trailing zeros, adapts precision."""
    f = float(v)
    if f == 0:       return "0"
    if f < 0.001:    return f"{f:.8f}".rstrip('0').rstrip('.')
    if f < 1:        return f"{f:.6f}".rstrip('0').rstrip('.')
    if f < 100:      return f"{f:.4f}".rstrip('0').rstrip('.')
    return f"{f:.2f}"


def _pct(a, b):
    """% change from a to b  →  e.g. +1.27%"""
    if float(a) == 0: return ""
    return f"{((float(b) - float(a)) / float(a)) * 100:+.2f}%"


def _side_label(side: str) -> str:
    return "🚀 LONG" if side == 'Long' else "🔻 SHORT"


# ─── Core PnL Formula (FIXED) ─────────────────────────────────────────────────

def _calc_pnl(side: str, entry: float, exit_price: float, qty: float, leverage: int = 1) -> float:
    """
    Hitung PnL realized dengan fee.

    ⚠️  BUG FIX: qty sudah mengandung leverage karena:
        qty = (balance × risk% × leverage) / entry
        → TIDAK perlu dikalikan leverage lagi!
        (Versi lama: return round(raw_pnl * leverage, 4)  ← SALAH)

    Parameter leverage dibiarkan untuk backward-compat tapi tidak digunakan.
    """
    if side == 'Long':
        raw_pnl = (exit_price - entry) * qty
    else:
        raw_pnl = (entry - exit_price) * qty

    # Bybit taker fee 0.055% open + 0.055% close
    fee = (entry * qty * 0.00055) + (exit_price * qty * 0.00055)
    return round(raw_pnl - fee, 4)


# ─── PAPER EXECUTE (Entry Fill) ───────────────────────────────────────────────

def paper_execute(trade: dict, current_price: float) -> bool:
    """
    Simulasi fill entry untuk PENDING paper trade.
    Returns True jika terisi, False jika masih menunggu.
    """
    side  = trade['side']
    entry = float(trade['entry_price'])
    tg_id = trade.get('telegram_msg_id')

    filled = (side == 'Long'  and current_price <= entry) or \
             (side == 'Short' and current_price >= entry)

    if not filled:
        return False

    update_active_trade(trade['id'], {"status": "OPEN", "entry_price": current_price})
    logger.info(f"📋 [PAPER] Entry filled: {trade['symbol']} {side} @ {current_price}")

    # Hitung info posisi untuk notifikasi
    qty    = float(trade.get('quantity', 0))
    lev    = int(trade.get('leverage', 1))
    # Estimasi margin dari qty dan leverage
    pos_val = current_price * qty
    margin  = pos_val / lev if lev > 0 else pos_val

    msg = (
        f"{SEP}\n"
        f"📋 <b>[PAPER] Entry Filled</b>\n"
        f"{SEP}\n\n"
        f"📌 <b>{trade['symbol']}</b>  {_side_label(side)}\n"
        f"💰 Fill    <code>{_fp(current_price)}</code>\n\n"
        f"🛑 Stop    <code>{_fp(trade['sl_price'])}</code>   <i>({_pct(current_price, trade['sl_price'])})</i>\n"
        f"🎯 TP1     <code>{_fp(trade['tp1'])}</code>   <i>({_pct(current_price, trade['tp1'])})</i>\n"
        f"🎯 TP2     <code>{_fp(trade['tp2'])}</code>\n"
        f"🎯 TP3     <code>{_fp(trade['tp3'])}</code>   <i>({_pct(current_price, trade['tp3'])})</i>\n\n"
        f"{SEP2}\n"
        f"💼 Margin  <code>${margin:.2f}</code>  ·  Pos  <code>${pos_val:.2f}</code>  ·  Lev  <code>{lev}x</code>\n"
        f"<i>🕐 {datetime.now().strftime('%H:%M:%S')}</i>"
    )
    send_reply(msg, reply_to_message_id=tg_id)
    return True


# ─── PAPER MONITOR (TP / SL Check) ───────────────────────────────────────────

def paper_monitor(trade: dict, current_price: float):
    """
    Cek apakah TP atau SL sudah kena untuk open paper trade.
    Handles breakeven move setelah TP1.
    """
    t_id     = trade['id']
    sym      = trade['symbol']
    side     = trade['side']
    entry    = float(trade['entry_price'])
    sl       = float(trade['sl_price'])
    tp1      = float(trade['tp1'])
    tp2      = float(trade['tp2'])
    tp3      = float(trade['tp3'])
    qty      = float(trade['quantity'])
    lev      = int(trade.get('leverage', 1))
    tg_id    = trade.get('telegram_msg_id')
    sl_moved = trade.get('is_sl_moved', False)

    effective_sl = entry if sl_moved else sl

    if side == 'Long':
        sl_hit  = current_price <= effective_sl
        tp1_hit = current_price >= tp1
        tp2_hit = current_price >= tp2
        tp3_hit = current_price >= tp3
    else:
        sl_hit  = current_price >= effective_sl
        tp1_hit = current_price <= tp1
        tp2_hit = current_price <= tp2
        tp3_hit = current_price <= tp3

    # ── SL Hit ──────────────────────────────────────────────────────────────
    if sl_hit:
        pnl    = _calc_pnl(side, entry, effective_sl, qty, lev)
        reason = "Breakeven SL" if sl_moved else "Stop Loss"
        _close_paper_trade(t_id, sym, side, entry, effective_sl, pnl, reason, tg_id)
        return

    # ── TP3 Hit (full close) ─────────────────────────────────────────────────
    if tp3_hit:
        pnl = _calc_pnl(side, entry, tp3, qty, lev)
        _close_paper_trade(t_id, sym, side, entry, tp3, pnl, "TP3", tg_id)
        return

    # ── TP2 Hit (partial, notify once) ───────────────────────────────────────
    if tp2_hit and not trade.get('_tp2_logged'):
        partial_pnl = _calc_pnl(side, entry, tp2, qty * 0.30, lev)

        update_active_trade(t_id, {"status": "OPEN_TPS_SET", "_tp2_logged": True})
        logger.info(f"🎯 [PAPER] {sym} TP2 hit @ {current_price}")

        msg = (
            f"{SEP}\n"
            f"🎯 <b>[PAPER] TP2 Hit</b>\n"
            f"{SEP}\n\n"
            f"📌 <b>{sym}</b>  {_side_label(side)}\n"
            f"📈 Price  <code>{_fp(current_price)}</code>  →  TP2  <code>{_fp(tp2)}</code>  <i>({_pct(entry, tp2)})</i>\n\n"
            f"💰 Partial PnL  <code>${partial_pnl:+.4f}</code>  <i>(30% posisi)</i>\n"
            f"⏳ Holding 40% ke TP3 @ <code>{_fp(tp3)}</code>\n\n"
            f"<i>🕐 {datetime.now().strftime('%H:%M:%S')}</i>"
        )
        send_reply(msg, reply_to_message_id=tg_id)

    # ── TP1 Hit → Breakeven ──────────────────────────────────────────────────
    if tp1_hit and not sl_moved:
        partial_pnl = _calc_pnl(side, entry, tp1, qty * 0.30, lev)

        update_active_trade(t_id, {"is_sl_moved": True})
        logger.info(f"♻️  [PAPER] {sym} TP1 hit — moving SL to breakeven @ {entry}")

        msg = (
            f"{SEP}\n"
            f"♻️ <b>[PAPER] TP1 Hit — Breakeven Set</b>\n"
            f"{SEP}\n\n"
            f"📌 <b>{sym}</b>  {_side_label(side)}\n"
            f"📈 Price  <code>{_fp(current_price)}</code>  →  TP1  <code>{_fp(tp1)}</code>  <i>({_pct(entry, tp1)})</i>\n\n"
            f"💰 Partial PnL  <code>${partial_pnl:+.4f}</code>  <i>(30% posisi)</i>\n"
            f"🔒 Stop dipindah → Breakeven @ <code>{_fp(entry)}</code>\n"
            f"⏳ Holding 70% ke TP2 / TP3\n\n"
            f"<i>🕐 {datetime.now().strftime('%H:%M:%S')}</i>"
        )
        send_reply(msg, reply_to_message_id=tg_id)


# ─── CLOSE TRADE ──────────────────────────────────────────────────────────────

def _close_paper_trade(
    trade_id: int,
    symbol: str,
    side: str,
    entry: float,
    exit_price: float,
    pnl: float,
    reason: str,
    telegram_msg_id: int = None,
):
    """Tutup paper trade, update balance, kirim notifikasi Telegram."""
    balance     = get_paper_balance()
    new_balance = balance + pnl
    update_paper_balance(new_balance)
    update_active_trade(trade_id, {"status": "CLOSED", "pnl": pnl})

    is_win   = pnl >= 0
    emoji    = "✅" if is_win else "❌"
    result   = "PROFIT 🟢" if is_win else "LOSS 🔴"
    bal_pct  = (pnl / balance * 100) if balance > 0 else 0
    is_sl    = "SL" in reason or "Stop" in reason
    exit_ico = "🛑" if is_sl else "🎯"

    logger.info(
        f"{emoji} [PAPER] {symbol} CLOSED ({reason}) | "
        f"Entry: {entry} → Exit: {exit_price} | "
        f"PnL: ${pnl:+.4f} | Balance: ${new_balance:.2f}"
    )

    msg = (
        f"{SEP}\n"
        f"{emoji} <b>[PAPER] Trade Closed</b>\n"
        f"{SEP}\n\n"
        f"📌 <b>{symbol}</b>  {_side_label(side)}\n"
        f"📊 Closed by   <b>{reason}</b>\n\n"
        f"📍 Entry    <code>{_fp(entry)}</code>\n"
        f"{exit_ico} Exit     <code>{_fp(exit_price)}</code>   <i>({_pct(entry, exit_price)})</i>\n\n"
        f"{'━'*11}\n"
        f"💰 PnL      <code>${pnl:+.4f}</code>   <b>{result}</b>\n"
        f"💼 Balance  <code>${new_balance:.2f}</code>   <i>({bal_pct:+.2f}%)</i>\n"
        f"{'━'*11}\n\n"
        f"<i>🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
    )
    send_reply(msg, reply_to_message_id=telegram_msg_id)
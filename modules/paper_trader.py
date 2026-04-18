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


def _pct_price(a, b) -> str:
    """
    Persentase perubahan harga dari a ke b.
    Dipakai untuk menunjukkan jarak TP/SL dari entry.
    Contoh: entry=100, tp1=105 → +5.00%
    """
    a, b = float(a), float(b)
    if a == 0:
        return ""
    return f"{((b - a) / a) * 100:+.2f}%"


# Alias pendek untuk backward compat
_pct = _pct_price


def _roi_on_margin(pnl: float, margin: float) -> str:
    """
    ROI relatif terhadap margin (modal yang dipertaruhkan).
    Ini adalah angka yang paling relevan untuk trader leverage.

    Contoh:
      margin=$1, leverage=50x, entry=9.6, exit=9.7 (Long)
      qty    = $1 × 50 / 9.6 = 5.208
      pnl    = (9.7 - 9.6) × 5.208 = $0.5208
      ROI    = $0.5208 / $1 = +52.08%  (= price_pct × leverage = 1.04% × 50)
    """
    if margin <= 0:
        return ""
    return f"{(pnl / margin) * 100:+.2f}%"


def _roi_on_balance(pnl: float, balance: float) -> str:
    """
    ROI relatif terhadap total balance.
    Contoh: balance=$100, pnl=$0.52 → +0.52%
    """
    if balance <= 0:
        return ""
    return f"{(pnl / balance) * 100:+.2f}%"


def _calc_margin(entry: float, qty: float, lev: int) -> float:
    """
    Hitung margin dari entry, qty, dan leverage.
    Rumus: margin = (entry × qty) / leverage
    Ini kebalikan dari: qty = (margin × leverage) / entry
    """
    if lev <= 0:
        return entry * qty
    return (entry * qty) / lev


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

    qty     = float(trade.get('quantity', 0))
    lev     = int(trade.get('leverage', 1))
    margin  = _calc_margin(current_price, qty, lev)
    pos_val = current_price * qty

    # Hitung risk % dari setiap level ke entry (price % dan ROI on margin)
    sl_price_pct = _pct_price(current_price, trade['sl_price'])
    tp1_price_pct = _pct_price(current_price, trade['tp1'])
    tp3_price_pct = _pct_price(current_price, trade['tp3'])

    # ROI on margin per level
    sl_pnl  = _calc_pnl(side, current_price, float(trade['sl_price']), qty, lev)
    tp1_pnl = _calc_pnl(side, current_price, float(trade['tp1']),      qty, lev)
    tp3_pnl = _calc_pnl(side, current_price, float(trade['tp3']),      qty, lev)

    sl_roi  = _roi_on_margin(sl_pnl,  margin)
    tp1_roi = _roi_on_margin(tp1_pnl, margin)
    tp3_roi = _roi_on_margin(tp3_pnl, margin)

    msg = (
        f"{SEP}\n"
        f"📋 <b>[PAPER] Entry Filled</b>\n"
        f"{SEP}\n\n"
        f"📌 <b>{trade['symbol']}</b>  {_side_label(side)}\n"
        f"💰 Fill    <code>{_fp(current_price)}</code>\n\n"
        f"🛑 Stop    <code>{_fp(trade['sl_price'])}</code>  "
            f"<i>({sl_price_pct} · ROI {sl_roi})</i>\n"
        f"🎯 TP1     <code>{_fp(trade['tp1'])}</code>  "
            f"<i>({tp1_price_pct} · ROI {tp1_roi})</i>\n"
        f"🎯 TP2     <code>{_fp(trade['tp2'])}</code>\n"
        f"🎯 TP3     <code>{_fp(trade['tp3'])}</code>  "
            f"<i>({tp3_price_pct} · ROI {tp3_roi})</i>\n\n"
        f"{SEP2}\n"
        f"💼 Margin  <code>${margin:.2f}</code>  ·  "
            f"Pos  <code>${pos_val:.2f}</code>  ·  "
            f"Lev  <code>{lev}x</code>\n"
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
        _close_paper_trade(t_id, sym, side, entry, effective_sl, pnl, reason, tg_id,
                           qty=qty, lev=lev)
        return

    # ── TP3 Hit (full close) ─────────────────────────────────────────────────
    if tp3_hit:
        pnl = _calc_pnl(side, entry, tp3, qty, lev)
        _close_paper_trade(t_id, sym, side, entry, tp3, pnl, "TP3", tg_id,
                           qty=qty, lev=lev)
        return

    # ── TP2 Hit (partial, notify once) ───────────────────────────────────────
    if tp2_hit and not trade.get('_tp2_logged'):
        partial_qty = qty * 0.30
        partial_pnl = _calc_pnl(side, entry, tp2, partial_qty, lev)
        full_margin  = _calc_margin(entry, qty, lev)
        partial_margin = full_margin * 0.30

        update_active_trade(t_id, {"status": "OPEN_TPS_SET", "_tp2_logged": True})
        logger.info(f"🎯 [PAPER] {sym} TP2 hit @ {current_price}")

        msg = (
            f"{SEP}\n"
            f"🎯 <b>[PAPER] TP2 Hit</b>\n"
            f"{SEP}\n\n"
            f"📌 <b>{sym}</b>  {_side_label(side)}\n"
            f"📈 TP2  <code>{_fp(tp2)}</code>  "
                f"<i>({_pct_price(entry, tp2)} dari entry)</i>\n\n"
            f"💰 Partial PnL   <code>${partial_pnl:+.4f}</code>  "
                f"<i>(30% posisi)</i>\n"
            f"📊 ROI on margin <code>{_roi_on_margin(partial_pnl, partial_margin)}</code>  "
                f"<i>(dari ${partial_margin:.2f})</i>\n"
            f"⏳ Holding 40% ke TP3 @ <code>{_fp(tp3)}</code>  "
                f"<i>({_pct_price(entry, tp3)})</i>\n\n"
            f"<i>🕐 {datetime.now().strftime('%H:%M:%S')}</i>"
        )
        send_reply(msg, reply_to_message_id=tg_id)

    # ── TP1 Hit → Breakeven ──────────────────────────────────────────────────
    if tp1_hit and not sl_moved:
        partial_qty    = qty * 0.30
        partial_pnl    = _calc_pnl(side, entry, tp1, partial_qty, lev)
        full_margin    = _calc_margin(entry, qty, lev)
        partial_margin = full_margin * 0.30

        update_active_trade(t_id, {"is_sl_moved": True})
        logger.info(f"♻️  [PAPER] {sym} TP1 hit — moving SL to breakeven @ {entry}")

        msg = (
            f"{SEP}\n"
            f"♻️ <b>[PAPER] TP1 Hit — Breakeven Set</b>\n"
            f"{SEP}\n\n"
            f"📌 <b>{sym}</b>  {_side_label(side)}\n"
            f"📈 TP1  <code>{_fp(tp1)}</code>  "
                f"<i>({_pct_price(entry, tp1)} dari entry)</i>\n\n"
            f"💰 Partial PnL   <code>${partial_pnl:+.4f}</code>  "
                f"<i>(30% posisi)</i>\n"
            f"📊 ROI on margin <code>{_roi_on_margin(partial_pnl, partial_margin)}</code>  "
                f"<i>(dari ${partial_margin:.2f})</i>\n"
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
    qty: float = 0.0,
    lev: int = 1,
):
    """Tutup paper trade, update balance, kirim notifikasi Telegram."""
    balance     = get_paper_balance()
    new_balance = balance + pnl
    update_paper_balance(new_balance)
    update_active_trade(trade_id, {"status": "CLOSED", "pnl": pnl})

    is_win   = pnl >= 0
    emoji    = "✅" if is_win else "❌"
    result   = "PROFIT 🟢" if is_win else "LOSS 🔴"
    is_sl    = "SL" in reason or "Stop" in reason
    exit_ico = "🛑" if is_sl else "🎯"

    # ── Perhitungan persentase yang benar ─────────────────────────────────
    # 1. Price % — seberapa jauh harga bergerak dari entry
    price_pct = _pct_price(entry, exit_price)

    # 2. ROI on margin — keuntungan/kerugian relatif terhadap modal yang dipertaruhkan
    #    Ini angka paling relevan: +52% artinya modal (margin) naik 52%
    margin    = _calc_margin(entry, qty, lev) if qty > 0 else 0.0
    roi_margin = _roi_on_margin(pnl, margin) if margin > 0 else ""

    # 3. ROI on balance — impact ke total akun
    roi_balance = _roi_on_balance(pnl, balance)

    logger.info(
        f"{emoji} [PAPER] {symbol} CLOSED ({reason}) | "
        f"Entry: {entry} → Exit: {exit_price} ({price_pct}) | "
        f"PnL: ${pnl:+.4f} | ROI/Margin: {roi_margin} | "
        f"Balance: ${new_balance:.2f} ({roi_balance})"
    )

    msg = (
        f"{SEP}\n"
        f"{emoji} <b>[PAPER] Trade Closed</b>\n"
        f"{SEP}\n\n"
        f"📌 <b>{symbol}</b>  {_side_label(side)}\n"
        f"📊 Closed by   <b>{reason}</b>\n\n"
        f"📍 Entry    <code>{_fp(entry)}</code>\n"
        f"{exit_ico} Exit     <code>{_fp(exit_price)}</code>  "
            f"<i>({price_pct} harga)</i>\n\n"
        f"{'━'*11}\n"
        f"💰 PnL           <code>${pnl:+.4f}</code>   <b>{result}</b>\n"
        f"📊 ROI/Margin    <code>{roi_margin}</code>  "
            f"<i>(dari ${margin:.2f}  ×{lev})</i>\n"
        f"💼 ROI/Balance   <code>{roi_balance}</code>  "
            f"→  <code>${new_balance:.2f}</code>\n"
        f"{'━'*11}\n\n"
        f"<i>🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
    )
    send_reply(msg, reply_to_message_id=telegram_msg_id)
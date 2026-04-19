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
    pnl (SL) = (9.4868 - 9.6084) × 5.203 = -$0.63

Partial TP Logic:
    TP1 hit → jual 30%, balance update realtime, SL → Breakeven (entry)
    TP2 hit → jual 30%, balance update realtime, SL → TP1 price
    TP3 hit → tutup sisa 40%, balance update, trade CLOSED

Cascade TP (skip detection):
    Jika harga loncat langsung ke TP3 (skip TP1/TP2), proses TP1 & TP2 partial
    terlebih dahulu (dengan harga TP masing-masing), baru tutup di TP3.
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
    a, b = float(a), float(b)
    if a == 0:
        return ""
    return f"{((b - a) / a) * 100:+.2f}%"

_pct = _pct_price


def _roi_on_margin(pnl: float, margin: float) -> str:
    if margin <= 0:
        return ""
    return f"{(pnl / margin) * 100:+.2f}%"


def _roi_on_balance(pnl: float, balance: float) -> str:
    if balance <= 0:
        return ""
    return f"{(pnl / balance) * 100:+.2f}%"


def _calc_margin(entry: float, qty: float, lev: int) -> float:
    if lev <= 0:
        return entry * qty
    return (entry * qty) / lev


def _side_label(side: str) -> str:
    return "🚀 LONG" if side == 'Long' else "🔻 SHORT"


# ─── Core PnL Formula ─────────────────────────────────────────────────────────

def _calc_pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    """
    Hitung PnL realized dengan fee.
    qty sudah mengandung leverage, TIDAK perlu dikalikan lagi.
    """
    if side == 'Long':
        raw_pnl = (exit_price - entry) * qty
    else:
        raw_pnl = (entry - exit_price) * qty

    # Bybit taker fee 0.055% open + 0.055% close
    fee = (entry * qty * 0.00055) + (exit_price * qty * 0.00055)
    return round(raw_pnl - fee, 4)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _remaining_qty(qty: float, tp1_logged: bool, tp2_logged: bool) -> float:
    """
    Sisa qty setelah partial TP:
      TP1 hit → jual 30% → sisa 70%
      TP2 hit → jual 30% lagi → sisa 40%
    """
    remaining = qty
    if tp1_logged:
        remaining -= qty * 0.30
    if tp2_logged:
        remaining -= qty * 0.30
    return round(remaining, 8)


def _effective_sl(trade: dict) -> float:
    """
    SL efektif berdasarkan trailing status:
      - Belum kena TP1  → SL original
      - TP1 hit         → SL = entry (breakeven)
      - TP2 hit         → SL = TP1 price
    """
    if trade.get('_tp2_logged'):
        return float(trade['tp1'])          # trail ke TP1
    if trade.get('is_sl_moved'):
        return float(trade['entry_price'])  # breakeven
    return float(trade['sl_price'])         # original


def _apply_partial_balance(pnl: float, label: str, symbol: str) -> float:
    """Update balance dengan partial PnL realtime. Returns new balance."""
    balance = get_paper_balance()
    new_balance = balance + pnl
    update_paper_balance(new_balance)
    logger.info(
        f"💰 [PAPER] {symbol} {label} partial — PnL: ${pnl:+.4f} "
        f"| Balance: ${balance:.2f} → ${new_balance:.2f}"
    )
    return new_balance


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

    sl_price_pct  = _pct_price(current_price, trade['sl_price'])
    tp1_price_pct = _pct_price(current_price, trade['tp1'])
    tp3_price_pct = _pct_price(current_price, trade['tp3'])

    sl_pnl  = _calc_pnl(side, current_price, float(trade['sl_price']), qty)
    tp1_pnl = _calc_pnl(side, current_price, float(trade['tp1']),      qty)
    tp3_pnl = _calc_pnl(side, current_price, float(trade['tp3']),      qty)

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


# ─── PARTIAL TP HANDLERS ──────────────────────────────────────────────────────

def _handle_tp1_partial(trade: dict, cascade: bool = False) -> float:
    """
    Partial close di TP1 (30% posisi).
    ✅ Update balance realtime
    ✅ Set SL ke Breakeven (entry)
    Returns partial PnL.
    """
    t_id   = trade['id']
    sym    = trade['symbol']
    side   = trade['side']
    entry  = float(trade['entry_price'])
    tp1    = float(trade['tp1'])
    tp2    = float(trade['tp2'])
    tp3    = float(trade['tp3'])
    qty    = float(trade['quantity'])
    lev    = int(trade.get('leverage', 1))
    tg_id  = trade.get('telegram_msg_id')

    partial_qty    = qty * 0.30
    partial_pnl    = _calc_pnl(side, entry, tp1, partial_qty)
    full_margin    = _calc_margin(entry, qty, lev)
    partial_margin = full_margin * 0.30

    # ✅ Balance update realtime
    new_balance = _apply_partial_balance(partial_pnl, "TP1", sym)

    # ✅ SL → Breakeven, flag TP1 processed
    update_active_trade(t_id, {
        "is_sl_moved": True,
        "_tp1_logged": True,
    })
    logger.info(f"♻️  [PAPER] {sym} TP1 → SL dipindah ke Breakeven @ {entry}")

    cascade_note = "\n⚡ <i>Cascade: harga skip ke TP2/TP3</i>" if cascade else ""

    msg = (
        f"{SEP}\n"
        f"♻️ <b>[PAPER] TP1 Hit — Breakeven Set</b>\n"
        f"{SEP}\n\n"
        f"📌 <b>{sym}</b>  {_side_label(side)}\n"
        f"📈 TP1  <code>{_fp(tp1)}</code>  "
            f"<i>({_pct_price(entry, tp1)} dari entry)</i>\n\n"
        f"💰 Partial PnL   <code>${partial_pnl:+.4f}</code>  <i>(30% posisi)</i>\n"
        f"📊 ROI on margin <code>{_roi_on_margin(partial_pnl, partial_margin)}</code>  "
            f"<i>(dari ${partial_margin:.2f})</i>\n"
        f"💼 Balance       <code>${new_balance:.2f}</code>\n"
        f"🔒 Stop → Breakeven @ <code>{_fp(entry)}</code>\n"
        f"⏳ Holding 70% ke TP2 / TP3{cascade_note}\n\n"
        f"<i>🕐 {datetime.now().strftime('%H:%M:%S')}</i>"
    )
    send_reply(msg, reply_to_message_id=tg_id)
    return partial_pnl


def _handle_tp2_partial(trade: dict, cascade: bool = False) -> float:
    """
    Partial close di TP2 (30% posisi).
    ✅ Update balance realtime
    ✅ Set SL ke TP1 price (trailing stop)
    Returns partial PnL.
    """
    t_id   = trade['id']
    sym    = trade['symbol']
    side   = trade['side']
    entry  = float(trade['entry_price'])
    tp1    = float(trade['tp1'])
    tp2    = float(trade['tp2'])
    tp3    = float(trade['tp3'])
    qty    = float(trade['quantity'])
    lev    = int(trade.get('leverage', 1))
    tg_id  = trade.get('telegram_msg_id')

    partial_qty    = qty * 0.30
    partial_pnl    = _calc_pnl(side, entry, tp2, partial_qty)
    full_margin    = _calc_margin(entry, qty, lev)
    partial_margin = full_margin * 0.30

    # ✅ Balance update realtime
    new_balance = _apply_partial_balance(partial_pnl, "TP2", sym)

    # ✅ SL → TP1 price (trail), flag TP2 processed
    update_active_trade(t_id, {
        "status": "OPEN_TPS_SET",
        "_tp2_logged": True,
        # effective_sl() akan baca tp1 field secara otomatis saat _tp2_logged=True
    })
    logger.info(f"🎯 [PAPER] {sym} TP2 → SL dipindah ke TP1 @ {tp1}")

    cascade_note = "\n⚡ <i>Cascade: harga skip ke TP3</i>" if cascade else ""

    msg = (
        f"{SEP}\n"
        f"🎯 <b>[PAPER] TP2 Hit — SL Trail ke TP1</b>\n"
        f"{SEP}\n\n"
        f"📌 <b>{sym}</b>  {_side_label(side)}\n"
        f"📈 TP2  <code>{_fp(tp2)}</code>  "
            f"<i>({_pct_price(entry, tp2)} dari entry)</i>\n\n"
        f"💰 Partial PnL   <code>${partial_pnl:+.4f}</code>  <i>(30% posisi)</i>\n"
        f"📊 ROI on margin <code>{_roi_on_margin(partial_pnl, partial_margin)}</code>  "
            f"<i>(dari ${partial_margin:.2f})</i>\n"
        f"💼 Balance       <code>${new_balance:.2f}</code>\n"
        f"🔒 Stop → TP1 @ <code>{_fp(tp1)}</code>\n"
        f"⏳ Holding 40% ke TP3 @ <code>{_fp(tp3)}</code>  "
            f"<i>({_pct_price(entry, tp3)})</i>{cascade_note}\n\n"
        f"<i>🕐 {datetime.now().strftime('%H:%M:%S')}</i>"
    )
    send_reply(msg, reply_to_message_id=tg_id)
    return partial_pnl


# ─── PAPER MONITOR (TP / SL Check) ───────────────────────────────────────────

def paper_monitor(trade: dict, current_price: float):
    """
    Cek apakah TP atau SL sudah kena untuk open paper trade.

    SL Trailing:
      Belum TP1  → SL = original
      TP1 hit    → SL = entry (breakeven)
      TP2 hit    → SL = TP1 price

    Cascade TP:
      Harga skip TP1/TP2 langsung ke TP2/TP3:
      → proses TP sebelumnya dulu (cascade=True) baru lanjut ke level berikutnya
    """
    t_id       = trade['id']
    sym        = trade['symbol']
    side       = trade['side']
    entry      = float(trade['entry_price'])
    tp1        = float(trade['tp1'])
    tp2        = float(trade['tp2'])
    tp3        = float(trade['tp3'])
    qty        = float(trade['quantity'])
    lev        = int(trade.get('leverage', 1))
    tg_id      = trade.get('telegram_msg_id')
    tp1_logged = trade.get('_tp1_logged', False)
    tp2_logged = trade.get('_tp2_logged', False)

    eff_sl = _effective_sl(trade)

    if side == 'Long':
        sl_hit  = current_price <= eff_sl
        tp1_hit = current_price >= tp1
        tp2_hit = current_price >= tp2
        tp3_hit = current_price >= tp3
    else:
        sl_hit  = current_price >= eff_sl
        tp1_hit = current_price <= tp1
        tp2_hit = current_price <= tp2
        tp3_hit = current_price <= tp3

    # ── SL Hit ──────────────────────────────────────────────────────────────
    if sl_hit:
        remaining = _remaining_qty(qty, tp1_logged, tp2_logged)
        pnl       = _calc_pnl(side, entry, eff_sl, remaining)

        if tp2_logged:
            reason = "SL Trail (TP1 level)"
        elif trade.get('is_sl_moved'):
            reason = "Breakeven SL"
        else:
            reason = "Stop Loss"

        _close_paper_trade(t_id, sym, side, entry, eff_sl, pnl, reason, tg_id,
                           qty=remaining, lev=lev)
        return

    # ── TP3 Hit (close remaining 40%) ────────────────────────────────────────
    if tp3_hit:
        # Cascade TP1 jika belum
        if not tp1_logged:
            _handle_tp1_partial(trade, cascade=True)
            trade['_tp1_logged'] = True
            tp1_logged = True

        # Cascade TP2 jika belum
        if not tp2_logged:
            _handle_tp2_partial(trade, cascade=True)
            trade['_tp2_logged'] = True
            tp2_logged = True

        # Close sisa 40% di TP3
        remaining = _remaining_qty(qty, tp1_logged, tp2_logged)
        pnl       = _calc_pnl(side, entry, tp3, remaining)
        _close_paper_trade(t_id, sym, side, entry, tp3, pnl, "TP3", tg_id,
                           qty=remaining, lev=lev)
        return

    # ── TP2 Hit (partial 30%) ────────────────────────────────────────────────
    if tp2_hit and not tp2_logged:
        # Cascade TP1 jika belum
        if not tp1_logged:
            _handle_tp1_partial(trade, cascade=True)
            trade['_tp1_logged'] = True

        _handle_tp2_partial(trade, cascade=False)
        return

    # ── TP1 Hit (partial 30%) ────────────────────────────────────────────────
    if tp1_hit and not tp1_logged:
        _handle_tp1_partial(trade, cascade=False)
        return


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

    price_pct   = _pct_price(entry, exit_price)
    margin      = _calc_margin(entry, qty, lev) if qty > 0 else 0.0
    roi_margin  = _roi_on_margin(pnl, margin) if margin > 0 else ""
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
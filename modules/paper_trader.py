"""
Paper Trader — simulates order fills, TP hits, SL hits, and PnL
without placing any real orders on Bybit.
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


def paper_execute(trade: dict, current_price: float) -> bool:
    """
    Simulate order fill for a PENDING paper trade.
    Returns True if 'filled', False if still waiting.
    """
    side  = trade['side']
    entry = float(trade['entry_price'])
    tg_id = trade.get('telegram_msg_id')

    filled = (side == 'Long' and current_price <= entry) or \
             (side == 'Short' and current_price >= entry)

    if filled:
        update_active_trade(trade['id'], {"status": "OPEN", "entry_price": current_price})
        logger.info(f"📋 [PAPER] Entry filled: {trade['symbol']} {side} @ {current_price}")

        # ✅ Kirim notifikasi Telegram sebagai reply ke sinyal asli
        msg = (
            f"📋 <b>[PAPER] Entry Filled</b>\n"
            f"📌 {trade['symbol']} <b>{side}</b>\n"
            f"💰 Fill Price: <code>{current_price}</code>\n"
            f"🛑 SL: <code>{trade['sl_price']}</code>\n"
            f"🎯 TP1: <code>{trade['tp1']}</code> | TP2: <code>{trade['tp2']}</code> | TP3: <code>{trade['tp3']}</code>"
        )
        send_reply(msg, reply_to_message_id=tg_id)

    return filled


def paper_monitor(trade: dict, current_price: float):
    """
    Check if TP or SL has been hit for an open paper trade.
    Handles breakeven move after TP1.
    """
    t_id   = trade['id']
    sym    = trade['symbol']
    side   = trade['side']
    entry  = float(trade['entry_price'])
    sl     = float(trade['sl_price'])
    tp1, tp2, tp3 = float(trade['tp1']), float(trade['tp2']), float(trade['tp3'])
    qty    = float(trade['quantity'])
    sl_moved = trade.get('is_sl_moved', False)
    lev    = int(trade.get('leverage', 1))
    tg_id  = trade.get('telegram_msg_id')

    # Effective SL after breakeven move
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

    # SL hit → close trade at loss
    if sl_hit:
        pnl = _calc_pnl(side, entry, effective_sl, qty, lev)
        reason = "SL hit" if not sl_moved else "Breakeven SL hit"
        _close_paper_trade(t_id, sym, pnl, reason, tg_id)
        return

    # TP3 hit → full close at max profit
    if tp3_hit:
        pnl = _calc_pnl(side, entry, tp3, qty, lev)
        _close_paper_trade(t_id, sym, pnl, "TP3 hit", tg_id)
        return

    # TP1 hit → move SL to breakeven
    if tp1_hit and not sl_moved:
        logger.info(f"♻️  [PAPER] {sym} TP1 hit — moving SL to breakeven")
        update_active_trade(t_id, {"is_sl_moved": True})

        # ✅ Notifikasi TP1 hit
        partial_pnl = _calc_pnl(side, entry, tp1, qty * 0.33, lev)
        send_reply(
            f"♻️ <b>[PAPER] TP1 Hit</b> — {sym} {side}\n"
            f"📈 Price: <code>{current_price}</code> → <code>TP1={tp1}</code>\n"
            f"🔒 SL dipindah ke Breakeven @ <code>{entry}</code>\n"
            f"💰 Est. partial PnL: <code>${partial_pnl:+.4f}</code>",
            reply_to_message_id=tg_id,
        )

    # TP2 hit (partial close simulation — log + notify)
    if tp2_hit and not trade.get('_tp2_logged'):
        logger.info(f"🎯 [PAPER] {sym} TP2 hit @ {current_price}")
        update_active_trade(t_id, {"status": "OPEN_TPS_SET"})

        # ✅ Notifikasi TP2 hit
        partial_pnl = _calc_pnl(side, entry, tp2, qty * 0.33, lev)
        send_reply(
            f"🎯 <b>[PAPER] TP2 Hit</b> — {sym} {side}\n"
            f"📈 Price: <code>{current_price}</code> → <code>TP2={tp2}</code>\n"
            f"💰 Est. partial PnL: <code>${partial_pnl:+.4f}</code>\n"
            f"⏳ Holding sisa posisi ke TP3: <code>{tp3}</code>",
            reply_to_message_id=tg_id,
        )


def _calc_pnl(side: str, entry: float, exit_price: float, qty: float, leverage: int) -> float:
    """Calculate PnL for a paper trade."""
    if side == 'Long':
        raw_pnl = (exit_price - entry) * qty
    else:
        raw_pnl = (entry - exit_price) * qty
    return round(raw_pnl * leverage, 4)


def _close_paper_trade(trade_id: int, symbol: str, pnl: float, reason: str,
                        telegram_msg_id: int = None):
    """Close a paper trade, update balance, and notify Telegram."""
    balance     = get_paper_balance()
    new_balance = balance + pnl
    update_paper_balance(new_balance)
    update_active_trade(trade_id, {"status": "CLOSED", "pnl": pnl})

    emoji = "✅" if pnl >= 0 else "❌"
    logger.info(f"{emoji} [PAPER] {symbol} CLOSED ({reason}) | PnL: ${pnl:+.4f} | Balance: ${new_balance:.2f}")

    # ✅ Kirim notifikasi close ke Telegram sebagai reply ke sinyal asli
    result_emoji = "🟢 PROFIT" if pnl >= 0 else "🔴 LOSS"
    send_reply(
        f"{emoji} <b>[PAPER] Trade Closed</b> — {symbol}\n"
        f"📊 Alasan: <b>{reason}</b>\n"
        f"💰 PnL: <code>${pnl:+.4f}</code> ({result_emoji})\n"
        f"💼 Paper Balance: <code>${new_balance:.2f}</code>",
        reply_to_message_id=telegram_msg_id,
    )
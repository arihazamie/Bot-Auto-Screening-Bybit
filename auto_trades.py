"""
auto_trades.py — Bybit Auto Trader
===================================
  auto_trade: true  → Real orders on Bybit
  auto_trade: false → Paper trade simulation (no real orders)

Toggle is set in config.json → "auto_trade": true/false
"""

import ccxt
import time
import schedule
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pybit.unified_trading import WebSocket

from modules.config_loader import CONFIG
from modules.database import (
    init_db,
    insert_active_trade,
    update_active_trade,
    get_active_trade_by_symbol,
    get_active_trades_by_status,
    count_open_active_trades,
    get_waiting_signals,
    mark_signal_ingested,
    get_closed_trades_last_24h,
    get_paper_balance,
    save_daily_report,
)
from modules.paper_trader import paper_execute, paper_monitor
from modules.notifier import send, trade_closed_alert

# ─── Config ────────────────────────────────────────────────

AUTO_TRADE     = CONFIG.get('auto_trade', False)   # ← Main toggle
RISK           = CONFIG['risk']
TARGET_LEV     = RISK['target_leverage']
RISK_PERCENT   = RISK['risk_percent']
MAX_POSITIONS  = RISK['max_positions']
TP_SPLIT       = RISK['tp_split']
MODE           = "REAL" if AUTO_TRADE else "PAPER"

# ─── Logging ───────────────────────────────────────────────
# Konfigurasi hanya dilakukan saat dijalankan langsung (standalone).
# Saat di-import oleh main.py, root logger sudah dikonfigurasi di sana
# (termasuk RotatingFileHandler untuk data/bot.log).

logger = logging.getLogger("AutoTrader")

# ─── Exchange (API key hanya dimuat saat REAL mode) ────────
# Paper mode hanya butuh public endpoint (fetch_ticker, load_markets)
# sehingga tidak perlu meng-ekspos credential ke library.

_exchange_opts: dict = {'options': {'defaultType': 'swap', 'adjustForTimeDifference': True}}
if AUTO_TRADE:
    bybit_key    = CONFIG['api'].get('bybit_key', '')
    bybit_secret = CONFIG['api'].get('bybit_secret', '')
    if not bybit_key or not bybit_secret or 'YOUR_' in bybit_key:
        raise ValueError("auto_trade=true tetapi bybit_key / bybit_secret belum diisi di config.json")
    _exchange_opts['apiKey'] = bybit_key
    _exchange_opts['secret'] = bybit_secret

exchange = ccxt.bybit(_exchange_opts)


# ══════════════════════════════════════════════════════════
# REAL TRADE HELPERS
# ══════════════════════════════════════════════════════════

def place_split_tps(symbol: str, side: str, total_qty: float, tp1, tp2, tp3) -> bool:
    """Place 3 limit TP orders on Bybit (real mode only)."""
    try:
        tp_side = 'sell' if str(side).lower() in ['buy', 'long'] else 'buy'
        q1 = float(exchange.amount_to_precision(symbol, total_qty * TP_SPLIT[0]))
        q2 = float(exchange.amount_to_precision(symbol, total_qty * TP_SPLIT[1]))
        q3 = float(exchange.amount_to_precision(symbol, total_qty * TP_SPLIT[2]))
        # Fix rounding drift
        diff = total_qty - (q1 + q2 + q3)
        if diff:
            q3 = float(exchange.amount_to_precision(symbol, q3 + diff))
        params = {'reduceOnly': True}
        logger.info(f"⚡ Placing TPs {symbol} ({tp_side}): {q1} | {q2} | {q3}")
        exchange.create_order(symbol, 'limit', tp_side, q1, float(tp1), params)
        exchange.create_order(symbol, 'limit', tp_side, q2, float(tp2), params)
        exchange.create_order(symbol, 'limit', tp_side, q3, float(tp3), params)
        return True
    except Exception as e:
        logger.error(f"TP placement failed {symbol}: {e}")
        return False


# ══════════════════════════════════════════════════════════
# WEBSOCKET HANDLERS (real mode only)
# ══════════════════════════════════════════════════════════

def on_execution_update(message):
    """Called when a real order is filled on Bybit."""
    if not AUTO_TRADE:
        return
    try:
        for item in message.get('data', []):
            if item.get('execType') != 'Trade':
                continue
            symbol = item['symbol']
            side = item['side']
            row = get_active_trade_by_symbol(symbol, status='OPEN')
            if not row:
                continue
            logger.info(f"⚡ WS: Entry filled {symbol}. Placing split TPs…")
            pos = exchange.fetch_position(symbol)
            size = float(pos['contracts'])
            if size > 0:
                ok = place_split_tps(symbol, side, size, row['tp1'], row['tp2'], row['tp3'])
                if ok:
                    update_active_trade(row['id'], {"status": "OPEN_TPS_SET"})
    except Exception as e:
        logger.error(f"WS execution error: {e}")


def on_position_update(message):
    """Called when a position changes (TP/SL hit, closed, etc.)."""
    if not AUTO_TRADE:
        return
    try:
        for pos in message.get('data', []):
            symbol = pos['symbol']
            size = float(pos['size'])
            mark_price = float(pos['markPrice'])
            side = pos['side']
            row = get_active_trade_by_symbol(symbol, status='OPEN_TPS_SET')
            if not row:
                continue
            t_id = row['id']

            # Closed
            if size == 0:
                logger.info(f"🏁 WS: {symbol} closed. Fetching real PnL…")
                time.sleep(1)
                try:
                    trades = exchange.fetch_my_trades(symbol, limit=1)
                    pnl = float(trades[0]['info'].get('closedPnl', 0)) if trades else 0
                except Exception:
                    pnl = 0
                update_active_trade(t_id, {"status": "CLOSED", "pnl": pnl})
                trade_closed_alert(symbol, pnl, "Position closed", MODE)
                continue

            # Breakeven logic — move SL to entry after TP1
            entry = float(row['entry_price'])
            tp1 = float(row['tp1'])
            sl_moved = row.get('is_sl_moved', False)
            hit_tp1 = (side == 'Buy' and mark_price >= tp1) or (side == 'Sell' and mark_price <= tp1)
            if hit_tp1 and not sl_moved:
                logger.info(f"♻️  WS: {symbol} hit TP1, moving SL to entry…")
                try:
                    exchange.set_position_stop_loss(symbol, entry, side.lower())
                    update_active_trade(t_id, {"is_sl_moved": True})
                except Exception as e:
                    logger.error(f"SL move failed {symbol}: {e}")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
# SIGNAL INGESTION
# ══════════════════════════════════════════════════════════

def ingest_signals():
    """Pull waiting signals from DB and create active trades."""
    if count_open_active_trades() >= MAX_POSITIONS:
        return

    try:
        if AUTO_TRADE:
            balance_info = exchange.fetch_balance()
            equity = float(balance_info['total']['USDT'])
        else:
            equity = get_paper_balance()
            exchange.load_markets()  # needed for precision
    except Exception as e:
        logger.error(f"Balance fetch error: {e}")
        return

    open_count = count_open_active_trades()
    for sig in get_waiting_signals():
        if open_count >= MAX_POSITIONS:
            break

        sym = sig['symbol']
        side = sig['side']
        entry = float(sig['entry_price'])
        sl = float(sig['sl_price'])
        tp1, tp2, tp3 = float(sig['tp1']), float(sig['tp2']), float(sig['tp3'])

        # Determine leverage
        final_lev = TARGET_LEV
        if AUTO_TRADE:
            try:
                mkt = exchange.market(sym)
                max_lev = float(mkt.get('limits', {}).get('leverage', {}).get('max', TARGET_LEV))
                final_lev = min(TARGET_LEV, int(max_lev))
            except Exception:
                pass

        position_value = equity * RISK_PERCENT * final_lev
        qty = position_value / entry

        if position_value < 6.0:
            logger.warning(f"⚠️  {sym} skipped — position value ${position_value:.2f} < $6 min")
            mark_signal_ingested(sig['id'])
            continue

        trade_id = insert_active_trade({
            "signal_id": sig['id'],
            "symbol": sym,
            "side": side,
            "entry_price": entry,
            "sl_price": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "quantity": qty,
            "leverage": final_lev,
            "mode": MODE,
        })
        mark_signal_ingested(sig['id'])
        open_count += 1
        logger.info(f"📥 Signal ingested: {sym} {side} | ${position_value:.2f} | {MODE}")


# ══════════════════════════════════════════════════════════
# EXECUTION
# ══════════════════════════════════════════════════════════

def execute_pending():
    """Execute PENDING trades — real orders or paper fills."""
    orders = get_active_trades_by_status(['PENDING'])
    if not orders:
        return

    for trade in orders:
        sym = trade['symbol']
        try:
            ticker = exchange.fetch_ticker(sym)
            current_price = float(ticker['last'])
        except Exception as e:
            logger.error(f"Price fetch failed {sym}: {e}")
            continue

        if AUTO_TRADE:
            _real_execute(trade, current_price)
        else:
            paper_execute(trade, current_price)


def _real_execute(trade: dict, current_price: float):
    """Place a real limit or market order on Bybit."""
    oid, sym, side = trade['id'], trade['symbol'], trade['side']
    entry, sl, qty, lev = (
        float(trade['entry_price']), float(trade['sl_price']),
        float(trade['quantity']), int(trade['leverage'])
    )
    try:
        try:
            exchange.set_leverage(lev, sym)
        except Exception:
            pass

        is_better = (side == 'Long' and current_price <= entry) or \
                    (side == 'Short' and current_price >= entry)
        order_side = 'buy' if side == 'Long' else 'sell'
        params = {'stopLoss': sl}
        qty_prec = float(exchange.amount_to_precision(sym, qty))

        if is_better:
            logger.info(f"⚡ MARKET {sym} — price {current_price} better than {entry}")
            res = exchange.create_order(sym, 'market', order_side, qty_prec, None, params)
        else:
            logger.info(f"⏳ LIMIT {sym} @ {entry} — current {current_price}")
            res = exchange.create_order(sym, 'limit', order_side, entry, qty_prec, params)

        if res and 'id' in res:
            update_active_trade(oid, {"order_id": res['id'], "status": "OPEN"})
            logger.info(f"✅ Order placed {sym} (ID: {res['id']})")
    except Exception as e:
        logger.error(f"❌ Real execute failed {sym}: {e}")
        update_active_trade(oid, {"status": "FAILED"})


# ══════════════════════════════════════════════════════════
# PAPER MONITOR LOOP
# ══════════════════════════════════════════════════════════

def monitor_paper_trades():
    """Tick paper trades against live prices (paper mode only)."""
    if AUTO_TRADE:
        return

    open_trades = get_active_trades_by_status(['OPEN', 'OPEN_TPS_SET'])
    if not open_trades:
        return

    for trade in open_trades:
        sym = trade['symbol']
        try:
            ticker = exchange.fetch_ticker(sym)
            current_price = float(ticker['last'])
            paper_monitor(trade, current_price)
        except Exception as e:
            logger.error(f"Paper monitor error {sym}: {e}")


# ══════════════════════════════════════════════════════════
# REAL SAFETY NET
# ══════════════════════════════════════════════════════════

def real_safety_check():
    """Recover missed TP placements for real trades (real mode only)."""
    if not AUTO_TRADE:
        return

    for trade in get_active_trades_by_status(['OPEN']):
        t_id, sym, side, oid = trade['id'], trade['symbol'], trade['side'], trade.get('order_id')
        if not oid:
            continue
        try:
            order_status = None
            try:
                order = exchange.fetch_order(oid, sym, params={'acknowledged': True})
                order_status = order['status']
            except Exception:
                for o in exchange.fetch_closed_orders(sym, limit=50):
                    if str(o['id']) == str(oid):
                        order_status = o['status']
                        break

            if order_status == 'closed':
                pos = exchange.fetch_position(sym)
                size = float(pos['contracts'])
                if size > 0:
                    ok = place_split_tps(sym, side, size, trade['tp1'], trade['tp2'], trade['tp3'])
                    if ok:
                        update_active_trade(t_id, {"status": "OPEN_TPS_SET"})
                        logger.info(f"🛡️  Safety net recovered TPs for {sym}")
            elif order_status == 'canceled':
                update_active_trade(t_id, {"status": "CANCELLED"})
        except Exception as e:
            logger.error(f"Safety check error {sym}: {e}")


# ══════════════════════════════════════════════════════════
# DAILY REPORT
# ══════════════════════════════════════════════════════════

def daily_report():
    trades = get_closed_trades_last_24h()
    if not trades:
        return
    pnls = [float(t.get('pnl', 0)) for t in trades]
    total = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    pnl_sum = sum(pnls)
    report = {
        "mode": MODE,
        "total_trades": total,
        "wins": wins,
        "losses": total - wins,
        "total_pnl": round(pnl_sum, 4),
        "win_rate": round((wins / total) * 100, 2) if total else 0,
        "best_trade": max(pnls),
        "worst_trade": min(pnls),
        "generated_at": str(datetime.now()),
    }
    if not AUTO_TRADE:
        report["paper_balance"] = get_paper_balance()

    date_str = datetime.now().strftime("%Y-%m-%d")
    save_daily_report(date_str, report)

    msg = (
        f"📊 Daily Report ({MODE})\n"
        f"Trades: {total} | W/L: {wins}/{total - wins}\n"
        f"PnL: ${pnl_sum:+.4f} | WR: {report['win_rate']}%"
    )
    if not AUTO_TRADE:
        msg += f"\n💼 Paper Balance: ${report['paper_balance']:.2f}"
    send(msg)
    logger.info(msg)


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # ─── Standalone logging (tidak di-import oleh main.py) ─────────────────
    import os, sys
    os.makedirs("data", exist_ok=True)
    LOG_LEVEL = logging.DEBUG if os.getenv("BOT_DEBUG", "").lower() == "true" else logging.INFO
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(
                "data/bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            ),
        ],
    )
    # ────────────────────────────────────────────────────────────────────────
    mode_banner = "💰 REAL TRADE MODE" if AUTO_TRADE else "📋 PAPER TRADE MODE"
    logger.info(f"🟢 Starting Auto-Trader — {mode_banner}")
    logger.info(f"   Max positions : {MAX_POSITIONS}")
    logger.info(f"   Risk/trade    : {RISK_PERCENT * 100}%")
    logger.info(f"   Leverage      : {TARGET_LEV}x")

    init_db()

    # WebSocket — only for real trading
    if AUTO_TRADE:
        ws = WebSocket(
            testnet=False,
            channel_type="private",
            api_key=CONFIG['api']['bybit_key'],
            api_secret=CONFIG['api']['bybit_secret'],
        )
        ws.execution_stream(callback=on_execution_update)
        ws.position_stream(callback=on_position_update)
        logger.info("🔌 WebSocket connected.")

    # Schedule tasks
    schedule.every(1).minutes.do(ingest_signals)
    schedule.every(5).seconds.do(execute_pending)

    if AUTO_TRADE:
        schedule.every(10).seconds.do(real_safety_check)
    else:
        schedule.every(10).seconds.do(monitor_paper_trades)

    schedule.every().day.at("00:00").do(daily_report)

    logger.info(f"🚀 Bot running in {MODE} mode. Press Ctrl+C to stop.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped.")
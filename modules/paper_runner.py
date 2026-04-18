"""
paper_runner.py — Paper Trade Background Runner
================================================
Dijalankan otomatis sebagai daemon thread ketika auto_trade: false.
Tidak perlu terminal kedua. Cukup jalankan main.py saja.

Loop ini menangani:
  • Ingest sinyal dari DB  → setiap 60 detik
  • Execute pending fills  → setiap 5 detik
  • Monitor TP/SL hits     → setiap 10 detik
  • Daily report           → setiap hari jam 00:00
"""

import time
import logging
import threading
from datetime import datetime


from modules.config_loader import CONFIG
from modules.exchange import BybitClient
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
from modules.notifier import send, send_reply, trade_closed_alert

logger = logging.getLogger("PaperRunner")

# ─── Config ────────────────────────────────────────────────────────────────
RISK             = CONFIG["risk"]
USE_MAX_LEVERAGE = RISK.get("use_max_leverage", False)   # True = pakai max lev tiap coin dari Bybit
TARGET_LEV       = RISK["target_leverage"]               # Fallback / fixed leverage jika use_max_leverage=False
RISK_PERCENT     = RISK["risk_percent"]
MAX_POSITIONS    = RISK["max_positions"]
MODE             = "PAPER"

# ─── Exchange (public-only — untuk harga & market info) ───────────────────
_client: BybitClient | None = None
_client_lock = threading.Lock()


def _get_client() -> BybitClient:
    """Lazy-init BybitClient singleton (public only, tidak butuh API key)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = BybitClient(debug=False, auto_trade=False)
                logger.info("📡 PaperRunner — BybitClient ready")
    return _client


def _get_leverage_for(symbol: str) -> int:
    """
    Tentukan leverage yang akan dipakai untuk satu symbol.

    use_max_leverage: true  → ambil max leverage per coin dari Bybit
                               (BTC=100x, GALA=25x, DOGE=50x, dst)
    use_max_leverage: false → pakai target_leverage dari config (fixed semua coin)

    Sumber data: BybitClient.fetch_max_leverage()
      → baca info.leverageFilter.maxLeverage (Bybit native, paling akurat)
      → fallback ke limits.leverage.max (ccxt standard)
      → fallback ke TARGET_LEV (config)
    """
    if not USE_MAX_LEVERAGE:
        return TARGET_LEV

    client = _get_client()
    max_lev = client.fetch_max_leverage(symbol, fallback=TARGET_LEV)
    return max_lev


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL INGESTION
# ══════════════════════════════════════════════════════════════════════════════

def _ingest_signals():
    """Ambil sinyal WAITING dari DB dan buat paper trade baru."""
    if count_open_active_trades() >= MAX_POSITIONS:
        return

    client = _get_client()
    try:
        equity = get_paper_balance()
    except Exception as e:
        logger.error(f"Paper balance fetch error: {e}")
        return

    open_count = count_open_active_trades()
    signals = get_waiting_signals()

    if signals:
        logger.info(f"📥 [{MODE}] {len(signals)} sinyal menunggu untuk diingest")

    for sig in signals:
        if open_count >= MAX_POSITIONS:
            logger.warning(f"⚠️  Max positions ({MAX_POSITIONS}) tercapai, sinyal ditunda")
            break

        sym   = sig["symbol"]
        side  = sig["side"]
        entry = float(sig["entry_price"])
        sl    = float(sig["sl_price"])
        tp1   = float(sig["tp1"])
        tp2   = float(sig["tp2"])
        tp3   = float(sig["tp3"])

        # ✅ Ambil telegram_msg_id dari sinyal agar bisa reply ke pesan asli
        tg_msg_id = sig.get("telegram_msg_id")

        # Leverage: per-coin max dari Bybit, atau fixed dari config
        final_lev = _get_leverage_for(sym)

        position_value = equity * RISK_PERCENT * final_lev
        qty = position_value / entry if entry > 0 else 0

        if position_value < 6.0:
            logger.warning(
                f"⚠️  [{sym}] skip — position value ${position_value:.2f} < $6 min"
            )
            mark_signal_ingested(sig["id"])
            continue

        insert_active_trade({
            "signal_id":       sig["id"],
            "symbol":          sym,
            "side":            side,
            "entry_price":     entry,
            "sl_price":        sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "quantity":        qty,
            "leverage":        final_lev,
            "mode":            MODE,
            "telegram_msg_id": tg_msg_id,   # ✅ simpan untuk reply selanjutnya
        })
        mark_signal_ingested(sig["id"])
        open_count += 1

        lev_tag = f"MAX={final_lev}x" if USE_MAX_LEVERAGE else f"Fixed={final_lev}x"
        margin  = equity * RISK_PERCENT   # uang yang terkunci sebagai jaminan
        log_msg = (
            f"✅ [{MODE}] Trade created: {sym} {side} | "
            f"Qty={qty:.4f} | Margin=${margin:.2f} | PosVal=${position_value:.2f} | Lev={lev_tag}"
        )
        logger.info(log_msg)

        # ✅ Kirim notifikasi Telegram sebagai reply ke sinyal asli
        notify_msg = (
            f"✅ <b>[PAPER] Trade Created</b>\n"
            f"📌 {sym} <b>{side}</b> | ⚡ <code>{lev_tag}</code>\n"
            f"{'─' * 24}\n"
            f"💵 Margin used  : <code>${margin:.2f}</code>  ({RISK_PERCENT*100:.1f}%)\n"
            f"📊 Pos. Value   : <code>${position_value:.2f}</code>  (margin × {final_lev}x)\n"
            f"📦 Qty          : <code>{qty:.4f}</code>\n"
            f"{'─' * 24}\n"
            f"⏳ Menunggu entry @ <code>{entry}</code>"
        )
        send_reply(notify_msg, reply_to_message_id=tg_msg_id)


# ══════════════════════════════════════════════════════════════════════════════
# EXECUTE PENDING (simulasi fill entry)
# ══════════════════════════════════════════════════════════════════════════════

PENDING_EXPIRE_HOURS = 24  # Auto-expire PENDING trade jika entry tidak terisi


def _expire_pending_if_old(trade: dict) -> bool:
    """
    Cek apakah PENDING trade sudah melewati batas waktu 24 jam.
    Jika ya, cancel dan kirim notifikasi. Returns True jika di-expire.
    """
    try:
        created_at = datetime.fromisoformat(trade.get("created_at", ""))
    except (ValueError, TypeError):
        return False

    age_hours = (datetime.now() - created_at).total_seconds() / 3600
    if age_hours < PENDING_EXPIRE_HOURS:
        return False

    sym   = trade["symbol"]
    side  = trade["side"]
    entry = trade["entry_price"]
    tg_id = trade.get("telegram_msg_id")

    update_active_trade(trade["id"], {"status": "CANCELLED"})
    logger.info(
        f"⏰ [PAPER] {sym} PENDING expire — entry {entry} tidak terisi "
        f"dalam {PENDING_EXPIRE_HOURS} jam"
    )

    from modules.paper_trader import SEP, _side_label, _fp
    from modules.notifier import send_reply
    msg = (
        f"{SEP}\n"
        f"⏰ <b>[PAPER] Order Expired</b>\n"
        f"{SEP}\n\n"
        f"📌 <b>{sym}</b>  {_side_label(side)}\n"
        f"💰 Entry <code>{_fp(entry)}</code> tidak pernah terisi\n"
        f"⌛ Expired setelah <b>{PENDING_EXPIRE_HOURS} jam</b>\n\n"
        f"<i>🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
    )
    send_reply(msg, reply_to_message_id=tg_id)
    return True


def _execute_pending():
    """
    Cek pending trades apakah harga sudah menyentuh entry.
    Auto-expire jika sudah lebih dari 24 jam tanpa fill.
    """
    orders = get_active_trades_by_status(["PENDING"])
    if not orders:
        return

    client = _get_client()
    for trade in orders:
        sym = trade["symbol"]

        # ✅ Auto-expire setelah 24 jam
        if _expire_pending_if_old(trade):
            continue

        try:
            ticker = client.fetch_ticker(sym)
            if ticker is None:
                logger.warning(f"Execute pending [{sym}] — ticker kosong, skip")
                continue
            current_price = float(ticker["last"])
            paper_execute(trade, current_price)
        except Exception as e:
            logger.error(f"Execute pending error [{sym}]: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MONITOR OPEN TRADES (simulasi TP/SL)
# ══════════════════════════════════════════════════════════════════════════════

def _monitor_trades():
    """Pantau trade OPEN dan cek apakah TP atau SL kena."""
    open_trades = get_active_trades_by_status(["OPEN", "OPEN_TPS_SET"])
    if not open_trades:
        return

    client = _get_client()
    for trade in open_trades:
        sym = trade["symbol"]
        try:
            ticker = client.fetch_ticker(sym)
            if ticker is None:
                logger.warning(f"Monitor [{sym}] — ticker kosong, skip")
                continue
            current_price = float(ticker["last"])
            paper_monitor(trade, current_price)
        except Exception as e:
            logger.error(f"Paper monitor error [{sym}]: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# DAILY REPORT
# ══════════════════════════════════════════════════════════════════════════════

def _daily_report():
    """Kirim ringkasan harian PnL ke Telegram."""
    trades = get_closed_trades_last_24h()
    if not trades:
        logger.info("[PAPER] Daily report: tidak ada trade tertutup dalam 24 jam")
        return

    pnls    = [float(t.get("pnl", 0)) for t in trades]
    total   = len(pnls)
    wins    = sum(1 for p in pnls if p > 0)
    pnl_sum = sum(pnls)
    balance = get_paper_balance()

    report = {
        "mode":          MODE,
        "total_trades":  total,
        "wins":          wins,
        "losses":        total - wins,
        "total_pnl":     round(pnl_sum, 4),
        "win_rate":      round((wins / total) * 100, 2) if total else 0,
        "best_trade":    max(pnls),
        "worst_trade":   min(pnls),
        "paper_balance": balance,
        "generated_at":  str(datetime.now()),
    }
    date_str = datetime.now().strftime("%Y-%m-%d")
    save_daily_report(date_str, report)

    msg = (
        f"📊 <b>Daily Report (PAPER)</b>\n"
        f"Trades: {total} | W/L: {wins}/{total - wins}\n"
        f"PnL: ${pnl_sum:+.4f} | WR: {report['win_rate']}%\n"
        f"💼 Paper Balance: <b>${balance:.2f}</b>"
    )
    send(msg)
    logger.info(msg)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP (dijalankan di thread terpisah)
# ══════════════════════════════════════════════════════════════════════════════

def _run_loop():
    """
    Loop utama paper runner.
    Menggunakan counter manual (bukan schedule global) agar tidak
    konflik dengan scheduler di main.py.
    """
    logger.info("🟢 PaperRunner thread started")
    logger.info(f"   Balance awal  : ${get_paper_balance():.2f}")
    logger.info(f"   Risk/trade    : {RISK_PERCENT * 100:.1f}%")
    logger.info(f"   Max positions : {MAX_POSITIONS}")
    if USE_MAX_LEVERAGE:
        logger.info("   Leverage      : MAX per coin (dari Bybit) 🔝")
    else:
        logger.info(f"   Leverage      : Fixed {TARGET_LEV}x semua coin")

    last_ingest  = 0.0   # setiap 60 detik
    last_execute = 0.0   # setiap 5 detik
    last_monitor = 0.0   # setiap 10 detik
    last_report_day = -1 # setiap hari jam 00:00

    while True:
        try:
            now = time.time()

            if now - last_execute >= 5:
                _execute_pending()
                last_execute = now

            if now - last_monitor >= 10:
                _monitor_trades()
                last_monitor = now

            if now - last_ingest >= 60:
                _ingest_signals()
                last_ingest = now

            # Daily report jam 07:00
            current_day  = datetime.now().day
            current_hour = datetime.now().hour
            if current_hour == 7 and current_day != last_report_day:
                _daily_report()
                last_report_day = current_day

        except Exception as e:
            logger.error(f"PaperRunner loop error: {e}", exc_info=True)

        time.sleep(1)


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_paper_update():
    """
    Jalankan satu siklus pengecekan paper trade (ingest → execute → monitor).
    Dipanggil oleh scheduler di main.py setiap 1 menit.
    """
    _ingest_signals()
    _execute_pending()
    _monitor_trades()


def start_paper_runner():
    """
    Jalankan paper trade runner sebagai daemon thread.
    Panggil ini dari main.py ketika auto_trade=False.
    Thread daemon akan otomatis mati saat main process berhenti.
    """
    t = threading.Thread(
        target=_run_loop,
        name="PaperRunner",
        daemon=True,   # mati otomatis kalau main.py dihentikan
    )
    t.start()
    logger.info("🧵 PaperRunner daemon thread launched")
    return t
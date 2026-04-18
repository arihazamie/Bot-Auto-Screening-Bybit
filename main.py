import time
import schedule
import random
import os
import sys
import io
import logging
import pandas as pd
import pandas_ta as ta
import numpy as np
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────────────────────
# FIX #3: Force UTF-8 di Windows agar emoji tidak crash (UnicodeEncodeError cp1252)
# ─────────────────────────────────────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from modules.config_loader import CONFIG
from modules.exchange import BybitClient
from modules.database import init_db, get_active_signals, save_signal_to_db
from modules.technicals import get_technicals, detect_divergence
from modules.quant import calculate_metrics, check_fakeout
from modules.derivatives import analyze_derivatives
from modules.smc import analyze_smc
from modules.patterns import find_pattern
from modules.watchlist import refresh_watchlist, get_watchlist, get_watchlist_info
from modules.paper_runner import start_paper_runner
from modules.telegram_commands import start_command_listener, is_paused
from modules.telegram_bot import send_alert, update_status_dashboard, send_scan_completion
from modules.paper_runner import run_paper_update

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup — semua module pakai logger ini
# Set DEBUG=true di env untuk verbose output
# ─────────────────────────────────────────────────────────────────────────────
LOG_LEVEL = logging.DEBUG if os.getenv("BOT_DEBUG", "").lower() == "true" else logging.INFO

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),                              # print ke terminal (UTF-8)
        logging.FileHandler("data/bot.log", mode="a", encoding="utf-8"),  # simpan ke file (UTF-8)
    ],
)
logger = logging.getLogger("Main")


# ─────────────────────────────────────────────────────────────────────────────
# Mode & Config
# ─────────────────────────────────────────────────────────────────────────────
AUTO_TRADE_ENABLED = CONFIG.get("auto_trade", False)
DEBUG_MODE         = os.getenv("BOT_DEBUG", "").lower() == "true"

print("=" * 60)
print("🤖 Bybit Screening Bot v8")
print("=" * 60)
print(f"   Mode    : {'AUTO TRADE 🤖' if AUTO_TRADE_ENABLED else 'PAPER TRADE 📋 (signal + simulasi)'}")
print(f"   Debug   : {'ON 🔍' if DEBUG_MODE else 'OFF (set BOT_DEBUG=true untuk verbose)'}")
print(f"   Env     : {os.getenv('BOT_ENV', 'PROD')}")
print("=" * 60)

ENTRY_TF = CONFIG["system"].get("entry_timeframe", "15m")
TREND_TF = CONFIG["system"].get("trend_timeframe", "1h")
print(f"📐 Timeframes — Entry: {ENTRY_TF} | Trend: {TREND_TF}")


# ─────────────────────────────────────────────────────────────────────────────
# Exchange client (singleton)
# ─────────────────────────────────────────────────────────────────────────────
client = BybitClient(debug=DEBUG_MODE, auto_trade=AUTO_TRADE_ENABLED)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def get_btc_bias() -> str:
    """BTC directional bias menggunakan EMA 13/21 pada TREND_TF."""
    try:
        df = client.fetch_ohlcv("BTC/USDT:USDT", TREND_TF, limit=100)
        if df is None or len(df) < 30:
            logger.warning("get_btc_bias: data BTC tidak cukup — fallback Sideways")
            return "Sideways"

        df["ema13"] = ta.ema(df["close"], length=13)
        df["ema21"] = ta.ema(df["close"], length=21)
        curr = df.iloc[-1]

        if pd.isna(curr["ema13"]) or pd.isna(curr["ema21"]):
            logger.warning("get_btc_bias: EMA NaN — fallback Sideways")
            return "Sideways"

        bias = "Bullish" if curr["ema13"] > curr["ema21"] else "Bearish"
        logger.info(f"BTC Bias ({TREND_TF}): {bias} | EMA13={curr['ema13']:.2f} EMA21={curr['ema21']:.2f}")
        return bias

    except Exception as e:
        logger.error(f"get_btc_bias GAGAL: {type(e).__name__}: {e}")
        return "Sideways"


def get_symbol_trend(symbol: str) -> str:
    """Per-symbol trend pada TREND_TF menggunakan EMA 13/21."""
    try:
        df = client.fetch_ohlcv(symbol, TREND_TF, limit=100)
        if df is None or len(df) < 50:
            return "Sideways"

        df["ema13"] = ta.ema(df["close"], length=13)
        df["ema21"] = ta.ema(df["close"], length=21)
        curr = df.iloc[-1]

        if pd.isna(curr["ema13"]) or pd.isna(curr["ema21"]):
            return "Sideways"

        if curr["ema13"] > curr["ema21"]:
            return "Bullish"
        elif curr["ema13"] < curr["ema21"]:
            return "Bearish"
        return "Sideways"

    except Exception as e:
        logger.debug(f"get_symbol_trend [{symbol}]: {type(e).__name__}: {e}")
        return "Sideways"


def calculate_rr(entry, sl, tp3) -> float:
    if entry <= 0 or sl <= 0 or tp3 <= 0:
        return 0.0
    risk = abs(entry - sl)
    return round(abs(tp3 - entry) / risk, 2) if risk > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# analyze_ticker — analisis lengkap satu symbol
# ─────────────────────────────────────────────────────────────────────────────
def analyze_ticker(symbol: str, btc_bias: str, active_signals: set, counters: dict):
    """
    Multi-Timeframe analysis per symbol.
    Return dict hasil jika lolos semua filter, None jika tidak.

    Setiap titik rejection di-log secara eksplisit sehingga mudah di-debug.
    """

    # ── 1. Duplicate check ────────────────────────────────────────────────
    if (symbol, ENTRY_TF) in active_signals:
        counters["duplicate"] += 1
        return None

    step = "init"   # diupdate setiap langkah — muncul di error log
    try:

        # ── 2. Fetch ticker ───────────────────────────────────────────────
        step = "fetch_ticker"
        ticker_info = client.fetch_ticker(symbol)
        if ticker_info is None:
            counters["no_ticker"] += 1
            logger.debug(f"[{symbol}] skip — ticker kosong/invalid")
            return None

        # Skip settlement token
        if "ST" in ticker_info.get("info", {}).get("symbol", ""):
            return None

        # ── 3. Symbol trend (TREND_TF) ────────────────────────────────────
        step = "symbol_trend"
        symbol_trend = get_symbol_trend(symbol)

        # ── 4. Fetch OHLCV entry timeframe ───────────────────────────────
        step = "fetch_ohlcv"
        min_candles = CONFIG["system"].get("min_candles_analysis", 150)
        df = client.fetch_ohlcv(symbol, ENTRY_TF, limit=min_candles + 50)

        if df is None or len(df) < min_candles:
            counters["no_candles"] += 1
            logger.debug(
                f"[{symbol}] skip — OHLCV kurang "
                f"({len(df) if df is not None else 0}/{min_candles} bars)"
            )
            return None

        # ── 5. Technicals & Pattern ───────────────────────────────────────
        step = "technicals"
        df = get_technicals(df)

        step = "pattern"
        pattern = find_pattern(df)
        if not pattern:
            counters["no_pattern"] += 1
            return None

        side = CONFIG["pattern_signals"].get(pattern)
        if not side:
            logger.warning(f"[{symbol}] pattern '{pattern}' tidak ada di pattern_signals config")
            counters["no_pattern"] += 1
            return None

        # ── 6. MTF alignment ─────────────────────────────────────────────
        step = "mtf"
        if symbol_trend == "Bearish" and side == "Long":
            counters["mtf_conflict"] += 1
            logger.debug(f"[{symbol}] skip — MTF conflict: trend Bearish tapi signal Long")
            return None
        if symbol_trend == "Bullish" and side == "Short":
            counters["mtf_conflict"] += 1
            logger.debug(f"[{symbol}] skip — MTF conflict: trend Bullish tapi signal Short")
            return None

        # ── 7. BTC bias filter ────────────────────────────────────────────
        step = "btc_bias"
        if "Bearish" in btc_bias and side == "Long":
            counters["btc_filter"] += 1
            return None
        if "Bullish" in btc_bias and side == "Short":
            counters["btc_filter"] += 1
            return None

        # ── 8. SMC ────────────────────────────────────────────────────────
        step = "smc"
        valid_smc, smc_score, smc_reasons = analyze_smc(df, side)
        min_smc = CONFIG["strategy"].get("min_smc_score", 0)
        if not valid_smc or smc_score < min_smc:
            counters["smc_fail"] += 1
            logger.debug(f"[{symbol}] skip — SMC fail: valid={valid_smc} score={smc_score} min={min_smc} | {smc_reasons}")
            return None

        # ── 9. Quant & Derivatives ────────────────────────────────────────
        step = "quant"
        df, basis, z_score, zeta_score, obi, quant_score, quant_reasons = calculate_metrics(df, ticker_info)

        step = "derivatives"
        valid_deriv, deriv_score, deriv_reasons = analyze_derivatives(df, ticker_info, side)
        min_deriv = CONFIG["strategy"].get("min_deriv_score", 0)
        if not valid_deriv or deriv_score < min_deriv:
            counters["deriv_fail"] += 1
            logger.debug(f"[{symbol}] skip — Deriv fail: valid={valid_deriv} score={deriv_score} | {deriv_reasons}")
            return None

        # ── 10. Divergence & Tech score ───────────────────────────────────
        step = "divergence"
        div_score, div_msg = detect_divergence(df)
        # Base 3 (pattern) + divergence + smc_score kontribusi
        # Sehingga tidak hanya bergantung pada divergence yang jarang terjadi
        tech_score   = 3 + div_score + min(smc_score, 2)
        tech_reasons = [f"Pattern: {pattern}", div_msg] + smc_reasons
        min_tech     = CONFIG["strategy"]["min_tech_score"]

        if tech_score < min_tech:
            counters["low_tech_score"] += 1
            logger.debug(f"[{symbol}] skip — tech_score={tech_score} < min={min_tech}")
            return None

        # ── 11. Fakeout / RVOL check ──────────────────────────────────────
        step = "rvol"
        min_rvol  = CONFIG["indicators"]["min_rvol"]
        valid_fo, fo_msg = check_fakeout(df, min_rvol)
        if not valid_fo:
            counters["low_rvol"] += 1
            rvol_val = df["RVOL"].iloc[-1]
            logger.debug(f"[{symbol}] skip — RVOL={rvol_val:.2f} < min={min_rvol} | {fo_msg}")
            return None

        # ── 12. Setup & R:R ───────────────────────────────────────────────
        step = "setup"
        s          = CONFIG["setup"]
        swing_high = df["high"].iloc[-50:].max()
        swing_low  = df["low"].iloc[-50:].min()
        rng        = swing_high - swing_low

        if rng <= 0:
            counters["bad_setup"] += 1
            logger.debug(f"[{symbol}] skip — range=0 (harga flat?)")
            return None

        if side == "Long":
            entry = (swing_high - rng * s["fib_entry_start"] + swing_high - rng * s["fib_entry_end"]) / 2
            sl    = swing_low  - rng * s["fib_sl"]
            tp1   = swing_low  + rng
            tp2   = swing_low  + rng * 1.618
            tp3   = swing_low  + rng * 2.618
        else:
            entry = (swing_low + rng * s["fib_entry_start"] + swing_low + rng * s["fib_entry_end"]) / 2
            sl    = swing_high + rng * s["fib_sl"]
            tp1   = swing_high - rng
            tp2   = swing_high - rng * 1.618
            tp3   = swing_high - rng * 2.618

        rr     = calculate_rr(entry, sl, tp3)
        min_rr = CONFIG["strategy"].get("risk_reward_min", 2.0)
        if rr < min_rr:
            counters["low_rr"] += 1
            logger.debug(f"[{symbol}] skip — R:R={rr} < min={min_rr}")
            return None

        df["funding"] = float(ticker_info.get("info", {}).get("fundingRate", 0))

        total_score = tech_score + smc_score + quant_score + deriv_score
        logger.info(
            f"✅ SIGNAL [{symbol}] {side} | {pattern} | {ENTRY_TF} | "
            f"RR={rr} | Score={total_score} "
            f"(tech={tech_score} smc={smc_score} quant={quant_score} deriv={deriv_score})"
        )

        return {
            "Symbol":       symbol,
            "Side":         side,
            "Timeframe":    ENTRY_TF,
            "Trend_TF":     TREND_TF,
            "Symbol_Trend": symbol_trend,
            "Pattern":      pattern,
            "Entry":  float(entry), "SL": float(sl),
            "TP1":    float(tp1),   "TP2": float(tp2), "TP3": float(tp3),
            "RR":     float(rr),
            "Tech_Score":  int(tech_score),  "Quant_Score": int(quant_score),
            "Deriv_Score": int(deriv_score), "SMC_Score":   int(smc_score),
            "Basis":      float(basis),
            "Z_Score":    float(z_score),
            "Zeta_Score": float(zeta_score),
            "OBI":        float(obi),
            "BTC_Bias":   btc_bias,
            "Reason":     pattern,
            "Tech_Reasons":  ", ".join(str(r) for r in tech_reasons),
            "Quant_Reasons": ", ".join(str(r) for r in quant_reasons),
            "SMC_Reasons":   ", ".join(str(r) for r in smc_reasons if r),
            "Deriv_Reasons": ", ".join(str(r) for r in deriv_reasons),
            "df": df,
        }

    except Exception as e:
        counters["exception"] += 1
        # Selalu log exception dengan step & traceback — ini kunci debugging
        logger.error(
            f"💥 [{symbol}] Exception di step '{step}': "
            f"{type(e).__name__}: {e}"
        )
        if DEBUG_MODE:
            logger.debug(traceback.format_exc())
        return None


# ─────────────────────────────────────────────────────────────────────────────
# scan
# ─────────────────────────────────────────────────────────────────────────────
def scan():
    # ✅ Cek pause flag dari Telegram /pause command
    if is_paused():
        logger.info("⏸ Scan dilewati — bot sedang di-pause via Telegram (/resume untuk lanjutkan)")
        return

    start_time = time.time()
    mode_label = "AUTO TRADE 🤖" if AUTO_TRADE_ENABLED else "SIGNAL ONLY 📡"
    logger.info(f"🔭 Scan dimulai | Mode: {mode_label}")

    btc_bias = get_btc_bias()
    logger.info(f"📊 BTC Bias ({TREND_TF}): {btc_bias}")

    active_signals = get_active_signals()
    logger.info(f"🛡️  Active Signals (skip duplicate): {len(active_signals)}")

    signal_count = 0
    counters     = defaultdict(int)

    try:
        syms = get_watchlist()

        if syms:
            info = get_watchlist_info()
            logger.info(f"📋 Watchlist: {len(syms)} pairs (updated: {info.get('updated_at', '?')[:16]})")
        else:
            logger.warning("⚠️  Watchlist belum ada — fallback fetch semua pair dari Bybit...")
            markets = client.load_markets()
            STABLECOINS = {"USDC","USDT","DAI","FDUSD","USDD","USDE","TUSD","BUSD","PYUSD","USDS","EUR","USD"}
            syms = [
                s for s in markets
                if markets[s].get("swap")
                and markets[s]["quote"] == "USDT"
                and markets[s].get("active")
                and markets[s]["base"] not in STABLECOINS
            ]

        random.shuffle(syms)
        logger.info(f"🔍 Scanning {len(syms)} pairs dengan {CONFIG['system']['max_threads']} threads...")

        with ThreadPoolExecutor(max_workers=CONFIG["system"]["max_threads"]) as ex:
            futures = {ex.submit(analyze_ticker, s, btc_bias, active_signals, counters): s for s in syms}
            for f in as_completed(futures):
                res = f.result()
                if res:
                    # ✅ send_alert now returns Telegram message_id (int) on success, None on failure
                    tg_msg_id = send_alert(res, auto_trade=AUTO_TRADE_ENABLED)
                    if tg_msg_id:
                        signal_count += 1
                        # Simpan ke DB di KEDUA mode agar paper_runner bisa ingest
                        # ✅ Teruskan telegram_msg_id agar paper_runner bisa reply ke pesan asli
                        save_signal_to_db(res, telegram_msg_id=tg_msg_id)
                        mode_tag = "🤖 real" if AUTO_TRADE_ENABLED else "📋 paper"
                        logger.info(f"   {mode_tag} signal queued: {res['Symbol']} {res['Side']} [{res['Timeframe']}]")

    except Exception as e:
        logger.error(f"Scan Error: {type(e).__name__}: {e}", exc_info=True)

    finally:
        duration = time.time() - start_time

        # ── Filter Breakdown ────────────────────────────────────────────
        total_filtered = sum(counters.values())
        print()
        print(f"📊 Filter Breakdown — {total_filtered} pairs diproses:")
        labels = {
            "no_ticker":      "Ticker kosong / invalid",
            "no_candles":     "OHLCV tidak cukup",
            "no_pattern":     "Tidak ada pattern",
            "mtf_conflict":   "MTF conflict (1h vs 15m)",
            "btc_filter":     "BTC bias conflict",
            "smc_fail":       "SMC fail",
            "deriv_fail":     "Derivatives fail",
            "low_rvol":       f"RVOL rendah (< {CONFIG['indicators']['min_rvol']}x)",
            "low_tech_score": f"Tech score rendah (< {CONFIG['strategy']['min_tech_score']})",
            "low_rr":         f"R:R rendah (< {CONFIG['strategy'].get('risk_reward_min', 2.0)})",
            "bad_setup":      "Setup invalid (range=0)",
            "duplicate":      "Duplikat signal aktif",
            "exception":      "⚠️  Exception (cek log!)",   # ← ini yang penting
        }
        for key, label in labels.items():
            val = counters.get(key, 0)
            if val > 0:
                bar = "█" * min(val, 35)
                pct = val / len(syms) * 100 if syms else 0
                print(f"   {label:<40} {val:>4} ({pct:4.1f}%)  {bar}")

        if total_filtered == 0:
            print("   (tidak ada pair yang diproses — kemungkinan watchlist kosong)")

        print()
        logger.info(f"✅ Scan selesai {duration:.2f}s | Signals: {signal_count} | Mode: {mode_label}")
        send_scan_completion(signal_count, duration, btc_bias)


# ─────────────────────────────────────────────────────────────────────────────
# Watchlist refresh
# ─────────────────────────────────────────────────────────────────────────────
def refresh_daily_watchlist():
    logger.info("🔄 Daily watchlist refresh...")
    symbols = refresh_watchlist(client.raw, top_n=CONFIG["system"].get("watchlist_top_n", 100))
    if symbols:
        logger.info(f"✅ Watchlist refreshed: {len(symbols)} pairs")
    else:
        logger.warning("⚠️  Watchlist refresh gagal")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)
    init_db()

    # ── Auto-start Paper Trader jika mode SIGNAL ONLY ─────────────
    if not AUTO_TRADE_ENABLED:
        print()
        print("📋 Paper Trade Runner — AUTO START")
        print("   (auto_trade=false → paper trader berjalan di background thread)")
        print()
        start_paper_runner()

    # ── Start Telegram Command Listener ───────────────────────────
    print("🎧 Telegram Command Listener — AUTO START")
    print("   Perintah: /start /status /trades /balance /report /pause /resume")
    print()
    start_command_listener()

    # ── Cek koneksi Bybit sebelum mulai ─────────────────────────
    ok = client.health_check(auto_trade=AUTO_TRADE_ENABLED)
    if not ok:
        print("\n❌ Health check gagal — pastikan:")
        print("   1. Koneksi internet aktif")
        print("   2. Bybit API tidak diblokir (coba VPN jika perlu)")
        print("   3. bybit_key & bybit_secret di config.json benar")
        print("   Bot tetap berjalan, tapi sinyal mungkin tidak keluar.\n")

    # ── Refresh watchlist saat startup ──────────────────────────
    if not get_watchlist():
        logger.info("📋 Watchlist belum ada — fetch sekarang...")
        refresh_watchlist(client.raw, top_n=CONFIG["system"].get("watchlist_top_n", 100))

    # ── Mulai scan pertama ───────────────────────────────────────
    scan()

    # ── Schedule ─────────────────────────────────────────────────
    schedule.every(15).minutes.do(scan)
    schedule.every(1).minutes.do(run_paper_update)
    schedule.every().day.at("07:00").do(refresh_daily_watchlist)

    print("\n🚀 Bot Started.")
    print(f"🕖 Watchlist refresh otomatis setiap hari jam 07:00")
    print(f"💡 Tip: jalankan dengan BOT_DEBUG=true untuk verbose output\n")
    while True:
        schedule.run_pending()
        time.sleep(1)
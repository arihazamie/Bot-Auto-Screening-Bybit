import time
import schedule
import random
import os
import sys
import io
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
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
from modules.database import (
    init_db,
    get_active_signals,
    save_signal_to_db,
    get_closed_trades_today,
    get_trades_today,
    get_paper_balance,
)
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
# Set "debug": true di config.json untuk verbose output
# ─────────────────────────────────────────────────────────────────────────────
LOG_LEVEL = logging.DEBUG if CONFIG.get("debug", False) else logging.INFO

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),                              # print ke terminal (UTF-8)
        RotatingFileHandler(                                                # rotasi otomatis, maks 5 MB × 3 backup
            "data/bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("Main")


# ─────────────────────────────────────────────────────────────────────────────
# Mode & Config
# ─────────────────────────────────────────────────────────────────────────────
AUTO_TRADE_ENABLED = CONFIG.get("auto_trade", False)
DEBUG_MODE         = CONFIG.get("debug", False)
RISK_CFG           = CONFIG.get("risk", {})
DAILY_PROFIT_TARGET = RISK_CFG.get("daily_profit_target_pct", 0.015)
MAX_DAILY_LOSS       = RISK_CFG.get("max_daily_loss_pct", 0.01)
MAX_DAILY_TRADES     = RISK_CFG.get("max_daily_trades", 3)

print("=" * 60)
print("🤖 Bybit Screening Bot v8")
print("=" * 60)
print(f"   Mode    : {'AUTO TRADE 🤖' if AUTO_TRADE_ENABLED else 'PAPER TRADE 📋 (signal + simulasi)'}")
print(f"   Debug   : {'ON 🔍' if DEBUG_MODE else 'OFF (set \"debug\": true di config.json untuk verbose)'}")
print(f"   Env     : {CONFIG.get('env', 'PROD')}")
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
# FIX #4: BTC Bias — EMA 13/21 + ADX + price-position filter
#
# Masalah lama: hanya EMA13 > EMA21 → banyak false signal di sideways market.
# Fix: tambahkan ADX ≥ threshold (default 20) dan pastikan close berada di sisi
# yang benar dari kedua EMA, sehingga kondisi choppy → otomatis "Sideways".
BTC_BIAS_ADX_MIN = float(CONFIG.get("strategy", {}).get("btc_bias_adx_min", 20.0))


def get_btc_bias() -> str:
    """
    BTC directional bias menggunakan EMA 13/21 + ADX(14) + price-position filter
    pada TREND_TF.

    Rules:
      Bullish  → EMA13 > EMA21  AND  close > EMA13  AND  ADX ≥ btc_bias_adx_min
      Bearish  → EMA13 < EMA21  AND  close < EMA21  AND  ADX ≥ btc_bias_adx_min
      Sideways → semua kondisi lain (ADX lemah, EMA crossed tapi price ambiguous)

    Config: tambahkan "btc_bias_adx_min": 20 di bagian "strategy" untuk tuning.
    """
    try:
        df = client.fetch_ohlcv("BTC/USDT:USDT", TREND_TF, limit=100)
        if df is None or len(df) < 30:
            logger.warning("get_btc_bias: data BTC tidak cukup — fallback Sideways")
            return "Sideways"

        df["ema13"] = ta.ema(df["close"], length=13)
        df["ema21"] = ta.ema(df["close"], length=21)

        adx_df  = ta.adx(df["high"], df["low"], df["close"], length=14)
        adx_col = [c for c in adx_df.columns if c.startswith("ADX_")]
        df["adx14"] = adx_df[adx_col[0]] if adx_col else np.nan

        curr = df.iloc[-1]

        if pd.isna(curr["ema13"]) or pd.isna(curr["ema21"]) or pd.isna(curr["adx14"]):
            logger.warning("get_btc_bias: EMA/ADX NaN — fallback Sideways")
            return "Sideways"

        ema13        = float(curr["ema13"])
        ema21        = float(curr["ema21"])
        close        = float(curr["close"])
        adx          = float(curr["adx14"])
        trend_strong = adx >= BTC_BIAS_ADX_MIN

        if ema13 > ema21 and close > ema13 and trend_strong:
            bias = "Bullish"
        elif ema13 < ema21 and close < ema21 and trend_strong:
            bias = "Bearish"
        else:
            bias = "Sideways"

        logger.info(
            f"BTC Bias ({TREND_TF}): {bias} | "
            f"EMA13={ema13:.2f} EMA21={ema21:.2f} Close={close:.2f} "
            f"ADX={adx:.1f} (min={BTC_BIAS_ADX_MIN}, strong={trend_strong})"
        )
        return bias

    except Exception as e:
        logger.error(f"get_btc_bias GAGAL: {type(e).__name__}: {e}")
        return "Sideways"


def get_symbol_trend(symbol: str) -> str:
    """Per-symbol trend pada TREND_TF menggunakan Supertrend (ATR period=10, multiplier=3.5)."""
    try:
        df = client.fetch_ohlcv(symbol, TREND_TF, limit=100)
        if df is None or len(df) < 50:
            return "Sideways"

        st = ta.supertrend(df["high"], df["low"], df["close"],
                           length=10, multiplier=3.5)

        if st is None or st.empty:
            return "Sideways"

        # Kolom direction: SUPERTd_10_3.5 → nilai 1 = Bullish, -1 = Bearish
        direction_col = [c for c in st.columns if c.startswith("SUPERTd")]
        if not direction_col:
            return "Sideways"

        direction = st[direction_col[0]].iloc[-1]

        if pd.isna(direction):
            return "Sideways"

        if direction == 1:
            return "Bullish"
        elif direction == -1:
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


ATR_SL_MULTIPLIER       = float(CONFIG.get("risk", {}).get("atr_sl_multiplier", 1.5))
ATR_SL_LENGTH           = int(CONFIG.get("risk", {}).get("atr_sl_length", 14))
STRATEGY_CFG            = CONFIG.get("strategy", {})
MIN_ADX                 = float(STRATEGY_CFG.get("min_adx", 18))
MIN_TP1_DIST_PCT        = float(STRATEGY_CFG.get("min_tp1_distance_pct", 0.003))
MIN_ATR_PCT             = float(STRATEGY_CFG.get("min_atr_pct", 0.0015))
MAX_ATR_PCT             = float(STRATEGY_CFG.get("max_atr_pct", 0.03))
REQUIRE_SL_BEYOND_SWING = bool(STRATEGY_CFG.get("require_sl_beyond_swing", True))
SL_SWING_BUFFER_ATR     = float(STRATEGY_CFG.get("sl_swing_buffer_atr", 0.1))


def resolve_atr(df: pd.DataFrame) -> float:
    """
    Ambil ATR terakhir dari candle yang sudah difetch.
    Default fallback hitung ATR 14 langsung dari OHLCV jika perlu.
    """
    if df is None or df.empty:
        return 0.0

    try:
        atr = ta.atr(df["high"], df["low"], df["close"], length=ATR_SL_LENGTH)
        if atr is None or len(atr) == 0:
            return 0.0
        value = float(atr.iloc[-1])
        if np.isfinite(value) and value > 0:
            return value
    except Exception as e:
        logger.debug(f"resolve_atr fallback gagal: {type(e).__name__}: {e}")

    return 0.0


def build_rr_targets(entry: float, sl: float, side: str) -> tuple[float, float, float]:
    """
    Build TP levels from fixed risk distance:
      TP1 = 1R, TP2 = 2R, TP3 = 3R
    """
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0, 0.0, 0.0

    if side == "Long":
        return (
            entry + risk * 1.0,
            entry + risk * 2.0,
            entry + risk * 3.0,
        )
    return (
        entry - risk * 1.0,
        entry - risk * 2.0,
        entry - risk * 3.0,
    )


def entry_quality_reject_reason(
    df: pd.DataFrame,
    side: str,
    entry: float,
    sl: float,
    tp1: float,
    atr: float,
    swing_high: float,
    swing_low: float,
) -> str | None:
    if entry <= 0:
        return "entry<=0"

    adx = float(df["adx"].iloc[-1]) if "adx" in df.columns else 0.0
    if MIN_ADX and adx < MIN_ADX:
        return f"ADX {adx:.1f} < {MIN_ADX:.1f}"

    atr_pct = atr / entry
    if MIN_ATR_PCT and atr_pct < MIN_ATR_PCT:
        return f"ATR {atr_pct:.2%} < min {MIN_ATR_PCT:.2%}"
    if MAX_ATR_PCT and atr_pct > MAX_ATR_PCT:
        return f"ATR {atr_pct:.2%} > max {MAX_ATR_PCT:.2%}"

    tp1_dist = abs(tp1 - entry) / entry
    if MIN_TP1_DIST_PCT and tp1_dist < MIN_TP1_DIST_PCT:
        return f"TP1 distance {tp1_dist:.2%} < {MIN_TP1_DIST_PCT:.2%}"

    if REQUIRE_SL_BEYOND_SWING:
        buffer = atr * SL_SWING_BUFFER_ATR
        if side == "Long" and sl > (swing_low - buffer):
            return "SL not beyond swing low"
        if side == "Short" and sl < (swing_high + buffer):
            return "SL not beyond swing high"

    return None


# ─────────────────────────────────────────────────────────────────────────────
# FIX #3: analyze_ticker() di-refactor menjadi fungsi-fungsi kecil
#
# Masalah lama: satu fungsi monolitik 200+ baris, 13 step, sulit di-test/debug.
# Fix: setiap kelompok step menjadi fungsi tersendiri dengan return type eksplisit.
# analyze_ticker() sekarang hanya orkestrator tipis.
# ─────────────────────────────────────────────────────────────────────────────

def _step_fetch_market_data(
    symbol: str,
    counters: dict,
) -> "tuple[dict, pd.DataFrame] | None":
    """Steps 2–4: Fetch ticker + filter settlement token + fetch OHLCV."""
    ticker_info = client.fetch_ticker(symbol)
    if ticker_info is None:
        counters["no_ticker"] += 1
        logger.debug(f"[{symbol}] skip — ticker kosong/invalid")
        return None

    if "ST" in ticker_info.get("info", {}).get("symbol", ""):
        return None

    min_candles = CONFIG["system"].get("min_candles_analysis", 150)
    df = client.fetch_ohlcv(symbol, ENTRY_TF, limit=min_candles + 50)
    if df is None or len(df) < min_candles:
        counters["no_candles"] += 1
        logger.debug(
            f"[{symbol}] skip — OHLCV kurang "
            f"({len(df) if df is not None else 0}/{min_candles} bars)"
        )
        return None

    return ticker_info, df


def _step_technicals_and_pattern(
    df: pd.DataFrame,
    symbol: str,
    counters: dict,
) -> "tuple[pd.DataFrame, str, str] | None":
    """Step 5: Hitung technical indicators + deteksi pattern + lookup side."""
    df      = get_technicals(df)
    pattern = find_pattern(df)

    if not pattern:
        counters["no_pattern"] += 1
        return None

    side = CONFIG["pattern_signals"].get(pattern)
    if not side:
        logger.warning(f"[{symbol}] pattern '{pattern}' tidak ada di pattern_signals config")
        counters["no_pattern"] += 1
        return None

    return df, pattern, side


def _step_alignment_filters(
    symbol: str,
    side: str,
    symbol_trend: str,
    btc_bias: str,
    counters: dict,
) -> "str | None":
    """
    Steps 6–7: MTF alignment (Supertrend vs pattern) + BTC bias filter.
    Returns reject-reason string jika harus skip, None jika lolos.
    """
    if symbol_trend == "Bearish" and side == "Long":
        counters["mtf_conflict"] += 1
        logger.debug(f"[{symbol}] skip — MTF conflict: trend Bearish tapi signal Long")
        return "mtf_conflict"
    if symbol_trend == "Bullish" and side == "Short":
        counters["mtf_conflict"] += 1
        logger.debug(f"[{symbol}] skip — MTF conflict: trend Bullish tapi signal Short")
        return "mtf_conflict"

    if "Bearish" in btc_bias and side == "Long":
        counters["btc_filter"] += 1
        return "btc_filter"
    if "Bullish" in btc_bias and side == "Short":
        counters["btc_filter"] += 1
        return "btc_filter"

    return None


def _step_score_filters(
    df: pd.DataFrame,
    symbol: str,
    side: str,
    ticker_info: dict,
    pattern: str,
    counters: dict,
) -> "dict | None":
    """
    Steps 8–11: SMC, Quant+OB, Derivatives, Divergence+Tech score, RVOL.
    Returns scores dict jika lolos semua filter, None jika skip.
    """
    # SMC
    valid_smc, smc_score, smc_reasons = analyze_smc(df, side)
    min_smc = CONFIG["strategy"].get("min_smc_score", 0)
    if not valid_smc or smc_score < min_smc:
        counters["smc_fail"] += 1
        logger.debug(f"[{symbol}] skip — SMC fail: valid={valid_smc} score={smc_score} | {smc_reasons}")
        return None

    # Order Book + Quant
    try:
        order_book = client.raw.fetch_order_book(symbol, limit=10)
    except Exception as _ob_err:
        logger.debug(f"[{symbol}] order book fetch failed: {_ob_err} — OBI skipped")
        order_book = {}

    df, basis, z_score, zeta_score, obi, quant_score, quant_reasons = calculate_metrics(
        df, ticker_info, order_book
    )

    # Derivatives
    valid_deriv, deriv_score, deriv_reasons = analyze_derivatives(df, ticker_info, side)
    min_deriv = CONFIG["strategy"].get("min_deriv_score", 0)
    if not valid_deriv or deriv_score < min_deriv:
        counters["deriv_fail"] += 1
        logger.debug(f"[{symbol}] skip — Deriv fail: valid={valid_deriv} score={deriv_score} | {deriv_reasons}")
        return None

    # Divergence + Tech Score
    div_score, div_msg = detect_divergence(df)
    tech_score   = 3 + div_score + min(smc_score, 2)
    tech_reasons = [f"Pattern: {pattern}", div_msg] + smc_reasons
    min_tech     = CONFIG["strategy"]["min_tech_score"]
    if tech_score < min_tech:
        counters["low_tech_score"] += 1
        logger.debug(f"[{symbol}] skip — tech_score={tech_score} < min={min_tech}")
        return None

    # RVOL / Fakeout
    min_rvol = CONFIG["indicators"]["min_rvol"]
    valid_fo, fo_msg = check_fakeout(df, min_rvol)
    if not valid_fo:
        counters["low_rvol"] += 1
        rvol_val = df["RVOL"].iloc[-1]
        logger.debug(f"[{symbol}] skip — RVOL={rvol_val:.2f} < min={min_rvol} | {fo_msg}")
        return None

    return {
        "df":            df,
        "smc_score":     smc_score,   "smc_reasons":   smc_reasons,
        "quant_score":   quant_score, "quant_reasons": quant_reasons,
        "deriv_score":   deriv_score, "deriv_reasons": deriv_reasons,
        "tech_score":    tech_score,  "tech_reasons":  tech_reasons,
        "basis":         basis,       "z_score":       z_score,
        "zeta_score":    zeta_score,  "obi":           obi,
    }


def _step_build_trade_setup(
    df: pd.DataFrame,
    ticker_info: dict,
    side: str,
    symbol: str,
    counters: dict,
) -> "dict | None":
    """
    FIX #1 + Step 12: Hitung entry/SL/TP dan validasi R:R.

    FIX #1 — Entry dari bid/ask live, bukan kalkulasi Fib swing:
      Sebelumnya entry = rata-rata (swing_high − range×0.5, swing_high − range×0.618).
      Ini sering sudah lewat dari harga saat ini → banyak reject di max_entry_drift_pct.
      Sekarang: Long entry = ask, Short entry = bid — selalu actionable.
      Filter max_entry_drift_pct tidak lagi diperlukan dan dihapus dari pipeline ini.
    """
    swing_high = df["high"].iloc[-50:].max()
    swing_low  = df["low"].iloc[-50:].min()
    rng        = swing_high - swing_low

    if rng <= 0:
        counters["bad_setup"] += 1
        logger.debug(f"[{symbol}] skip — range=0 (harga flat?)")
        return None

    atr = resolve_atr(df)
    if atr <= 0:
        counters["bad_setup"] += 1
        logger.debug(f"[{symbol}] skip — ATR invalid/zero")
        return None

    # FIX #1: bid/ask live sebagai entry point
    last_price = float(ticker_info.get("last", 0))
    bid        = float(ticker_info.get("bid", last_price))
    ask        = float(ticker_info.get("ask", last_price))

    if last_price <= 0:
        counters["bad_setup"] += 1
        logger.debug(f"[{symbol}] skip — last_price=0 (ticker stale?)")
        return None

    entry = ask if side == "Long" else bid
    sl    = (entry - atr * ATR_SL_MULTIPLIER) if side == "Long" else (entry + atr * ATR_SL_MULTIPLIER)
    tp1, tp2, tp3 = build_rr_targets(entry, sl, side)

    quality_reason = entry_quality_reject_reason(
        df, side, entry, sl, tp1, atr, swing_high, swing_low
    )
    if quality_reason:
        counters["entry_quality"] += 1
        counters[f"entry_quality:{quality_reason.split()[0]}"] += 1
        logger.debug(f"[{symbol}] skip — entry quality: {quality_reason}")
        return None

    rr     = calculate_rr(entry, sl, tp3)
    min_rr = CONFIG["strategy"].get("risk_reward_min", 2.0)
    if rr < min_rr:
        counters["low_rr"] += 1
        logger.debug(f"[{symbol}] skip — R:R={rr} < min={min_rr}")
        return None

    return {
        "entry": float(entry), "sl":  float(sl),
        "tp1":   float(tp1),   "tp2": float(tp2), "tp3": float(tp3),
        "rr":    float(rr),
    }


# ─────────────────────────────────────────────────────────────────────────────
# analyze_ticker — orkestrator (setelah FIX #3 refactor)
# ─────────────────────────────────────────────────────────────────────────────
def analyze_ticker(symbol: str, btc_bias: str, active_signals: set, counters: dict):
    """
    Multi-Timeframe analysis per symbol — orkestrator tipis setelah refactor.
    Return dict hasil jika lolos semua filter, None jika tidak.

    Pipeline:
      _step_fetch_market_data()      → ticker + OHLCV
      _step_technicals_and_pattern() → indicators + pattern + side
      _step_alignment_filters()      → MTF & BTC bias
      _step_score_filters()          → SMC, Quant, Deriv, Tech, RVOL
      _step_build_trade_setup()      → entry (bid/ask), SL, TP, R:R
    """
    if (symbol, ENTRY_TF) in active_signals:
        counters["duplicate"] += 1
        return None

    step = "init"
    try:
        step = "fetch_market_data"
        market_data = _step_fetch_market_data(symbol, counters)
        if market_data is None:
            return None
        ticker_info, df = market_data

        step = "symbol_trend"
        symbol_trend = get_symbol_trend(symbol)

        step = "technicals_pattern"
        pattern_data = _step_technicals_and_pattern(df, symbol, counters)
        if pattern_data is None:
            return None
        df, pattern, side = pattern_data

        step = "alignment_filters"
        if _step_alignment_filters(symbol, side, symbol_trend, btc_bias, counters):
            return None

        step = "score_filters"
        scores = _step_score_filters(df, symbol, side, ticker_info, pattern, counters)
        if scores is None:
            return None
        df = scores["df"]

        step = "build_trade_setup"
        setup = _step_build_trade_setup(df, ticker_info, side, symbol, counters)
        if setup is None:
            return None

        df["funding"] = float(ticker_info.get("info", {}).get("fundingRate", 0))

        total_score = (
            scores["tech_score"] + scores["smc_score"]
            + scores["quant_score"] + scores["deriv_score"]
        )
        logger.info(
            f"✅ SIGNAL [{symbol}] {side} | {pattern} | {ENTRY_TF} | "
            f"RR={setup['rr']} | Score={total_score} "
            f"(tech={scores['tech_score']} smc={scores['smc_score']} "
            f"quant={scores['quant_score']} deriv={scores['deriv_score']})"
        )

        return {
            "Symbol":       symbol,
            "Side":         side,
            "Timeframe":    ENTRY_TF,
            "Trend_TF":     TREND_TF,
            "Symbol_Trend": symbol_trend,
            "Pattern":      pattern,
            "Entry":  setup["entry"], "SL":  setup["sl"],
            "TP1":    setup["tp1"],   "TP2": setup["tp2"], "TP3": setup["tp3"],
            "RR":     setup["rr"],
            "Tech_Score":  int(scores["tech_score"]),
            "Quant_Score": int(scores["quant_score"]),
            "Deriv_Score": int(scores["deriv_score"]),
            "SMC_Score":   int(scores["smc_score"]),
            "Basis":      float(scores["basis"]),
            "Z_Score":    float(scores["z_score"]),
            "Zeta_Score": float(scores["zeta_score"]),
            "OBI":        float(scores["obi"]),
            "BTC_Bias":   btc_bias,
            "Reason":     pattern,
            "Tech_Reasons":  ", ".join(str(r) for r in scores["tech_reasons"]),
            "Quant_Reasons": ", ".join(str(r) for r in scores["quant_reasons"]),
            "SMC_Reasons":   ", ".join(str(r) for r in scores["smc_reasons"] if r),
            "Deriv_Reasons": ", ".join(str(r) for r in scores["deriv_reasons"]),
            "df": df,
        }

    except Exception as e:
        counters["exception"] += 1
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
def is_active_hour() -> bool:
    """
    Kembalikan True jika jam UTC saat ini berada dalam window aktif.
    Window dikonfigurasi via system.active_hours_utc: [start, end] (inklusif start, eksklusif end).
    Default: [6, 22] → 06:00–22:00 UTC (sesi London + New York).
    """
    window   = CONFIG["system"].get("active_hours_utc", [6, 22])
    start_h  = int(window[0])
    end_h    = int(window[1])
    current_h = datetime.utcnow().hour
    return start_h <= current_h < end_h


def _account_balance_for_daily_guard() -> float:
    if not AUTO_TRADE_ENABLED:
        return get_paper_balance()
    try:
        balance_info = client.fetch_balance()
        return float(balance_info["total"]["USDT"])
    except Exception as e:
        logger.debug(f"daily guard balance fetch failed: {e}")
        return 0.0


def daily_entry_limit_status() -> tuple[bool, str, int]:
    trades_today = get_trades_today()
    remaining = max(0, int(MAX_DAILY_TRADES or 0) - len(trades_today)) if MAX_DAILY_TRADES else 999999
    if MAX_DAILY_TRADES and remaining <= 0:
        return True, f"max daily trades {len(trades_today)}/{MAX_DAILY_TRADES}", 0

    closed_today = get_closed_trades_today()
    total_pnl = sum(float(t.get("pnl", 0)) for t in closed_today)
    balance = _account_balance_for_daily_guard()
    if balance <= 0:
        return False, "", remaining

    pnl_pct = total_pnl / balance
    if DAILY_PROFIT_TARGET and pnl_pct >= DAILY_PROFIT_TARGET:
        return True, f"daily target hit {pnl_pct:.2%} >= {DAILY_PROFIT_TARGET:.2%}", 0
    if MAX_DAILY_LOSS and pnl_pct <= -MAX_DAILY_LOSS:
        return True, f"daily loss hit {pnl_pct:.2%} <= -{MAX_DAILY_LOSS:.2%}", 0

    return False, "", remaining


def scan():
    # ✅ Cek pause flag dari Telegram /pause command
    if is_paused():
        logger.info("⏸ Scan dilewati — bot sedang di-pause via Telegram (/resume untuk lanjutkan)")
        return

    # ✅ Cek active hour window (default 06:00–22:00 UTC)
    if not is_active_hour():
        window = CONFIG["system"].get("active_hours_utc", [6, 22])
        logger.info(
            f"🌙 Scan dilewati — di luar jam aktif "
            f"(sekarang {datetime.utcnow().strftime('%H:%M')} UTC, "
            f"aktif {window[0]:02d}:00–{window[1]:02d}:00 UTC)"
        )
        return

    start_time = time.time()
    mode_label = "AUTO TRADE 🤖" if AUTO_TRADE_ENABLED else "SIGNAL ONLY 📡"
    logger.info(f"🔭 Scan dimulai | Mode: {mode_label}")

    blocked, reason, max_new_signals = daily_entry_limit_status()
    if blocked:
        logger.info(f"🛑 Scan dilewati — {reason}")
        return

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
                    if signal_count >= max_new_signals:
                        counters["daily_trade_limit"] += 1
                        continue
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
            "entry_too_far":  f"Entry terlalu jauh dari harga saat ini (> {CONFIG['strategy'].get('max_entry_drift_pct', 0.03):.0%})",
            "entry_quality":  "Entry quality filter fail",
            "entry_quality:ADX": "Entry quality: ADX lemah",
            "entry_quality:ATR": "Entry quality: ATR tidak ideal",
            "entry_quality:TP1": "Entry quality: TP1 terlalu dekat",
            "entry_quality:SL":  "Entry quality: SL swing rule",
            "bad_setup":      "Setup invalid (range=0)",
            "daily_trade_limit": "Kuota trade harian sudah penuh",
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
    print(f"💡 Tip: set \"debug\": true di config.json untuk verbose output\n")
    while True:
        schedule.run_pending()
        time.sleep(1)
import time
import schedule
import random
import os
import sys
import io
import logging
from datetime import datetime, timezone
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
    get_active_trades_today,
    get_paper_balance,
    purge_old_data,
    get_candle_confirm_state,
    set_candle_confirm_state,
    delete_candle_confirm_state,
)
from modules.technicals import get_technicals, detect_divergence
from modules.quant import calculate_metrics, check_fakeout
from modules.derivatives import analyze_derivatives
from modules.smc import analyze_smc
from modules.patterns import find_pattern, pattern_direction
from modules.pattern_registry import detect_all_patterns
from modules.regime import classify_regime, regime_allows
from modules.range_strategy import find_range_signal, range_strategy_enabled
from modules.watchlist import refresh_watchlist, get_watchlist, get_watchlist_info
from modules.paper_runner import start_paper_runner
from modules.telegram_commands import start_command_listener, is_paused
from modules.telegram_bot import send_alert, update_status_dashboard, send_scan_completion
# Note: run_paper_update tidak lagi dijadwal terpisah — daemon paper_runner
# yang menangani semua siklus. Import dihapus untuk menghindari kebingungan.

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup — semua module pakai logger ini
# Set "debug": true di config.json untuk verbose output
# ─────────────────────────────────────────────────────────────────────────────
LOG_LEVEL = logging.DEBUG if CONFIG.get("debug", False) else logging.INFO

# Auto-create data/ before logging.basicConfig so RotatingFileHandler
# does not crash when main.py is imported as a library.
os.makedirs("data", exist_ok=True)

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
DEBUG_MODE         = CONFIG.get("debug", False)
RISK_CFG           = CONFIG.get("risk", {})
DAILY_PROFIT_TARGET = RISK_CFG.get("daily_profit_target_pct", 0.015)
MAX_DAILY_LOSS       = RISK_CFG.get("max_daily_loss_pct", 0.01)
MAX_DAILY_TRADES     = RISK_CFG.get("max_daily_trades", 3)

print("=" * 60)
print("🤖 Bybit Screening Bot v8")
print("=" * 60)
print(f"   Mode    : SIGNAL ONLY 📡 (screening + paper portfolio tracker)")
print(f"   Debug   : {'ON 🔍' if DEBUG_MODE else 'OFF (set \"debug\": true di config.json untuk verbose)'}")
print(f"   Env     : {CONFIG.get('env', 'PROD')}")
print("=" * 60)

ENTRY_TF   = CONFIG["system"].get("entry_timeframe", "15m")
TREND_TF   = CONFIG["system"].get("trend_timeframe", "1h")
CONFIRM_TF = CONFIG["system"].get("confirm_timeframe", "5m")
print(f"📐 Timeframes — Entry: {ENTRY_TF} | Trend: {TREND_TF} | Confirm: {CONFIRM_TF}")

SKIP_WEEKENDS  = bool(CONFIG["system"].get("skip_weekends", True))
SKIP_HOURS_UTC = list(CONFIG["system"].get("skip_hours_utc", []))

_LTF_CFG       = CONFIG.get("strategy", {}).get("ltf_confirmation", {})
LTF_ENABLED    = bool(_LTF_CFG.get("enabled", True))
LTF_LOOKBACK   = int(_LTF_CFG.get("lookback_bars", 3))
LTF_REQUIRE_CLOSE = bool(_LTF_CFG.get("require_close_in_direction", True))

_REGIME_CFG          = CONFIG.get("strategy", {}).get("regime", {})
REGIME_SKIP_ANOMALY  = bool(_REGIME_CFG.get("skip_when_anomaly", True))


# ─────────────────────────────────────────────────────────────────────────────
# Exchange client (singleton)
# ─────────────────────────────────────────────────────────────────────────────
client = BybitClient(debug=DEBUG_MODE)

# ─────────────────────────────────────────────────────────────────────────────
# FIX: Bot startup timestamp — dipakai oleh heartbeat untuk hitung uptime
# ─────────────────────────────────────────────────────────────────────────────
_START_TIME = time.time()


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

# ─── FIX #02 Part A: SL Floor Minimum ────────────────────────────────────────
# SL tidak boleh lebih dekat dari MIN_SL_PCT dari entry.
# Tujuan: cegah SL terlalu ketat (< spread+slippage) yang mudah kena stop hunt.
# Contoh: entry $100, min_sl_pct=0.005 → SL Long max $99.50, SL Short min $100.50
# Config: tambahkan "min_sl_pct": 0.005 di bagian "strategy" untuk override.
MIN_SL_PCT = float(STRATEGY_CFG.get("min_sl_pct", 0.005))  # default 0.5%

# ─── FIX #08: Multi-TF Pattern Confluence ─────────────────────────────────────
# Cek apakah higher timeframe (TREND_TF = 1h) juga menunjukkan pattern yang
# SEARAH dengan entry signal. Jika 1h menampilkan pattern BERLAWANAN → reject.
# Jika 1h tidak ada pattern sama sekali → lolos (tidak ada conflict).
#
# Rules:
#   HTF pattern searah (Long+Long / Short+Short) → STRONG confluence ✅ lolos
#   HTF tidak ada pattern                         → WEAK  confluence ✅ lolos
#   HTF pattern berlawanan (Long vs Short)        → conflict ❌ reject
#
# Config: set "mtf_confluence_enabled": false di bagian "strategy" untuk disable.
MTF_CONFLUENCE_ENABLED = bool(STRATEGY_CFG.get("mtf_confluence_enabled", True))

# ─── FIX #05: Correlation Filter antar Posisi ─────────────────────────────────
# Tolak signal jika sudah ada posisi aktif dengan korelasi return ≥ threshold.
# Mencegah simultan Long BTC + ETH + BNB yang semua correlated >0.85 — satu event
# BTC dump bisa kena SL 3 posisi sekaligus = 3× risk dalam satu kejadian.
#
# Config (bagian "strategy" di config.json):
#   "correlation_filter_enabled": true   ← toggle on/off
#   "correlation_threshold":      0.7    ← absolute z-score threshold (fix I)
#   "correlation_lookback":       50     ← jumlah candle untuk hitung korelasi
#   "correlation_use_zscore":     true   ← rolling z-score returns (resistant)
#   "correlation_sector_max":     1      ← max posisi aktif per "sektor" coin
CORRELATION_ENABLED      = bool(STRATEGY_CFG.get("correlation_filter_enabled", True))
CORRELATION_THRESHOLD    = float(STRATEGY_CFG.get("correlation_threshold", 0.85))
CORRELATION_LOOKBACK     = int(STRATEGY_CFG.get("correlation_lookback", 50))
CORRELATION_USE_ZSCORE   = bool(STRATEGY_CFG.get("correlation_use_zscore", True))
CORRELATION_SECTOR_MAX   = int(STRATEGY_CFG.get("correlation_sector_max", 1))

# Cheap symbol → sector mapping. Edit / extend in code or via CONFIG override.
# Keys are uppercase base symbols, values are sector tags. Anything not listed
# is treated as sector "alt".
SECTOR_MAP_DEFAULT = {
    "BTC": "majors", "ETH": "majors",
    "BNB": "majors",
    "SOL": "L1", "AVAX": "L1", "ADA": "L1", "DOT": "L1",
    "ARB": "L2",  "OP": "L2",  "MATIC": "L2",
    "LINK": "oracle",
    "DOGE": "meme", "SHIB": "meme", "PEPE": "meme", "WIF": "meme",
    "UNI": "defi", "AAVE": "defi", "CAKE": "defi", "CRV": "defi",
    "FIL": "storage", "AR": "storage",
}
SECTOR_MAP = {**SECTOR_MAP_DEFAULT, **(STRATEGY_CFG.get("sector_map", {}) or {})}


def _symbol_sector(symbol: str) -> str:
    base = symbol.split("/")[0].upper()
    return SECTOR_MAP.get(base, "alt")

# ─── FIX #5: Candle Confirmation ──────────────────────────────────────────────
# Tunggu satu candle close setelah pattern terbentuk sebelum entry.
# Mengurangi false signal ~15–25% karena entry hanya terjadi ketika pattern
# sudah terkonfirmasi close, bukan saat intracandle.
CANDLE_CONFIRM_ENABLED  = bool(STRATEGY_CFG.get("candle_confirmation", True))

# ─── FIX #6: Limit Order Zone ─────────────────────────────────────────────────
# Entry limit order dipasang 0.1–0.3% lebih baik dari bid/ask saat ini.
# Long: entry = ask × (1 − offset)  → sedikit di bawah ask, harga lebih baik
# Short: entry = bid × (1 + offset) → sedikit di atas bid, harga lebih baik
# Meningkatkan R:R per trade, mengurangi fill rate ~20–30% pada market volatile.
LIMIT_ORDER_OFFSET_PCT  = float(STRATEGY_CFG.get("limit_order_offset_pct", 0.002))

# ─── FIX #07: Candle Confirm State — DB-backed ────────────────────────────────
# State tracker per-symbol untuk candle confirmation.
# SEBELUMNYA: in-memory dict → hilang setiap restart bot.
# SEKARANG  : disimpan ke DB (state_store) → survive restart.
#
# Key format : "candle_confirm:{symbol}:{pattern}:{side}"
# Value      : JSON {"bar_ts": <int ms>, "saved_at": <float unix>}
#
# Cleanup    : purge_candle_confirm_state(max_age_hours=4) dipanggil
#              otomatis oleh purge_old_data() setiap jam 03:00.


def _check_candle_confirmation(symbol: str, pattern: str, side: str, df: pd.DataFrame) -> bool:
    """
    FIX #5 + FIX #07 — Candle Confirmation Gate (DB-backed).

    Memastikan satu candle penuh telah CLOSE setelah pattern terbentuk sebelum
    sinyal dikirim. State disimpan ke SQLite sehingga restart bot tidak mereset
    semua antrian konfirmasi.

    Cara kerja:
      - Scan pertama  : pattern terdeteksi → state disimpan ke DB, return False (tunggu)
      - Scan berikutnya: bar timestamp baru → pattern terkonfirmasi, state dihapus dari DB
      - Jika pattern hilang / side berubah  → state di-reset saat pattern terdeteksi lagi

    Returns True jika pattern sudah terkonfirmasi candle close, False jika masih menunggu.
    Selalu True jika CANDLE_CONFIRM_ENABLED = False.
    """
    if not CANDLE_CONFIRM_ENABLED:
        return True

    # Ambil timestamp bar terakhir (int ms)
    try:
        idx = df.index[-1]
        last_bar_ts = int(idx.timestamp() * 1000) if hasattr(idx, "timestamp") else int(idx)
    except Exception:
        return True  # Tidak bisa tentukan timestamp → loloskan

    db_key  = f"candle_confirm:{symbol}:{pattern}:{side}"
    prev_ts = get_candle_confirm_state(db_key)

    if prev_ts is None:
        # Pertama kali pattern ini terdeteksi — simpan ke DB dan tunggu candle berikutnya
        set_candle_confirm_state(db_key, last_bar_ts)
        logger.debug(
            f"[{symbol}] ⏳ Candle confirm: {pattern} {side} terdaftar ke DB, "
            f"menunggu candle berikutnya (ts={last_bar_ts})"
        )
        return False

    if last_bar_ts <= prev_ts:
        # Masih candle yang sama — masih menunggu
        logger.debug(f"[{symbol}] ⏳ Candle confirm: masih candle sama (ts={last_bar_ts})")
        return False

    # Bar baru sudah close → pattern terkonfirmasi → hapus state dari DB
    delete_candle_confirm_state(db_key)
    logger.debug(
        f"[{symbol}] ✅ Candle confirm: {pattern} {side} terkonfirmasi "
        f"(prev_ts={prev_ts} → new_ts={last_bar_ts})"
    )
    return True


def _step_ltf_confirmation(symbol: str, side: str) -> bool:
    """
    Fix L — Multi-step LTF (5m) confirmation.

    After all gates pass on the entry TF, fetch the confirmation TF (default 5m)
    and require that the *most recent closed bar* is in the trade direction.
    This filters entries that look fine on 15m but are already mid-fade on 5m.

    Returns True if the LTF confirms (or feature disabled). Returns False if
    the LTF is in conflict — caller should reject the signal.
    """
    if not LTF_ENABLED:
        return True
    try:
        df_ltf = client.fetch_ohlcv(symbol, CONFIRM_TF, limit=max(LTF_LOOKBACK + 5, 10))
        if df_ltf is None or len(df_ltf) < LTF_LOOKBACK + 1:
            logger.debug(f"[{symbol}] LTF data insufficient — fail-open")
            return True
        recent = df_ltf.iloc[-(LTF_LOOKBACK + 1):]
        first_close = float(recent["close"].iloc[0])
        last_close  = float(recent["close"].iloc[-1])
        if not LTF_REQUIRE_CLOSE:
            return True
        if side == "Long" and last_close < first_close:
            logger.debug(
                f"[{symbol}] LTF reject Long: {first_close:.6g} → {last_close:.6g}"
            )
            return False
        if side == "Short" and last_close > first_close:
            logger.debug(
                f"[{symbol}] LTF reject Short: {first_close:.6g} → {last_close:.6g}"
            )
            return False
        return True
    except Exception as e:
        logger.debug(f"[{symbol}] LTF confirm error {type(e).__name__}: {e} — fail-open")
        return True


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

    # NaN-aware: float(NaN) < MIN_ADX evaluates to False in Python which
    # would silently let candles with corrupted indicator data bypass the
    # gate. Treat both "missing column" and "NaN value" as fail-closed.
    if "adx" not in df.columns:
        adx = 0.0
    else:
        try:
            raw_adx = float(df["adx"].iloc[-1])
            adx = raw_adx if np.isfinite(raw_adx) else 0.0
        except Exception:
            adx = 0.0
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
) -> "tuple[pd.DataFrame, str, str, dict] | None":
    """
    Step 5: Hitung technical indicators + klasifikasi regime + deteksi pattern.

    Routing (fix E + K):
      - regime ANOMALY               → reject (flash event, skip)
      - regime RANGE / SQUEEZE       → coba range-strategy dulu, fall through
                                       ke chart-pattern hanya kalau tidak ada
                                       (chart pattern di range akan auto-reject
                                       lewat ADX gate di patterns.py)
      - regime TREND_BULL / BEAR     → langsung chart pattern path
    """
    df      = get_technicals(df)
    regime  = classify_regime(df)

    if regime["label"] == "ANOMALY" and REGIME_SKIP_ANOMALY:
        counters["regime_anomaly"] += 1
        logger.debug(f"[{symbol}] skip — regime ANOMALY: {regime['reason']}")
        return None

    pattern = None
    side    = None

    if regime["label"] in ("RANGE", "SQUEEZE") and range_strategy_enabled():
        rng_sig = find_range_signal(df, regime)
        if rng_sig is not None:
            pattern = rng_sig["pattern"]
            side    = CONFIG["pattern_signals"].get(pattern) or rng_sig["side"]
            logger.debug(
                f"[{symbol}] range-mode signal: {pattern} {side} | {rng_sig['reason']}"
            )

    if pattern is None:
        pattern = find_pattern(df)
        if not pattern:
            counters["no_pattern"] += 1
            return None
        side = CONFIG["pattern_signals"].get(pattern)
        if not side:
            logger.warning(f"[{symbol}] pattern '{pattern}' tidak ada di pattern_signals config")
            counters["no_pattern"] += 1
            return None

    # Regime / side compatibility check (e.g. block Long in TREND_BEAR)
    if not regime_allows(regime, side):
        counters["regime_conflict"] += 1
        logger.debug(
            f"[{symbol}] skip — regime {regime['label']} blocks {side} "
            f"({regime['reason']})"
        )
        return None

    return df, pattern, side, regime


def _step_mtf_pattern_confluence(
    symbol: str,
    side: str,
    counters: dict,
) -> "tuple[str | None, str]":
    """
    FIX #08 — Multi-TF Pattern Confluence.

    Fetch TREND_TF (1h) OHLCV dan jalankan find_pattern() di sana.
    Bandingkan arah pattern 1h dengan arah entry signal 15m.

    Returns:
        (reject_reason, confluence_label)
        - reject_reason : None jika lolos, "mtf_confluence_fail" jika harus skip
        - confluence_label : "STRONG" | "WEAK" | "SKIP" (untuk logging & notif)

    Logic:
        HTF pattern searah   → (None, "STRONG")   ← lolos, konfirmasi berlapis
        HTF tidak ada pattern → (None, "WEAK")     ← lolos, tidak ada konflik
        HTF pattern berlawanan → ("mtf_confluence_fail", "CONFLICT") ← reject
        MTF disabled / error   → (None, "SKIP")   ← bypass, jangan reject

    Config: "mtf_confluence_enabled": false di bagian "strategy" untuk disable.
    """
    if not MTF_CONFLUENCE_ENABLED:
        return None, "SKIP"

    try:
        min_candles = CONFIG["system"].get("min_candles_analysis", 150)
        df_htf = client.fetch_ohlcv(symbol, TREND_TF, limit=min_candles + 50)

        if df_htf is None or len(df_htf) < 50:
            logger.debug(
                f"[{symbol}] MTF confluence: data {TREND_TF} tidak cukup "
                f"({len(df_htf) if df_htf is not None else 0} bars) — bypass"
            )
            return None, "SKIP"

        htf_pattern = find_pattern(df_htf)

        if htf_pattern is None:
            # Tidak ada pattern jelas di 1h → tidak ada konflik, signal bisa lewat
            logger.debug(f"[{symbol}] MTF confluence: tidak ada pattern di {TREND_TF} — WEAK (pass)")
            return None, "WEAK"

        htf_side = pattern_direction(htf_pattern)
        if htf_side is None:
            # pattern_direction tidak kenal pattern ini → bypass aman
            logger.debug(
                f"[{symbol}] MTF confluence: pattern '{htf_pattern}' arah tidak dikenal — bypass"
            )
            return None, "SKIP"

        if htf_side == side:
            logger.debug(
                f"[{symbol}] ✅ MTF confluence STRONG: "
                f"{ENTRY_TF}={side} + {TREND_TF}={htf_pattern}({htf_side})"
            )
            return None, f"STRONG({htf_pattern})"

        # Berlawanan arah → conflict → reject
        counters["mtf_confluence_fail"] += 1
        logger.debug(
            f"[{symbol}] ❌ MTF confluence CONFLICT: "
            f"{ENTRY_TF} signal={side} vs {TREND_TF} pattern={htf_pattern}({htf_side})"
        )
        return "mtf_confluence_fail", "CONFLICT"

    except Exception as e:
        logger.debug(
            f"[{symbol}] MTF confluence error: {type(e).__name__}: {e} — bypass"
        )
        return None, "SKIP"


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
        df, ticker_info, order_book, side=side
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

    # FIX #6: Limit Order Zone — offset 0.1–0.3% dari bid/ask untuk harga entry lebih baik
    # Long : entry sedikit di bawah ask  → pending buy limit, fill saat ada retrace kecil
    # Short: entry sedikit di atas bid   → pending sell limit, fill saat ada bounce kecil
    if LIMIT_ORDER_OFFSET_PCT > 0:
        if side == "Long":
            entry = ask * (1.0 - LIMIT_ORDER_OFFSET_PCT)
        else:
            entry = bid * (1.0 + LIMIT_ORDER_OFFSET_PCT)
        logger.debug(
            f"[{symbol}] Limit order offset {LIMIT_ORDER_OFFSET_PCT:.3%}: "
            f"{'ask' if side == 'Long' else 'bid'}="
            f"{ask if side == 'Long' else bid:.6f} → entry={entry:.6f}"
        )
    sl    = (entry - atr * ATR_SL_MULTIPLIER) if side == "Long" else (entry + atr * ATR_SL_MULTIPLIER)

    # ─── FIX #02 Part A: Enforce SL Floor Minimum ─────────────────────────────
    # Pastikan SL tidak lebih dekat dari MIN_SL_PCT (0.5%) dari entry.
    # Jika ATR terlalu kecil (low-vol pair) → SL otomatis ditarik ke floor.
    # Ini mencegah stop hunt: SL 0.1–0.2% hampir pasti kena spread/slippage.
    if MIN_SL_PCT > 0:
        sl_floor_long  = entry * (1.0 - MIN_SL_PCT)   # SL Long  ≤ floor ini
        sl_floor_short = entry * (1.0 + MIN_SL_PCT)   # SL Short ≥ floor ini
        if side == "Long" and sl > sl_floor_long:
            logger.debug(
                f"[{symbol}] SL floor applied (Long): "
                f"{sl:.6f} → {sl_floor_long:.6f} (min {MIN_SL_PCT:.2%} dari entry)"
            )
            sl = sl_floor_long
        elif side == "Short" and sl < sl_floor_short:
            logger.debug(
                f"[{symbol}] SL floor applied (Short): "
                f"{sl:.6f} → {sl_floor_short:.6f} (min {MIN_SL_PCT:.2%} dari entry)"
            )
            sl = sl_floor_short

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
# FIX #05: Correlation Filter antar Posisi
# ─────────────────────────────────────────────────────────────────────────────

def _step_correlation_filter(
    symbol: str,
    df: pd.DataFrame,
    active_signals: set,
    counters: dict,
) -> "str | None":
    """
    FIX #05 — Correlation Filter.

    Tolak signal jika sudah ada posisi aktif dengan korelasi return ≥
    CORRELATION_THRESHOLD. Mencegah 3 posisi Long bersamaan di BTC, ETH, BNB
    yang semuanya correlated >0.85 — satu event BTC dump kena SL 3 posisi = 3×
    risk dari satu kejadian.

    Algoritma:
      1. Hitung return series kandidat (pct_change) dari OHLCV yang sudah diambil
      2. Untuk tiap symbol di active_signals, fetch OHLCV lalu hitung korelasi Pearson
      3. Jika |corr| ≥ CORRELATION_THRESHOLD → reject

    Returns reject-reason string jika harus skip, None jika lolos.
    """
    if not CORRELATION_ENABLED or not active_signals:
        return None

    active_syms = [s for s, _ in active_signals if s != symbol]
    if not active_syms:
        return None

    # ── Sector group cap (fix I) ─────────────────────────────────────────────
    cand_sector = _symbol_sector(symbol)
    sector_count = sum(1 for s in active_syms if _symbol_sector(s) == cand_sector)
    if cand_sector != "alt" and sector_count >= CORRELATION_SECTOR_MAX:
        counters["correlation_reject"] += 1
        logger.debug(
            f"[{symbol}] skip — sector '{cand_sector}' already has {sector_count} "
            f"active position(s), cap={CORRELATION_SECTOR_MAX}"
        )
        return f"sector_cap_{cand_sector}"

    returns_cand = df["close"].pct_change().dropna().iloc[-CORRELATION_LOOKBACK:]
    if len(returns_cand) < 20:
        return None   # data terlalu pendek untuk korelasi yang bermakna

    # Rolling z-score is resistant to fat tails / outlier candles versus
    # raw Pearson on returns (fix I). Sample correlation between standardised
    # series equals Pearson on standardised inputs but with stable variance.
    def _zscore(arr: np.ndarray) -> np.ndarray:
        if not CORRELATION_USE_ZSCORE:
            return arr
        sd = np.std(arr)
        if sd <= 0 or not np.isfinite(sd):
            return arr
        return (arr - np.mean(arr)) / sd

    cand_z = _zscore(returns_cand.values)

    for active_sym in active_syms[:10]:    # batasi maks 10 cek agar tidak lambat
        try:
            df_active = client.fetch_ohlcv(
                active_sym, ENTRY_TF, limit=CORRELATION_LOOKBACK + 10
            )
            if df_active is None or len(df_active) < 20:
                continue

            returns_active = df_active["close"].pct_change().dropna().iloc[-CORRELATION_LOOKBACK:]
            min_len = min(len(returns_cand), len(returns_active))
            if min_len < 20:
                continue

            a = cand_z[-min_len:]
            b = _zscore(returns_active.values[-min_len:])
            corr_matrix = np.corrcoef(a, b)
            corr = float(corr_matrix[0, 1])

            if not np.isfinite(corr):
                continue

            if abs(corr) >= CORRELATION_THRESHOLD:
                counters["correlation_reject"] += 1
                base_name = active_sym.split("/")[0]
                logger.debug(
                    f"[{symbol}] skip — correlation={corr:.2f} with {active_sym} "
                    f"(threshold={CORRELATION_THRESHOLD}) — portfolio terlalu correlated"
                )
                return f"corr_with_{base_name}"

        except Exception as e:
            logger.debug(f"[{symbol}] correlation check error with {active_sym}: {e}")

    return None   # lolos semua check — diversified enough


# ─────────────────────────────────────────────────────────────────────────────
# analyze_ticker — orkestrator (setelah FIX #3 refactor)
# ─────────────────────────────────────────────────────────────────────────────
def analyze_ticker(symbol: str, btc_bias: str, active_signals: set, counters: dict):
    """
    Multi-Timeframe analysis per symbol — orkestrator tipis setelah refactor.
    Return dict hasil jika lolos semua filter, None jika tidak.

    Pipeline:
      _step_fetch_market_data()        → ticker + OHLCV
      _step_technicals_and_pattern()   → indicators + pattern + side
      _step_alignment_filters()        → MTF & BTC bias
      _step_mtf_pattern_confluence()   → FIX #08: pattern konfirmasi di 1h
      _step_score_filters()            → SMC, Quant, Deriv, Tech, RVOL
      _step_build_trade_setup()        → entry (bid/ask), SL, TP, R:R
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
        df, pattern, side, regime = pattern_data

        step = "alignment_filters"
        if _step_alignment_filters(symbol, side, symbol_trend, btc_bias, counters):
            return None

        # FIX #08 — Multi-TF Pattern Confluence: cek pattern di TREND_TF juga
        step = "mtf_pattern_confluence"
        confluence_reject, confluence_label = _step_mtf_pattern_confluence(
            symbol, side, counters
        )
        if confluence_reject:
            return None

        # FIX #05 — Correlation Filter: tolak jika portfolio sudah terlalu correlated
        step = "correlation_filter"
        corr_reject = _step_correlation_filter(symbol, df, active_signals, counters)
        if corr_reject:
            return None

        # FIX #5 — Candle Confirmation: pastikan pattern sudah close 1 candle penuh
        step = "candle_confirmation"
        if not _check_candle_confirmation(symbol, pattern, side, df):
            counters["candle_wait"] += 1
            return None

        # FIX L — Multi-step LTF confirmation (default 5m close in direction)
        step = "ltf_confirmation"
        if not _step_ltf_confirmation(symbol, side):
            counters["ltf_reject"] += 1
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
            f"RR={setup['rr']} | Score={total_score} | Confluence={confluence_label} "
            f"(tech={scores['tech_score']} smc={scores['smc_score']} "
            f"quant={scores['quant_score']} deriv={scores['deriv_score']})"
        )

        # ─── Phase 8: pattern registry (signal enrichment, not a gate) ──
        # Run the central pattern registry on the same dataframe we just
        # finished analysing. The MTF confluence step has already warmed
        # the OHLCV cache for TREND_TF, so refetching that timeframe is
        # effectively free — pass it in so the multi-TF divergence module
        # can fire confluence hits where ≥2 TFs agree.
        registry_hits: list[dict] = []
        try:
            multi_tf_dfs = {ENTRY_TF: df}
            try:
                min_candles = CONFIG["system"].get("min_candles_analysis", 150)
                df_htf_for_registry = client.fetch_ohlcv(
                    symbol, TREND_TF, limit=min_candles + 50
                )
                if df_htf_for_registry is not None and len(df_htf_for_registry) >= 50:
                    multi_tf_dfs[TREND_TF] = df_htf_for_registry
            except Exception as e:
                logger.debug(f"[{symbol}] registry HTF fetch fail: {e}")
            registry_hits = detect_all_patterns(df, multi_tf_dfs=multi_tf_dfs)
            if registry_hits:
                names = ", ".join(h.get("name", "?") for h in registry_hits[:6])
                more = f" (+{len(registry_hits)-6} more)" if len(registry_hits) > 6 else ""
                logger.info(
                    f"   📚 [{symbol}] registry hits: {len(registry_hits)} — {names}{more}"
                )
        except Exception as e:
            logger.debug(f"[{symbol}] registry detect fail: {e}", exc_info=False)

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
            "MTF_Confluence": confluence_label,        # FIX #08
            "Reason":     pattern,
            "Tech_Reasons":  ", ".join(str(r) for r in scores["tech_reasons"]),
            "Quant_Reasons": ", ".join(str(r) for r in scores["quant_reasons"]),
            "SMC_Reasons":   ", ".join(str(r) for r in scores["smc_reasons"] if r),
            "Deriv_Reasons": ", ".join(str(r) for r in scores["deriv_reasons"]),
            "RegistryHits":  registry_hits,            # Phase 8
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

    Gates (fix J):
      • active_hours_utc [start, end]  inklusif start, eksklusif end (default 6–22 UTC)
      • skip_weekends  : skip Sat/Sun ketika True
      • skip_hours_utc : daftar jam UTC tambahan yang ditolak (mis. low-liquidity window)
    """
    now = datetime.now(timezone.utc)
    if SKIP_WEEKENDS and now.weekday() >= 5:        # 5 = Sat, 6 = Sun
        return False
    if SKIP_HOURS_UTC and now.hour in SKIP_HOURS_UTC:
        return False
    window  = CONFIG["system"].get("active_hours_utc", [6, 22])
    start_h = int(window[0])
    end_h   = int(window[1])
    return start_h <= now.hour < end_h


def _account_balance_for_daily_guard() -> float:
    return get_paper_balance()


def daily_entry_limit_status() -> tuple[bool, str, int]:
    # Fix #4: hanya hitung trade yang benar-benar dibuka (bukan PENDING expire / FAILED)
    trades_today = get_active_trades_today()
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
            f"(sekarang {datetime.now(timezone.utc).strftime('%H:%M')} UTC, "
            f"aktif {window[0]:02d}:00–{window[1]:02d}:00 UTC)"
        )
        return

    start_time = time.time()
    logger.info("🔭 Scan dimulai | Mode: SIGNAL ONLY 📡")

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
                    # send_alert returns Telegram message_id (int) on success, None on failure
                    tg_msg_id = send_alert(res)
                    if tg_msg_id:
                        signal_count += 1
                        # Save to DB so paper_runner can ingest and track virtual PnL.
                        # telegram_msg_id allows paper_runner to reply to the original alert.
                        save_signal_to_db(res, telegram_msg_id=tg_msg_id)
                        logger.info(f"   📡 signal queued: {res['Symbol']} {res['Side']} [{res['Timeframe']}]")

    except Exception as e:
        logger.error(f"Scan Error: {type(e).__name__}: {e}", exc_info=True)

    finally:
        duration = time.time() - start_time

        # ── Filter Breakdown ────────────────────────────────────────────
        total_filtered = sum(counters.values())
        print()
        print(f"📊 Filter Breakdown — {total_filtered} pairs diproses:")
        labels = {
            "no_ticker":           "Ticker kosong / invalid",
            "no_candles":          "OHLCV tidak cukup",
            "no_pattern":          "Tidak ada pattern",
            "mtf_conflict":        "MTF conflict — Supertrend (1h vs 15m)",
            "mtf_confluence_fail": f"MTF confluence fail — pattern {TREND_TF} berlawanan (FIX #08)",
            "btc_filter":          "BTC bias conflict",
            "correlation_reject":  f"Correlation filter — correlated >={CORRELATION_THRESHOLD:.0%} dengan posisi aktif (FIX #05)",
            "smc_fail":            "SMC fail",
            "deriv_fail":          "Derivatives fail",
            "low_rvol":            f"RVOL rendah (< {CONFIG['indicators']['min_rvol']}x)",
            "low_tech_score":      f"Tech score rendah (< {CONFIG['strategy']['min_tech_score']})",
            "low_rr":              f"R:R rendah (< {CONFIG['strategy'].get('risk_reward_min', 2.0)})",
            "entry_too_far":       f"Entry terlalu jauh dari harga saat ini (> {CONFIG['strategy'].get('max_entry_drift_pct', 0.03):.0%})",
            "entry_quality":       "Entry quality filter fail",
            "entry_quality:ADX":   "Entry quality: ADX lemah",
            "entry_quality:ATR":   "Entry quality: ATR tidak ideal",
            "entry_quality:TP1":   "Entry quality: TP1 terlalu dekat",
            "entry_quality:SL":    "Entry quality: SL swing rule",
            "candle_wait":         "Menunggu konfirmasi candle close (FIX #5)",
            "bad_setup":           "Setup invalid (range=0)",
            "daily_trade_limit":   "Kuota trade harian sudah penuh",
            "duplicate":           "Duplikat signal aktif",
            "exception":           "⚠️  Exception (cek log!)",
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
        logger.info(f"✅ Scan selesai {duration:.2f}s | Signals: {signal_count} | Mode: SIGNAL ONLY 📡")
        send_scan_completion(signal_count, duration, btc_bias)


# ─────────────────────────────────────────────────────────────────────────────
# FIX #HEARTBEAT: Kirim status bot ke Telegram setiap jam
#
# Masalah: Tidak ada cara user tahu bot masih hidup tanpa sinyal masuk.
# Fix: Heartbeat otomatis tiap jam berisi uptime, balance, dan trade count.
# Jika heartbeat berhenti → bot mati / hang → user tahu langsung.
# ─────────────────────────────────────────────────────────────────────────────
def send_heartbeat():
    """Kirim status ringkas bot ke Telegram setiap jam sebagai tanda hidup."""
    try:
        uptime_sec    = time.time() - _START_TIME
        uptime_h      = int(uptime_sec // 3600)
        uptime_m      = int((uptime_sec % 3600) // 60)
        trades_today  = get_trades_today()
        trade_count   = len(trades_today)
        mode_label    = "SIGNAL ONLY 📡"

        # Balance (paper portfolio tracker)
        try:
            balance = get_paper_balance()
            bal_str = f"${balance:.2f} (paper)"
        except Exception:
            bal_str = "N/A"

        msg = (
            f"💗 <b>Heartbeat — Bot Aktif</b>\n"
            f"{'─' * 24}\n"
            f"⏱ Uptime       : <code>{uptime_h}h {uptime_m}m</code>\n"
            f"💼 Balance      : <code>{bal_str}</code>\n"
            f"📊 Trades hari ini : <code>{trade_count}/{MAX_DAILY_TRADES or '∞'}</code>\n"
            f"🔧 Mode         : <code>{mode_label}</code>\n"
            f"<i>🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC</i>"
        )
        from modules.telegram_bot import send_alert
        from modules.notifier import send
        send(msg)
        logger.info(
            f"💗 Heartbeat sent — Uptime: {uptime_h}h {uptime_m}m | "
            f"Balance: {bal_str} | Trades: {trade_count}"
        )
    except Exception as e:
        logger.error(f"send_heartbeat error: {type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Watchlist refresh
# ─────────────────────────────────────────────────────────────────────────────
def refresh_daily_watchlist():
    logger.info("🔄 Daily watchlist refresh...")
    symbols = refresh_watchlist(client.raw, top_n=CONFIG["system"].get("watchlist_top_n", 300))
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

    # ── Auto-start Paper Portfolio Tracker (always on in signal-only mode) ──
    print()
    print("📋 Paper Portfolio Tracker — AUTO START")
    print("   (virtual PnL tracking; no real orders are placed)")
    print()
    start_paper_runner()

    # ── Start Telegram Command Listener ───────────────────────────
    print("🎧 Telegram Command Listener — AUTO START")
    print("   Perintah: /start /status /trades /balance /report /pause /resume")
    print()
    start_command_listener()

    # ── Cek koneksi Bybit (public market data only) ─────────────
    ok = client.health_check()
    if not ok:
        print("\n❌ Health check gagal — pastikan:")
        print("   1. Koneksi internet aktif")
        print("   2. Bybit API tidak diblokir (coba VPN jika perlu)")
        print("   Bot tetap berjalan, tapi sinyal mungkin tidak keluar.\n")

    # ── Refresh watchlist saat startup ──────────────────────────
    if not get_watchlist():
        logger.info("📋 Watchlist belum ada — fetch sekarang...")
        refresh_watchlist(client.raw, top_n=CONFIG["system"].get("watchlist_top_n", 300))

    # ── Mulai scan pertama ───────────────────────────────────────
    scan()

    # ── Schedule (utility tasks only; scan dijalankan candle-aligned di loop) ──
    # Catatan ops-hardening-2:
    #   • schedule.every(15).minutes.do(scan)           → DIHAPUS, diganti
    #     dengan candle-aligned wait di loop bawah.
    #   • schedule.every(1).minutes.do(run_paper_update) → DIHAPUS karena
    #     start_paper_runner() sudah jalankan ingest/execute/monitor sebagai
    #     daemon. Dua-duanya jalan = race condition (fix #1A).
    # Jadwal harian dikunci ke timezone user (default Asia/Jakarta lewat
    # system.timezone) — tidak bergantung TZ VPS, tidak bergeser kalau
    # bot dipindahkan host. DB timestamps tetap UTC untuk konsistensi data.
    user_tz = CONFIG.get("system", {}).get("timezone", "Asia/Jakarta")
    schedule.every().day.at("07:00", user_tz).do(refresh_daily_watchlist)
    # FIX #01: Purge data lama setiap hari jam 03:00 user-TZ — cegah disk full
    schedule.every().day.at("03:00", user_tz).do(purge_old_data)
    # FIX #03: Heartbeat ke Telegram tiap jam — operational awareness
    schedule.every(1).hours.do(send_heartbeat)

    # ─── Candle-aligned scan scheduler ────────────────────────────────────
    # Tujuan: scan dipicu tepat setelah candle ENTRY_TF close, supaya
    # OHLCV yang dibaca selalu mengandung candle terbaru yang sudah final.
    # Contoh ENTRY_TF=15m: kalau bot start jam 10:17 → scan berikut pukul
    # 10:30:05, lalu 10:45:05, 11:00:05, dst (5s buffer biar exchange
    # sempat commit candle).
    _TF_SECONDS = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400,
    }
    candle_sec = _TF_SECONDS.get(ENTRY_TF, 900)
    boundary_buffer = float(CONFIG["system"].get("scan_post_close_buffer_sec", 5.0))

    def _next_boundary_epoch(after_epoch: float) -> float:
        """Epoch boundary candle berikutnya setelah `after_epoch`, plus buffer.

        Contoh ENTRY_TF=15m, buffer=5s:
          after=10:17:00 → return 10:30:05  (scan candle 10:15-10:30)
          after=10:30:04 → return 10:30:05  (scan candle yang baru close)
          after=10:30:05 → return 10:45:05  (sudah lewat, ke candle berikut)
        """
        # Candle index saat ini berdasarkan pembagian epoch.
        # Epoch 0 = 1970-01-01 00:00 UTC, kelipatan candle_sec = candle close.
        cur_idx = int(after_epoch // candle_sec)
        target = (cur_idx + 1) * candle_sec + boundary_buffer
        # Kalau kita masih sebelum boundary current_idx + buffer, fire di sana
        # (karena candle current_idx baru saja close).
        within_buffer = cur_idx * candle_sec + boundary_buffer
        if after_epoch < within_buffer:
            return within_buffer
        return target

    print("\n🚀 Bot Started.")
    print(f"🕖 Watchlist refresh otomatis setiap hari jam 07:00 {user_tz}")
    print(f"🧹 DB purge otomatis setiap hari jam 03:00 {user_tz} (cegah disk full)")
    print(f"💗 Heartbeat Telegram otomatis setiap 1 jam")
    print(f"📐 Scan candle-aligned: setiap candle {ENTRY_TF} close + {boundary_buffer:.0f}s buffer")
    print(f"💡 Tip: set \"debug\": true di config.json untuk verbose output\n")
    next_scan_at = _next_boundary_epoch(time.time())
    last_scan_idx = -1
    logger.info(
        f"📐 Next candle-aligned scan at "
        f"{datetime.fromtimestamp(next_scan_at, tz=timezone.utc).strftime('%H:%M:%S')} UTC"
    )
    while True:
        schedule.run_pending()

        if time.time() >= next_scan_at:
            # Catat candle index yang baru kita layani — boundary berikut
            # WAJIB > index ini supaya scan tidak diulang dalam buffer window.
            last_scan_idx = int(next_scan_at // candle_sec)
            try:
                scan()
            except Exception as e:
                logger.error(f"Scheduled scan error: {type(e).__name__}: {e}", exc_info=True)

            # Cari boundary berikut yang index-nya > last_scan_idx.
            candidate = _next_boundary_epoch(time.time())
            if int(candidate // candle_sec) <= last_scan_idx:
                candidate = (last_scan_idx + 1) * candle_sec + boundary_buffer
            next_scan_at = candidate
            tnext = datetime.fromtimestamp(next_scan_at, tz=timezone.utc).strftime("%H:%M:%S")
            logger.info(f"📐 Next candle-aligned scan at {tnext} UTC")

        time.sleep(1)
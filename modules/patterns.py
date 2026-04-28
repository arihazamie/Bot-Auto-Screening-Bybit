"""
patterns.py — Chart Pattern Detection
======================================
ANOMALY HARDENING (fix A):
  • Volume confirmation gate raised from 1.2× to configurable (default 1.8×)
    — eliminates marginal volume blips that produced false-breakout patterns
    in sideways markets.
  • Triangles, flags, rectangles now require ADX(14) >= MIN_PATTERN_ADX
    (default 20). In a tight range ADX < 18 → these patterns are statistical
    artefacts, not actionable breakouts.
  • Double-top / double-bottom now require:
      - meaningful spacing between the two peaks/lows (>= MIN_DOUBLE_GAP_BARS
        bars apart, default 5) so we do not pick up two adjacent candles.
      - the second touch must hold (the close after the second peak/low must
        bounce/reject back >= MIN_DOUBLE_REJECT_PCT of ATR, default 0.4).
  • Pattern direction registry (BULLISH_PATTERNS / BEARISH_PATTERNS) unchanged.

Config keys (under "patterns"):
  "tolerance":              0.015    double-top/bottom alignment tolerance
  "volume_multiplier":      1.8      recent vs prior volume ratio gate
  "min_pattern_adx":        20.0     ADX(14) gate for triangle/flag/rectangle
  "min_double_gap_bars":    5        minimum bars between the two peaks/lows
  "min_double_reject_atr":  0.4      ATR multiples the next bar must reject
"""

import logging
import numpy as np
from scipy.signal import argrelextrema
from scipy.stats import linregress
from modules.config_loader import CONFIG

logger = logging.getLogger("Patterns")

_PAT = CONFIG.get("patterns", {})
VOLUME_MULTIPLIER     = float(_PAT.get("volume_multiplier",     1.8))
MIN_PATTERN_ADX       = float(_PAT.get("min_pattern_adx",       20.0))
MIN_DOUBLE_GAP_BARS   = int(_PAT.get("min_double_gap_bars",     5))
MIN_DOUBLE_REJECT_ATR = float(_PAT.get("min_double_reject_atr", 0.4))

# ─── Pattern Direction Registry ───────────────────────────────────────────────
# Digunakan oleh MTF confluence check untuk menentukan apakah pattern pada
# higher timeframe searah atau berlawanan dengan signal entry timeframe.
BULLISH_PATTERNS: frozenset = frozenset({
    "double_bottom",
    "bull_flag",
    "ascending_triangle",
    "bullish_rectangle",
})

BEARISH_PATTERNS: frozenset = frozenset({
    "double_top",
    "bear_flag",
    "descending_triangle",
})


def pattern_direction(pattern: str) -> str | None:
    """
    Return arah sinyal dari sebuah pattern.
    Return 'Long', 'Short', atau None jika pattern tidak dikenal.
    """
    if pattern in BULLISH_PATTERNS:
        return "Long"
    if pattern in BEARISH_PATTERNS:
        return "Short"
    return None


def get_slope(values):
    try:
        return linregress(np.arange(len(values)), values)[0]
    except Exception as e:
        logger.debug(f"get_slope fallback: {e}")
        return 0.0


def check_alignment(values):
    if len(values) < 2:
        return False
    tol = _PAT.get("tolerance", 0.015)
    avg = np.mean(values)
    return all(abs(v - avg) / avg < tol for v in values)


def _has_volume_confirmation(df, lookback: int = 10) -> bool:
    """
    Recent N candles' average volume > VOLUME_MULTIPLIER × the prior N candles.
    Confirms that the breakout / pattern has real participation.
    """
    if len(df) < lookback * 2:
        return False
    recent = df["volume"].iloc[-lookback:].mean()
    prior  = df["volume"].iloc[-lookback * 2 : -lookback].mean()
    return prior > 0 and (recent / prior) > VOLUME_MULTIPLIER


def _adx_ok(df) -> bool:
    """
    Trend-strength gate (fix A). Returns True if ADX(14) >= MIN_PATTERN_ADX
    or if ADX column is missing (fail-open to keep backwards compat for callers
    that compute pattern before technicals).
    """
    if "adx" not in df.columns or MIN_PATTERN_ADX <= 0:
        return True
    try:
        last = float(df["adx"].iloc[-1])
        return last >= MIN_PATTERN_ADX
    except Exception:
        return True


def _has_valid_pole(df, pivot_idx: int, direction: str,
                    min_move: float = 0.03, pole_bars: int = 10) -> bool:
    """
    For flag patterns: verify there is a strong impulse move (the pole)
    in the last `pole_bars` candles before the pivot.

    direction: 'bull' (close moved up ≥ min_move) or 'bear' (moved down ≥ min_move).
    """
    start = max(0, pivot_idx - pole_bars)
    if start >= pivot_idx:
        return False
    pole = df.iloc[start:pivot_idx]
    if len(pole) < 3:
        return False
    pct_change = (pole["close"].iloc[-1] - pole["close"].iloc[0]) / (pole["close"].iloc[0] + 1e-10)
    if direction == "bull":
        return pct_change >= min_move
    return pct_change <= -min_move


def _atr_proxy(df, length: int = 14) -> float:
    """
    Cheap ATR proxy without depending on pandas_ta (used inside pattern guards
    where we may not have the indicator pre-computed).
    """
    if len(df) < length + 1:
        return 0.0
    high  = df["high"].iloc[-length:].values
    low   = df["low"].iloc[-length:].values
    close = df["close"].iloc[-length - 1: -1].values
    tr = np.maximum(high - low, np.maximum(np.abs(high - close), np.abs(low - close)))
    return float(np.mean(tr)) if len(tr) else 0.0


def _double_pattern_valid(df, idxs, kind: str) -> bool:
    """
    Validate a double-top / double-bottom (fix A):
      1. Two peaks/valleys must be at least MIN_DOUBLE_GAP_BARS bars apart.
      2. After the second touch, price must reject back by at least
         MIN_DOUBLE_REJECT_ATR × ATR — proves the level held.
    """
    if len(idxs) < 2:
        return False
    p1, p2 = int(idxs[-2]), int(idxs[-1])
    if (p2 - p1) < MIN_DOUBLE_GAP_BARS:
        return False
    if p2 >= len(df):
        return False
    atr = _atr_proxy(df)
    if atr <= 0:
        return True  # cannot evaluate reject — fall back to alignment-only

    last_close = float(df["close"].iloc[-1])
    second_lvl = (
        float(df["high"].iloc[p2]) if kind == "top"
        else float(df["low"].iloc[p2])
    )
    if kind == "top":
        # close must drop by reject threshold below the second peak
        return (second_lvl - last_close) >= (MIN_DOUBLE_REJECT_ATR * atr)
    # double_bottom: close must rise above the second valley
    return (last_close - second_lvl) >= (MIN_DOUBLE_REJECT_ATR * atr)


def find_pattern(df):
    if len(df) < 50:
        return None

    df_idx = df.reset_index(drop=True)
    n      = 3
    low_pos  = argrelextrema(df_idx["low"].values,  np.less_equal,    order=n)[0]
    high_pos = argrelextrema(df_idx["high"].values, np.greater_equal, order=n)[0]

    df_idx["min_local"] = np.nan
    df_idx["max_local"] = np.nan
    if len(low_pos):  df_idx.loc[low_pos,  "min_local"] = df_idx["low"].iloc[low_pos].values
    if len(high_pos): df_idx.loc[high_pos, "max_local"] = df_idx["high"].iloc[high_pos].values

    peaks   = df_idx[df_idx["max_local"].notnull()]["max_local"].values
    valleys = df_idx[df_idx["min_local"].notnull()]["min_local"].values
    if len(peaks) < 3 or len(valleys) < 3:
        return None

    enabled = _PAT
    s_high  = get_slope(peaks[-4:])
    s_low   = get_slope(valleys[-4:])
    vol_ok  = _has_volume_confirmation(df_idx)
    adx_ok  = _adx_ok(df_idx)

    # ── Triangles (require volume surge AND trend-strength) ───────────────────
    if enabled.get("ascending_triangle") and abs(s_high) < 0.0005 and s_low > 0.0002:
        if vol_ok and adx_ok:
            return "ascending_triangle"

    if enabled.get("descending_triangle") and abs(s_low) < 0.0005 and s_high < -0.0002:
        if vol_ok and adx_ok:
            return "descending_triangle"

    # ── Double patterns: structural validity (gap + post-reject) ──────────────
    if enabled.get("double_bottom") and check_alignment(valleys[-2:]):
        if _double_pattern_valid(df_idx, df_idx[df_idx["min_local"].notnull()].index[-2:],
                                 kind="bottom"):
            return "double_bottom"

    if enabled.get("double_top") and check_alignment(peaks[-2:]):
        if _double_pattern_valid(df_idx, df_idx[df_idx["max_local"].notnull()].index[-2:],
                                 kind="top"):
            return "double_top"

    # ── Bull flag: descending consolidation after a bullish pole ──────────────
    if enabled.get("bull_flag") and -0.002 < s_high < -0.0002 and -0.002 < s_low < -0.0002:
        peak_idx = df_idx[df_idx["max_local"].notnull()].index[-1]
        if adx_ok and _has_valid_pole(df_idx, peak_idx, "bull"):
            return "bull_flag"

    # ── Bear flag: ascending consolidation after a bearish pole ───────────────
    if enabled.get("bear_flag") and 0.0002 < s_high < 0.002 and 0.0002 < s_low < 0.002:
        valley_idx = df_idx[df_idx["min_local"].notnull()].index[-1]
        if adx_ok and _has_valid_pole(df_idx, valley_idx, "bear"):
            return "bear_flag"

    # ── Rectangle (require volume + trend-strength gate) ──────────────────────
    if enabled.get("bullish_rectangle") and abs(s_high) < 0.0005 and abs(s_low) < 0.0005:
        if vol_ok and adx_ok:
            return "bullish_rectangle"

    return None

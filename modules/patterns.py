"""
patterns.py — Chart Pattern Detection
======================================
Improvements over v1:
  - Volume confirmation required for triangle / rectangle breakouts
  - Bull/bear flag must have a valid POLE (impulse move before consolidation)
  - Bear flag added to bullish_rectangle check separation
  - Slope thresholds unchanged (tuned empirically); volume gate is the new guard
"""

import logging
import numpy as np
from scipy.signal import argrelextrema
from scipy.stats import linregress
from modules.config_loader import CONFIG

logger = logging.getLogger("Patterns")


def get_slope(values):
    try:
        return linregress(np.arange(len(values)), values)[0]
    except Exception as e:
        logger.debug(f"get_slope fallback: {e}")
        return 0.0


def check_alignment(values):
    if len(values) < 2:
        return False
    tol = CONFIG["patterns"].get("tolerance", 0.015)
    avg = np.mean(values)
    return all(abs(v - avg) / avg < tol for v in values)


def _has_volume_confirmation(df, lookback: int = 10) -> bool:
    """
    Recent N candles' average volume > 1.2× the prior N candles.
    Confirms that the breakout / pattern has real participation.
    """
    if len(df) < lookback * 2:
        return False
    recent = df["volume"].iloc[-lookback:].mean()
    prior  = df["volume"].iloc[-lookback * 2 : -lookback].mean()
    return prior > 0 and (recent / prior) > 1.2


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

    enabled = CONFIG["patterns"]
    s_high  = get_slope(peaks[-4:])
    s_low   = get_slope(valleys[-4:])
    vol_ok  = _has_volume_confirmation(df_idx)

    # ── Triangles (require volume surge on breakout) ──────────────────────────
    if enabled.get("ascending_triangle") and abs(s_high) < 0.0005 and s_low > 0.0002:
        if vol_ok:
            return "ascending_triangle"

    if enabled.get("descending_triangle") and abs(s_low) < 0.0005 and s_high < -0.0002:
        if vol_ok:
            return "descending_triangle"

    # ── Double patterns (no volume gate — reversal patterns, not breakouts) ───
    if enabled.get("double_bottom") and check_alignment(valleys[-2:]):
        return "double_bottom"

    if enabled.get("double_top") and check_alignment(peaks[-2:]):
        return "double_top"

    # ── Bull flag: descending consolidation after a bullish pole ──────────────
    if enabled.get("bull_flag") and -0.002 < s_high < -0.0002 and -0.002 < s_low < -0.0002:
        peak_idx = df_idx[df_idx["max_local"].notnull()].index[-1]
        if _has_valid_pole(df_idx, peak_idx, "bull"):
            return "bull_flag"

    # ── Bear flag: ascending consolidation after a bearish pole ───────────────
    if enabled.get("bear_flag") and 0.0002 < s_high < 0.002 and 0.0002 < s_low < 0.002:
        valley_idx = df_idx[df_idx["min_local"].notnull()].index[-1]
        if _has_valid_pole(df_idx, valley_idx, "bear"):
            return "bear_flag"

    # ── Rectangle (require volume confirmation) ───────────────────────────────
    if enabled.get("bullish_rectangle") and abs(s_high) < 0.0005 and abs(s_low) < 0.0005:
        if vol_ok:
            return "bullish_rectangle"

    return None
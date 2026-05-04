"""Pattern-aware invalidation level resolver.

Each pattern detector has a *structural invalidation point* — the price beyond
which the technical setup is no longer valid. Stops should be placed just
outside this level (with a small ATR buffer) instead of using a generic
``ATR × N`` distance that ignores what the chart actually says.

Examples
--------
* **Bullish engulfing** is invalidated when price closes below the engulfing
  candle's low. Stop = engulfing low − buffer.
* **Bullish ABC (Elliott)** is invalidated when price breaks below L₃
  (the C-low). Stop = L₃ − buffer.
* **Bullish order block** is invalidated when the block's low is mitigated.
  Stop = OB-low − buffer.
* **Inverse H&S** is invalidated when price breaks below the head.

Heuristic
---------
Most pattern detectors *don't* return their structural levels in the result
shape — only a confidence score. Re-deriving the exact level for every
pattern would mean modifying every detector. Instead we use a per-pattern
lookback heuristic:

    invalidation_long  = lowest  low  in last N bars
    invalidation_short = highest high in last N bars

where N is calibrated per pattern (1 bar for single-candle setups,
30 bars for harmonics, etc.). This is the same convention every chartist
uses by eye when "looking just below the recent low for stop placement".

Caller responsibilities
-----------------------
Apply the buffer (ATR-multiple) and direction sign at the call site:

    inv = get_invalidation_level(pattern, side, df)
    if inv is not None:
        buf = atr * SL_INVALIDATION_BUFFER_ATR
        sl  = inv - buf if side == "Long" else inv + buf
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ─── Per-pattern lookback table ──────────────────────────────────────────────
# Bars from current bar back to scan for the structural low/high. Calibrated
# to roughly match the visual "recent extreme" a chartist would point at.
# All keys lower-case; pattern names from BASELINE_WINRATES in
# modules.pattern_registry.
PATTERN_INVALIDATION_LOOKBACK: dict[str, int] = {
    # ─── single-candle ────────────────────────────────────────────────────
    "hammer":               1,
    "inverted_hammer":      1,
    "shooting_star":        1,
    "hanging_man":          1,
    "doji":                 2,

    # ─── 2-bar ────────────────────────────────────────────────────────────
    "bullish_engulfing":    2,
    "bearish_engulfing":    2,
    "tweezer_top":          2,
    "tweezer_bottom":       2,

    # ─── 3-bar ────────────────────────────────────────────────────────────
    "morning_star":         3,
    "evening_star":         3,
    "three_white_soldiers": 3,
    "three_black_crows":    3,

    # ─── chart patterns (span varies) ─────────────────────────────────────
    "double_bottom":              20,
    "double_top":                 20,
    "head_and_shoulders":         30,
    "inverse_head_and_shoulders": 30,
    "bull_flag":                  15,
    "bear_flag":                  15,
    "ascending_triangle":         25,
    "descending_triangle":        25,
    "bullish_rectangle":          20,
    "bearish_rectangle":          20,

    # ─── harmonic XABCD (≈ 30 bars) ───────────────────────────────────────
    "gartley":   30,
    "bat":       30,
    "butterfly": 30,
    "crab":      30,
    "shark":     30,

    # ─── SMC / order block / breaker ──────────────────────────────────────
    "smc_long":                 20,
    "smc_short":                20,
    "breaker_block_bullish":    20,
    "breaker_block_bearish":    20,
    "mitigation_block_bullish": 20,
    "mitigation_block_bearish": 20,

    # ─── Wyckoff ──────────────────────────────────────────────────────────
    "wyckoff_spring":   15,
    "wyckoff_upthrust": 15,

    # ─── volume profile ───────────────────────────────────────────────────
    "vp_poc_reaction":  15,
    "vp_vah_rejection": 15,
    "vp_val_reaction":  15,

    # ─── divergence ───────────────────────────────────────────────────────
    "divergence_rsi":      20,
    "divergence_macd":     20,
    "divergence_rsi_mtf":  25,
    "divergence_macd_mtf": 25,

    # ─── Elliott Wave ABC ─────────────────────────────────────────────────
    "elliott_abc_long":  25,
    "elliott_abc_short": 25,
}

# Default for unknown / unmapped patterns.
DEFAULT_LOOKBACK_BARS = 10

# Minimum bars required before we'll attempt to compute an invalidation level.
MIN_BARS_REQUIRED = 5


def get_invalidation_level(
    pattern_name: str | None,
    side: str,
    df: pd.DataFrame | None,
) -> Optional[float]:
    """Return the structural invalidation price for the given pattern.

    For ``side == "Long"``  this is a *low*  below current price.
    For ``side == "Short"`` this is a *high* above current price.

    Returns ``None`` if the dataframe is too short or required columns
    missing — caller should fall back to ATR-based SL.
    """
    if df is None or len(df) < MIN_BARS_REQUIRED:
        return None

    if "low" not in df.columns or "high" not in df.columns:
        return None

    side_norm = (side or "").strip().lower()
    if side_norm not in {"long", "short"}:
        return None

    name = (pattern_name or "").strip().lower()
    lookback = PATTERN_INVALIDATION_LOOKBACK.get(name, DEFAULT_LOOKBACK_BARS)

    # Clamp to available data
    lookback = max(1, min(lookback, len(df)))

    try:
        if side_norm == "long":
            level = float(df["low"].tail(lookback).min())
        else:
            level = float(df["high"].tail(lookback).max())
    except (ValueError, TypeError) as e:
        logger.debug(f"get_invalidation_level error ({type(e).__name__}: {e})")
        return None

    # Sanity: positive, finite
    import math
    if not math.isfinite(level) or level <= 0:
        return None

    return level


def lookback_for(pattern_name: str | None) -> int:
    """Public accessor for the lookback bars used by a given pattern."""
    name = (pattern_name or "").strip().lower()
    return PATTERN_INVALIDATION_LOOKBACK.get(name, DEFAULT_LOOKBACK_BARS)

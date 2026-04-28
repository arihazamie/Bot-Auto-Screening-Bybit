"""
range_strategy.py — Mean-Revert / Range-Bound Strategy
=======================================================
ANOMALY HARDENING (fix K).

Activated only when the regime classifier reports RANGE or SQUEEZE.
The default trend-continuation pipeline (patterns + SMC BOS) struggles
in directionless markets; this module adds three orthogonal signal
sources that are *appropriate* for those regimes:

  1. Bollinger reversion to the mean
       Long  : close <= BB lower band  AND  RSI(14) <= 30
       Short : close >= BB upper band  AND  RSI(14) >= 70
       Confirms with a wick-rejection candle.

  2. Failed-breakout fade
       Recent extreme high then close back inside the prior range
       (false breakout) → fade in opposite direction.

  3. Bollinger squeeze breakout
       Only when bbw_pct (from regime) is in the squeeze regime AND
       a single bar closes outside the band with at least 1.5x volume:
       follow the breakout direction.

`find_range_signal(df, regime)` returns either:
  None   → no range-mode signal here
  dict   → { "side": "Long"|"Short", "pattern": str, "reason": str }

The dict is intentionally compatible with the existing analyze_ticker
shape so main.scan can route this signal through the same SL/TP and
risk pipeline (no parallel order path required).

Config keys (under "strategy.range"):
  "enabled":             true     master switch
  "bb_length":           20
  "bb_std":              2.0
  "rsi_oversold":        30
  "rsi_overbought":      70
  "squeeze_breakout_volume_mult": 1.5
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
import pandas_ta as ta

from modules.config_loader import CONFIG

logger = logging.getLogger("RangeStrategy")

_RNG_CFG = CONFIG.get("strategy", {}).get("range", {})

ENABLED                  = bool(_RNG_CFG.get("enabled",                       True))
BB_LENGTH                = int(_RNG_CFG.get("bb_length",                     20))
BB_STD                   = float(_RNG_CFG.get("bb_std",                      2.0))
RSI_OVERSOLD             = float(_RNG_CFG.get("rsi_oversold",                30.0))
RSI_OVERBOUGHT           = float(_RNG_CFG.get("rsi_overbought",              70.0))
SQUEEZE_VOL_MULT         = float(_RNG_CFG.get("squeeze_breakout_volume_mult", 1.5))


def range_strategy_enabled() -> bool:
    return ENABLED


def _bb_bands(close: pd.Series) -> dict | None:
    """
    Resolve Bollinger band columns by prefix. Returns None if any of the
    three required bands cannot be matched — old code fell back to
    positional indices, but the order changes between pandas_ta versions
    (BBL/BBM/BBU vs BBL/BBU/BBM) so a missing-prefix scenario silently
    swapped bands.
    """
    bb = ta.bbands(close, length=BB_LENGTH, std=BB_STD)
    if bb is None or bb.empty:
        return None
    upper  = next((c for c in bb.columns if c.startswith("BBU_")), None)
    lower  = next((c for c in bb.columns if c.startswith("BBL_")), None)
    middle = next((c for c in bb.columns if c.startswith("BBM_")), None)
    if not (upper and lower and middle):
        logger.warning(
            f"_bb_bands: cannot identify BBU/BBL/BBM in {list(bb.columns)} — "
            "skipping range signal"
        )
        return None
    return {"upper": bb[upper], "lower": bb[lower], "middle": bb[middle]}


def _rsi(close: pd.Series, length: int = 14) -> pd.Series:
    rsi = ta.rsi(close, length=length)
    return rsi if rsi is not None else pd.Series(dtype=float)


def _is_wick_reject_long(row: pd.Series) -> bool:
    """Bullish reject: lower wick at least 1.5x body, close in upper half."""
    body = abs(row["close"] - row["open"])
    rng  = row["high"] - row["low"]
    if rng <= 0:
        return False
    lower_wick = min(row["open"], row["close"]) - row["low"]
    return lower_wick >= 1.5 * max(body, 1e-9) and (row["close"] - row["low"]) >= 0.5 * rng


def _is_wick_reject_short(row: pd.Series) -> bool:
    body = abs(row["close"] - row["open"])
    rng  = row["high"] - row["low"]
    if rng <= 0:
        return False
    upper_wick = row["high"] - max(row["open"], row["close"])
    return upper_wick >= 1.5 * max(body, 1e-9) and (row["high"] - row["close"]) >= 0.5 * rng


def find_range_signal(df: pd.DataFrame, regime: dict | None = None) -> dict | None:
    """Return a range-mode signal or None."""
    if not ENABLED:
        return None
    if df is None or len(df) < BB_LENGTH + 10:
        return None
    if regime is not None and regime.get("label") not in ("RANGE", "SQUEEZE"):
        # Only run in regimes designed for this strategy.
        return None

    try:
        bands = _bb_bands(df["close"])
        if bands is None:
            return None
        rsi = _rsi(df["close"])
        if rsi.empty:
            return None

        last = df.iloc[-1]
        last_close = float(last["close"])
        u = float(bands["upper"].iloc[-1])
        l = float(bands["lower"].iloc[-1])
        m = float(bands["middle"].iloc[-1])
        last_rsi = float(rsi.iloc[-1])

        # 1) BB reversion long --------------------------------------------
        if last_close <= l and last_rsi <= RSI_OVERSOLD and _is_wick_reject_long(last):
            return {
                "side":    "Long",
                "pattern": "bb_revert_long",
                "reason":  f"close<=BB_low ({last_close:.6g}<={l:.6g}), RSI={last_rsi:.0f}",
            }

        # 2) BB reversion short -------------------------------------------
        if last_close >= u and last_rsi >= RSI_OVERBOUGHT and _is_wick_reject_short(last):
            return {
                "side":    "Short",
                "pattern": "bb_revert_short",
                "reason":  f"close>=BB_up ({last_close:.6g}>={u:.6g}), RSI={last_rsi:.0f}",
            }

        # 3) Failed-breakout fade -----------------------------------------
        # Prev candle made an extreme high above the band, then current closed
        # back below the middle band (failed breakout to the upside).
        if len(df) >= 3:
            prev = df.iloc[-2]
            if (
                prev["high"] >= float(bands["upper"].iloc[-2])
                and last_close < m
                and last_rsi < 60
            ):
                return {
                    "side":    "Short",
                    "pattern": "failed_breakout_short",
                    "reason":  f"prev high={float(prev['high']):.6g} above BB_up, now back below mid",
                }
            if (
                prev["low"] <= float(bands["lower"].iloc[-2])
                and last_close > m
                and last_rsi > 40
            ):
                return {
                    "side":    "Long",
                    "pattern": "failed_breakout_long",
                    "reason":  f"prev low={float(prev['low']):.6g} below BB_low, now back above mid",
                }

        # 4) Squeeze breakout ---------------------------------------------
        if regime is not None and regime.get("label") == "SQUEEZE":
            vol_recent = float(df["volume"].iloc[-1])
            vol_avg    = float(df["volume"].iloc[-20:].mean())
            if vol_avg > 0 and vol_recent >= SQUEEZE_VOL_MULT * vol_avg:
                if last_close > u:
                    return {
                        "side":    "Long",
                        "pattern": "squeeze_breakout_long",
                        "reason":  f"squeeze break up, vol {vol_recent / vol_avg:.1f}x",
                    }
                if last_close < l:
                    return {
                        "side":    "Short",
                        "pattern": "squeeze_breakout_short",
                        "reason":  f"squeeze break down, vol {vol_recent / vol_avg:.1f}x",
                    }

    except Exception as e:
        logger.debug(f"find_range_signal fallback: {type(e).__name__}: {e}")

    return None

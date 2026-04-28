"""
indicators.py — shared indicator helpers.

Centralised so that ATR / ATR% used as gate thresholds across modules
(patterns, smc, regime) is computed identically to the ATR used for
SL/TP sizing in main.resolve_atr (Wilder smoothing via pandas_ta).

Before this consolidation each module had its own `_atr_proxy` /
`_atr_pct` that returned a *simple mean* of TR over the last N bars,
which differed from Wilder ATR by 5–15% — enough to cause regime
ANOMALY thresholds and sweep tolerance gates to fire (or not) at
different conditions than SL distance computation.
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
import pandas_ta as ta

logger = logging.getLogger("Indicators")


def wilder_atr(df: pd.DataFrame, length: int = 14) -> float:
    """
    Last Wilder-smoothed ATR value as a float.
    Returns 0.0 on insufficient data or any pandas_ta failure.
    """
    if df is None or len(df) < length + 1:
        return 0.0
    try:
        atr = ta.atr(df["high"], df["low"], df["close"], length=length)
        if atr is None or len(atr) == 0:
            return 0.0
        value = float(atr.iloc[-1])
        return value if np.isfinite(value) and value > 0 else 0.0
    except Exception as e:
        logger.debug(f"wilder_atr fallback ({type(e).__name__}: {e}) → 0.0")
        return 0.0


def wilder_atr_pct(df: pd.DataFrame, length: int = 14) -> float:
    """
    Wilder-smoothed ATR as a fraction of the last close.
    Used as a regime / volatility gate (e.g. anomaly_atr_pct = 0.025).
    """
    if df is None or df.empty:
        return 0.0
    atr = wilder_atr(df, length=length)
    last_close = float(df["close"].iloc[-1]) if len(df) else 0.0
    if last_close <= 0 or atr <= 0:
        return 0.0
    return atr / last_close

"""
divergence.py — RSI + MACD divergence detection (single-TF and multi-TF)
========================================================================

Detects **regular** divergence between price and momentum oscillator on each
provided OHLC DataFrame.

* **Bullish regular**: price prints a lower low, oscillator prints a higher low
  (momentum hasn't confirmed the new price low → reversal candidate).
* **Bearish regular**: price prints a higher high, oscillator prints a lower
  high (price extended without momentum → reversal candidate).

Two oscillators are evaluated:
  * `RSI(14)` via pandas_ta
  * `MACD histogram (12, 26, 9)` via pandas_ta

Single-TF detector: `detect_single_tf(df) -> list[dict]` returns one hit per
oscillator that fires.

Multi-TF aggregator: `detect_multi_tf({"15m": df_15m, "1h": df_1h, "4h": df_4h})`
returns confluence hits — a hit is fired only when ≥`MIN_TF_CONFLUENCE` TFs
show the same-side divergence on the same oscillator.

Returned dict shape:
    {
      "name": "divergence_rsi" / "divergence_macd" /
              "divergence_rsi_mtf" / "divergence_macd_mtf",
      "side": "Long" / "Short",
      "details": str
    }
"""
from __future__ import annotations

import logging
from typing import Mapping

import numpy as np
import pandas as pd
import pandas_ta as ta
from scipy.signal import argrelextrema

logger = logging.getLogger("Divergence")

PIVOT_ORDER         = 3
MIN_PIVOTS          = 2
MIN_TF_CONFLUENCE   = 2     # ≥2 TFs must agree for a multi-TF hit


def _pivot_indices(values: np.ndarray, kind: str, order: int = PIVOT_ORDER) -> np.ndarray:
    """Return indices of local extrema. `kind` ∈ {"high", "low"}."""
    if values.size < order * 2 + 2:
        return np.array([], dtype=int)
    cmp = np.greater if kind == "high" else np.less
    idx = argrelextrema(values, cmp, order=order)[0]
    return idx


def _check_regular_divergence(price: np.ndarray, osc: np.ndarray) -> str | None:
    """Return "Long" (bullish), "Short" (bearish), or None.

    Bullish: price[low2] < price[low1]  AND  osc[low2] > osc[low1]
    Bearish: price[hi2]  > price[hi1]   AND  osc[hi2]  < osc[hi1]

    Pivots used: most recent two pivots of each kind. We require the indices
    to be at least `PIVOT_ORDER + 1` bars apart so the oscillator has had time
    to actually diverge (avoids back-to-back micro-pivots).
    """
    lows = _pivot_indices(price, "low")
    highs = _pivot_indices(price, "high")

    # Bearish via highs
    if highs.size >= MIN_PIVOTS:
        h1, h2 = highs[-2], highs[-1]
        if h2 - h1 >= PIVOT_ORDER + 1:
            if price[h2] > price[h1] and osc[h2] < osc[h1]:
                return "Short"

    # Bullish via lows
    if lows.size >= MIN_PIVOTS:
        l1, l2 = lows[-2], lows[-1]
        if l2 - l1 >= PIVOT_ORDER + 1:
            if price[l2] < price[l1] and osc[l2] > osc[l1]:
                return "Long"
    return None


def _compute_rsi(df: pd.DataFrame, length: int = 14) -> np.ndarray | None:
    if len(df) < length + PIVOT_ORDER * 2 + 4:
        return None
    try:
        rsi = ta.rsi(df["close"], length=length)
        if rsi is None or rsi.empty:
            return None
        return rsi.bfill().to_numpy(dtype=float)
    except Exception as e:
        logger.debug(f"RSI fail: {e}")
        return None


def _compute_macd_hist(df: pd.DataFrame) -> np.ndarray | None:
    """Return MACD histogram series (last column of pandas_ta.macd output)."""
    if len(df) < 26 + 9 + PIVOT_ORDER * 2 + 4:
        return None
    try:
        macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
        if macd is None or macd.empty:
            return None
        # Identify histogram column robustly (varies across pandas_ta versions).
        hist_cols = [c for c in macd.columns if "MACDh" in c]
        col = hist_cols[0] if hist_cols else macd.columns[1]
        return macd[col].bfill().to_numpy(dtype=float)
    except Exception as e:
        logger.debug(f"MACD fail: {e}")
        return None


def detect_single_tf(df: pd.DataFrame) -> list[dict]:
    """Run RSI and MACD divergence on the most recent pivots in `df`."""
    if df is None or len(df) < 30:
        return []
    if not {"close"}.issubset(df.columns):
        return []
    price = df["close"].to_numpy(dtype=float)
    hits: list[dict] = []

    rsi = _compute_rsi(df)
    if rsi is not None and rsi.size == price.size:
        side = _check_regular_divergence(price, rsi)
        if side:
            hits.append({
                "name": "divergence_rsi",
                "side": side,
                "details": f"RSI {side.lower()} divergence on last 2 price pivots",
            })

    macd_h = _compute_macd_hist(df)
    if macd_h is not None and macd_h.size == price.size:
        side = _check_regular_divergence(price, macd_h)
        if side:
            hits.append({
                "name": "divergence_macd",
                "side": side,
                "details": f"MACD histogram {side.lower()} divergence on last 2 price pivots",
            })

    return hits


def detect_multi_tf(dfs: Mapping[str, pd.DataFrame]) -> list[dict]:
    """Aggregate single-TF divergence across multiple timeframes.

    `dfs` is a mapping like {"15m": df_15m, "1h": df_1h, "4h": df_4h}. A hit
    is reported when ≥`MIN_TF_CONFLUENCE` TFs show the same oscillator/side
    divergence on their most recent pivots.
    """
    if not dfs:
        return []
    # bucket: (osc_name, side) -> list[tf_label]
    bucket: dict[tuple[str, str], list[str]] = {}
    for tf_label, df in dfs.items():
        for hit in detect_single_tf(df):
            key = (hit["name"], hit["side"])
            bucket.setdefault(key, []).append(tf_label)

    confluent: list[dict] = []
    for (osc, side), tfs in bucket.items():
        if len(tfs) >= MIN_TF_CONFLUENCE:
            confluent.append({
                "name": f"{osc}_mtf",
                "side": side,
                "details": f"{osc.replace('divergence_', '').upper()} {side.lower()} divergence on {','.join(tfs)} ({len(tfs)} TFs)",
            })
    return confluent


def detect_all(df: pd.DataFrame, multi_tf_dfs: Mapping[str, pd.DataFrame] | None = None) -> list[dict]:
    """Convenience: single-TF divergence on `df`, plus multi-TF if provided."""
    hits = detect_single_tf(df)
    if multi_tf_dfs:
        hits.extend(detect_multi_tf(multi_tf_dfs))
    return hits

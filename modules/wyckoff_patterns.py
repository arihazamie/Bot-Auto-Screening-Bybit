"""
wyckoff_patterns.py — Wyckoff-style reversal patterns
=====================================================

Detects two classic Wyckoff reversal events on the latest closed candle:

* **Spring** (bullish): a false breakdown below the recent range low. Price
  briefly punctures support, sweeps liquidity, then closes back inside the
  range. Signature is high volume on the puncture and a long lower wick.

* **Upthrust** (bearish): the inverse — false breakout above range high.

Both patterns are stronger when accompanied by a relative volume spike
(`current_volume > rvol_threshold * mean(volume_20)`).

The detectors use range-percentage rules (lower wick percentage of total
range, body percentage, etc.) so they scale across microcaps and BTC alike.

Returned dict shape:
    {"name": str, "side": "Long"|"Short", "details": str}
"""
from __future__ import annotations

import logging
from typing import Callable

import numpy as np
import pandas as pd

logger = logging.getLogger("WyckoffPatterns")

RANGE_LOOKBACK = 30   # how many candles defines the "trading range"
RVOL_LENGTH    = 20   # volume MA window
RVOL_MIN       = 1.3  # current volume must be ≥ 1.3 × rolling mean to count


def _rvol(df: pd.DataFrame, length: int = RVOL_LENGTH) -> float:
    """Return current bar's relative volume vs trailing mean. 1.0 = average."""
    if "volume" not in df.columns or len(df) < length + 1:
        return 1.0
    vols = df["volume"].astype(float)
    mean = vols.tail(length + 1).head(length).mean()
    if not np.isfinite(mean) or mean <= 0:
        return 1.0
    cur = float(vols.iloc[-1])
    return cur / mean


def _range_bounds(df: pd.DataFrame, lookback: int = RANGE_LOOKBACK) -> tuple[float, float] | None:
    """Return (range_high, range_low) over the trailing lookback (excluding
    the latest bar so the bounds are defined by *prior* price action)."""
    if len(df) < lookback + 1:
        return None
    window = df.iloc[-(lookback + 1):-1]
    return float(window["high"].max()), float(window["low"].min())


def detect_spring(df: pd.DataFrame) -> dict | None:
    """Bullish spring: latest bar's low pierces below `range_low`, but its
    close is back ABOVE `range_low`. Lower wick must dominate the bar
    (≥ 50 % of range) and rvol ≥ RVOL_MIN."""
    bounds = _range_bounds(df)
    if bounds is None:
        return None
    _, range_low = bounds
    last = df.iloc[-1]
    o = float(last["open"]); h = float(last["high"])
    l = float(last["low"]);  c = float(last["close"])
    rng = max(h - l, 1e-12)
    if l >= range_low:
        return None  # no puncture
    if c <= range_low:
        return None  # failed to close back inside range
    lower_wick = min(o, c) - l
    if lower_wick / rng < 0.5:
        return None
    rvol = _rvol(df)
    if rvol < RVOL_MIN:
        return None
    return {
        "name": "wyckoff_spring",
        "side": "Long",
        "details": (
            f"Spring: low {l:.4f} pierced range_low {range_low:.4f} then closed back at {c:.4f} "
            f"(wick {lower_wick / rng:.0%} of range, rvol={rvol:.2f}×)"
        ),
    }


def detect_upthrust(df: pd.DataFrame) -> dict | None:
    """Bearish upthrust: latest bar's high pierces above `range_high` but
    closes back BELOW `range_high`. Upper wick ≥ 50 % of range, rvol ≥ RVOL_MIN."""
    bounds = _range_bounds(df)
    if bounds is None:
        return None
    range_high, _ = bounds
    last = df.iloc[-1]
    o = float(last["open"]); h = float(last["high"])
    l = float(last["low"]);  c = float(last["close"])
    rng = max(h - l, 1e-12)
    if h <= range_high:
        return None
    if c >= range_high:
        return None
    upper_wick = h - max(o, c)
    if upper_wick / rng < 0.5:
        return None
    rvol = _rvol(df)
    if rvol < RVOL_MIN:
        return None
    return {
        "name": "wyckoff_upthrust",
        "side": "Short",
        "details": (
            f"Upthrust: high {h:.4f} pierced range_high {range_high:.4f} then closed back at {c:.4f} "
            f"(wick {upper_wick / rng:.0%} of range, rvol={rvol:.2f}×)"
        ),
    }


# ─── Registry & aggregator ───────────────────────────────────────────────────

DETECTORS: dict[str, Callable[[pd.DataFrame], dict | None]] = {
    "wyckoff_spring":   detect_spring,
    "wyckoff_upthrust": detect_upthrust,
}


def detect_all(df: pd.DataFrame) -> list[dict]:
    if df is None or len(df) < RANGE_LOOKBACK + 1:
        return []
    if not {"open", "high", "low", "close"}.issubset(df.columns):
        return []
    hits: list[dict] = []
    for name, fn in DETECTORS.items():
        try:
            hit = fn(df)
        except Exception as e:
            logger.debug(f"[wyckoff:{name}] detector error: {e}")
            continue
        if hit:
            hits.append(hit)
    return hits

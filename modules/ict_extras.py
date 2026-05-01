"""
ict_extras.py — ICT/SMC patterns not already covered by `smc.py`
================================================================================

`smc.py` already detects: order blocks, fair value gaps (FVG), liquidity sweeps,
premium/discount zones, market structure (BoS/CHoCH), and inducement.

This module adds **Breaker Block** and **Mitigation Block** — the two remaining
ICT zone-style concepts most commonly traded.

Definitions
-----------
* **Breaker Block (BB)**: A *failed* order block. After price violates a swing
  high/low, the last opposing-side candle before the break becomes a "breaker".
  When price retraces back to the breaker zone, it tends to act as the inverse
  of its original side — a former bullish OB now serves as resistance (Short
  on retest), and vice versa.

* **Mitigation Block (MB)**: An order block that price returns to ("mitigates")
  for the *first* time after creation, before any structure break. Acts like a
  classic OB retest but is specifically the *first* tap.

Each detector inspects the most recent ~50 candles to find a zone, then checks
whether the most recent close is inside or testing it. Returns a list-friendly
dict on hit, or `None`.

Returned dict shape:
    {"name": str, "side": "Long"|"Short", "details": str}
"""
from __future__ import annotations

import logging
from typing import Callable

import numpy as np
import pandas as pd

from modules.indicators import wilder_atr

logger = logging.getLogger("ICTExtras")

LOOKBACK             = 50      # how far back to scan for swings / OB candidates
SWING_ORDER          = 3       # local-extrema window for swing detection
TEST_TOLERANCE_A     = 0.5     # how close to the zone (in ATR) counts as a "test"
DISPLACEMENT_ATR_MIN = 1.5     # minimum body of the displacement candle, in ATR units


# ─── Common helpers ──────────────────────────────────────────────────────────

def _atr_value(df: pd.DataFrame, length: int = 14) -> float:
    """Thin wrapper over `wilder_atr` (already returns a finite float or 0)."""
    return wilder_atr(df, length=length)


def _find_swept_swing_low(df: pd.DataFrame, lookback: int = LOOKBACK, order: int = SWING_ORDER) -> tuple[int, float] | None:
    """Return (idx, price) of the most recent swing low whose level was
    subsequently broken (sweep) AND has since been reclaimed by the latest
    close. This is the swing whose adjacent supply candle becomes the
    bullish breaker zone.
    """
    n = min(lookback, len(df))
    if n < order * 2 + 4:
        return None
    lows = df["low"].to_numpy(dtype=float)
    last_close = float(df["close"].iloc[-1])
    end = len(df) - order
    start = max(order, len(df) - n)
    for i in range(end - 1, start - 1, -1):
        window = lows[max(0, i - order):i + order + 1]
        if lows[i] > window.min():
            continue
        sw_low = float(lows[i])
        tail = lows[i + 1:]
        if tail.size == 0 or tail.min() >= sw_low:
            continue                  # never broken
        if last_close <= sw_low:
            continue                  # not yet reclaimed
        return i, sw_low
    return None


def _find_swept_swing_high(df: pd.DataFrame, lookback: int = LOOKBACK, order: int = SWING_ORDER) -> tuple[int, float] | None:
    """Mirror of `_find_swept_swing_low`: most recent swing high that was
    broken upward then reclaimed downward by the latest close."""
    n = min(lookback, len(df))
    if n < order * 2 + 4:
        return None
    highs = df["high"].to_numpy(dtype=float)
    last_close = float(df["close"].iloc[-1])
    end = len(df) - order
    start = max(order, len(df) - n)
    for i in range(end - 1, start - 1, -1):
        window = highs[max(0, i - order):i + order + 1]
        if highs[i] < window.max():
            continue
        sw_hi = float(highs[i])
        tail = highs[i + 1:]
        if tail.size == 0 or tail.max() <= sw_hi:
            continue
        if last_close >= sw_hi:
            continue
        return i, sw_hi
    return None


# ─── Breaker Block detectors ─────────────────────────────────────────────────

def detect_breaker_block_bullish(df: pd.DataFrame) -> dict | None:
    """Bullish Breaker: a swing low was broken (sweep) and price has since
    reclaimed above it. The last *down* candle just before that swing low
    — originally a supply OB — is now flipped and acts as support on retest.

    Trigger: latest bar is touching the breaker zone within `TEST_TOLERANCE_A`
    ATR.
    """
    if len(df) < LOOKBACK:
        return None
    atr = _atr_value(df)
    if atr <= 0:
        return None
    sl = _find_swept_swing_low(df)
    if sl is None:
        return None
    lo_idx, lo_price = sl
    # Last down candle within `lookback` bars before the swing low — the
    # supply OB that becomes the breaker after the failed move down.
    breaker_top = breaker_bot = None
    for i in range(lo_idx - 1, max(-1, lo_idx - LOOKBACK), -1):
        o, c = float(df["open"].iloc[i]), float(df["close"].iloc[i])
        if c < o:
            breaker_top = float(df["high"].iloc[i])
            breaker_bot = float(df["low"].iloc[i])
            break
    if breaker_top is None or breaker_bot is None:
        return None
    last_low   = float(df["low"].iloc[-1])
    last_close = float(df["close"].iloc[-1])
    if last_low > breaker_top + TEST_TOLERANCE_A * atr:
        return None
    if last_close < breaker_bot - TEST_TOLERANCE_A * atr:
        return None
    return {
        "name": "breaker_block_bullish",
        "side": "Long",
        "details": (
            f"Bullish breaker: swing low {lo_price:.4f} (idx {lo_idx}) swept then reclaimed; "
            f"breaker zone [{breaker_bot:.4f}, {breaker_top:.4f}] retested at {last_close:.4f}"
        ),
    }


def detect_breaker_block_bearish(df: pd.DataFrame) -> dict | None:
    """Mirror: swing high was swept upward then reclaimed back below. Last UP
    candle before the swing high becomes resistance on retest."""
    if len(df) < LOOKBACK:
        return None
    atr = _atr_value(df)
    if atr <= 0:
        return None
    sh = _find_swept_swing_high(df)
    if sh is None:
        return None
    hi_idx, hi_price = sh
    breaker_top = breaker_bot = None
    for i in range(hi_idx - 1, max(-1, hi_idx - LOOKBACK), -1):
        o, c = float(df["open"].iloc[i]), float(df["close"].iloc[i])
        if c > o:
            breaker_top = float(df["high"].iloc[i])
            breaker_bot = float(df["low"].iloc[i])
            break
    if breaker_top is None or breaker_bot is None:
        return None
    last_high  = float(df["high"].iloc[-1])
    last_close = float(df["close"].iloc[-1])
    if last_high < breaker_bot - TEST_TOLERANCE_A * atr:
        return None
    if last_close > breaker_top + TEST_TOLERANCE_A * atr:
        return None
    return {
        "name": "breaker_block_bearish",
        "side": "Short",
        "details": (
            f"Bearish breaker: swing high {hi_price:.4f} (idx {hi_idx}) swept then rejected; "
            f"breaker zone [{breaker_bot:.4f}, {breaker_top:.4f}] retested at {last_close:.4f}"
        ),
    }


# ─── Mitigation Block detectors ──────────────────────────────────────────────

def _last_displacement_block(df: pd.DataFrame, side: str, lookback: int = LOOKBACK) -> tuple[int, float, float] | None:
    """Return ``(idx, top, bot)`` of the last opposite-side OB candle that was
    immediately followed by a *strong displacement* candle.

    ``DISPLACEMENT_ATR_MIN`` (default 1.5 × ATR) is the minimum body size of
    the displacement candle — large enough to qualify as an impulsive move
    away from the OB, small enough to remain achievable on lower-vol pairs.
    Tighten to ``2.0–3.0`` for stricter ICT-style filtering on high-vol
    instruments; loosen to ``1.0`` to recover more candidates on quiet pairs.

    For ``side="Long"``: last DOWN candle followed by a big GREEN candle.
    For ``side="Short"``: mirror.
    """
    atr = _atr_value(df)
    if atr <= 0:
        return None
    n = min(lookback, len(df) - 1)
    for i in range(len(df) - 2, len(df) - 1 - n, -1):
        if i - 1 < 0:
            break
        ob_o = float(df["open"].iloc[i - 1])
        ob_c = float(df["close"].iloc[i - 1])
        disp_o = float(df["open"].iloc[i])
        disp_c = float(df["close"].iloc[i])
        body = abs(disp_c - disp_o)
        if body < DISPLACEMENT_ATR_MIN * atr:
            continue
        if side == "Long" and ob_c < ob_o and disp_c > disp_o:
            top = float(df["high"].iloc[i - 1])
            bot = float(df["low"].iloc[i - 1])
            return i - 1, top, bot
        if side == "Short" and ob_c > ob_o and disp_c < disp_o:
            top = float(df["high"].iloc[i - 1])
            bot = float(df["low"].iloc[i - 1])
            return i - 1, top, bot
    return None


def detect_mitigation_block_bullish(df: pd.DataFrame) -> dict | None:
    """First tap into a fresh bullish OB (last down candle that preceded a
    strong up-displacement). Triggers when price returns inside the OB box
    *for the first time* — verified by checking no candle since the
    displacement candle has touched the OB top."""
    if len(df) < LOOKBACK:
        return None
    found = _last_displacement_block(df, side="Long")
    if found is None:
        return None
    ob_idx, top, bot = found
    # First-tap check: skip the displacement candle (ob_idx + 1) since its
    # low naturally sits at/inside the OB; only intermediate bars after the
    # displacement count as "taps".
    between = df.iloc[ob_idx + 2:-1]
    if not between.empty and float(between["low"].min()) <= top:
        return None
    last_low = float(df["low"].iloc[-1])
    last_close = float(df["close"].iloc[-1])
    if last_low > top:
        return None
    if last_close < bot:
        return None
    return {
        "name": "mitigation_block_bullish",
        "side": "Long",
        "details": (
            f"First-tap bullish OB at [{bot:.4f}, {top:.4f}] (idx {ob_idx}) "
            f"retested at {last_close:.4f}"
        ),
    }


def detect_mitigation_block_bearish(df: pd.DataFrame) -> dict | None:
    if len(df) < LOOKBACK:
        return None
    found = _last_displacement_block(df, side="Short")
    if found is None:
        return None
    ob_idx, top, bot = found
    # Skip the displacement candle (its high naturally touches OB bottom).
    between = df.iloc[ob_idx + 2:-1]
    if not between.empty and float(between["high"].max()) >= bot:
        return None
    last_high = float(df["high"].iloc[-1])
    last_close = float(df["close"].iloc[-1])
    if last_high < bot:
        return None
    if last_close > top:
        return None
    return {
        "name": "mitigation_block_bearish",
        "side": "Short",
        "details": (
            f"First-tap bearish OB at [{bot:.4f}, {top:.4f}] (idx {ob_idx}) "
            f"retested at {last_close:.4f}"
        ),
    }


# ─── Registry & aggregator ───────────────────────────────────────────────────

DETECTORS: dict[str, Callable[[pd.DataFrame], dict | None]] = {
    "breaker_block_bullish":     detect_breaker_block_bullish,
    "breaker_block_bearish":     detect_breaker_block_bearish,
    "mitigation_block_bullish":  detect_mitigation_block_bullish,
    "mitigation_block_bearish":  detect_mitigation_block_bearish,
}


def detect_all(df: pd.DataFrame) -> list[dict]:
    """Run every ICT extras detector and return the list of hits."""
    if df is None or len(df) < LOOKBACK:
        return []
    if not {"open", "high", "low", "close"}.issubset(df.columns):
        return []
    hits: list[dict] = []
    for name, fn in DETECTORS.items():
        try:
            hit = fn(df)
        except Exception as e:
            logger.debug(f"[ict:{name}] detector error: {e}")
            continue
        if hit:
            hits.append(hit)
    return hits

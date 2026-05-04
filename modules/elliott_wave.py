"""
elliott_wave.py — Elliott Wave ABC corrective pattern detector
==============================================================

Detects a *completed* A-B-C three-wave correction terminating at the most
recent swing pivot. ABC corrections appear within a larger trend; their
completion signals end-of-pullback and likely resumption of the prior trend
direction. This is one of the highest-edge Elliott Wave setups because the
Wave-2 / Wave-4 / Wave-B end-zones cluster around well-defined Fibonacci
ratios (Frost & Prechter, *Elliott Wave Principle*, ch. 2).

──────────────────────────────────────────────────────────────────────────────
Wave anatomy
──────────────────────────────────────────────────────────────────────────────

Counter-trend leg sequence (bullish ABC = correction DOWN inside an uptrend):

    H₀ ── A ──► L₁ ── B ──► H₂ ── C ──► L₃        (pivot kinds: H L H L)
     |          |          |          |
     prior      end of     end of B   end of C  ← *trigger* candle: last
     swing      A leg      (C starts)  (correction        close > L₃ → bullish
     high                              presumed completed)  reversal in motion

Symmetric for bearish ABC (correction UP inside a downtrend): pivots L H L H.

──────────────────────────────────────────────────────────────────────────────
Validation
──────────────────────────────────────────────────────────────────────────────

Let  ``A = |H₀ − L₁|`` (the impulse leg of the correction)
     ``B_retrace = |H₂ − L₁| / A``
     ``C_extension = |H₂ − L₃| / A``

Fibonacci constraints (canonical zigzag / flat / irregular bands):

    B_retrace      ∈ [0.382, 0.95]   (zigzag 0.382–0.618; flat ≥0.90)
    C_extension    ∈ [0.618, 2.000]  (typical 1.000; extended C → 1.618)

Sub-type label (informational only):

    "zigzag"     — B 0.382–0.618 of A, C ≥ 1.0 of A (sharpest, highest edge)
    "flat"       — B 0.618–0.95 of A, C 0.95–1.382 of A
    "irregular"  — B > 1.0 of A (truncated — C may extend deep)

──────────────────────────────────────────────────────────────────────────────
Trend-context filter
──────────────────────────────────────────────────────────────────────────────

A bullish ABC is only meaningful inside a prior **uptrend**. We require the
20-bar close just before H₀ (the start of A) to be at least ``MIN_TREND_PCT``
below H₀'s price. Symmetric for bearish.

──────────────────────────────────────────────────────────────────────────────
Reversal trigger (suppress mid-correction false positives)
──────────────────────────────────────────────────────────────────────────────

Fire the hit only on the bar that *closes back through* the completion zone:

    bullish_abc → close[-1] > L₃   (price has started to lift off C low)
    bearish_abc → close[-1] < H₃   (price has started to fall off C high)

This avoids alerting in the middle of an unfolding C-leg where the geometry
*looks* complete but momentum has not yet flipped.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

logger = logging.getLogger("ElliottWave")


# ─── Tunables ────────────────────────────────────────────────────────────────

PIVOT_ORDER       = 5      # argrelextrema half-window (bars on each side)
LOOKBACK_BARS     = 120    # only consider pivots in trailing window
TREND_LOOKBACK    = 20     # bars before pivot H₀/L₀ used for trend check
MIN_TREND_PCT     = 0.015  # prior leg must move ≥1.5 % to qualify as a trend

B_RETRACE_MIN     = 0.382
B_RETRACE_MAX     = 0.95
C_EXTENSION_MIN   = 0.618
C_EXTENSION_MAX   = 2.000

ZIGZAG_B_MAX      = 0.618
ZIGZAG_C_MIN      = 1.000
FLAT_B_MIN        = 0.618
FLAT_C_MAX        = 1.382


# ─── Pivot detection (mirrors harmonic_patterns._find_pivots) ────────────────

def _find_pivots(df: pd.DataFrame, order: int = PIVOT_ORDER) -> list[tuple[int, float, str]]:
    """Return alternating-kind pivots ``[(idx, price, 'H'|'L'), …]`` sorted by
    bar index. Plateau collapses adopt the more extreme bar so flat candles
    don't fragment a single swing into two.
    """
    if len(df) < order * 2 + 1:
        return []
    highs = df["high"].to_numpy(dtype=float)
    lows  = df["low"].to_numpy(dtype=float)
    h_idx = argrelextrema(highs, np.greater_equal, order=order)[0]
    l_idx = argrelextrema(lows,  np.less_equal,    order=order)[0]
    seen: dict[int, tuple[int, float, str]] = {}
    for i in h_idx:
        seen[int(i)] = (int(i), float(highs[i]), "H")
    for i in l_idx:
        i = int(i)
        if i in seen:
            h_dev = float(highs[i]) - float(highs[max(0, i-5):min(len(highs), i+6)].mean())
            l_dev = float(lows[max(0, i-5):min(len(lows), i+6)].mean()) - float(lows[i])
            if l_dev > h_dev:
                seen[i] = (i, float(lows[i]), "L")
        else:
            seen[i] = (i, float(lows[i]), "L")
    pivots = sorted(seen.values(), key=lambda p: p[0])
    cleaned: list[tuple[int, float, str]] = []
    for piv in pivots:
        if cleaned and cleaned[-1][2] == piv[2] and (piv[0] - cleaned[-1][0]) <= order:
            prev = cleaned[-1]
            if piv[2] == "H":
                if piv[1] >= prev[1]:
                    cleaned[-1] = piv
            else:
                if piv[1] <= prev[1]:
                    cleaned[-1] = piv
            continue
        cleaned.append(piv)
    return cleaned


# ─── Trend-context filter ────────────────────────────────────────────────────

def _prior_trend_is_up(df: pd.DataFrame, h0_idx: int) -> bool:
    """True when close ``TREND_LOOKBACK`` bars before H₀ was at least
    ``MIN_TREND_PCT`` below H₀'s price (i.e. the market rallied INTO H₀)."""
    start = h0_idx - TREND_LOOKBACK
    if start < 0:
        return False
    h0_price = float(df["high"].iloc[h0_idx])
    ref      = float(df["close"].iloc[start])
    if h0_price <= 0 or ref <= 0:
        return False
    return (h0_price - ref) / ref >= MIN_TREND_PCT


def _prior_trend_is_down(df: pd.DataFrame, l0_idx: int) -> bool:
    """Mirror: market sold off INTO L₀."""
    start = l0_idx - TREND_LOOKBACK
    if start < 0:
        return False
    l0_price = float(df["low"].iloc[l0_idx])
    ref      = float(df["close"].iloc[start])
    if l0_price <= 0 or ref <= 0:
        return False
    return (ref - l0_price) / ref >= MIN_TREND_PCT


# ─── Sub-type classification (informational) ─────────────────────────────────

def _classify_subtype(b_retrace: float, c_extension: float) -> str:
    """Return zigzag / flat / irregular based on Fibonacci bands."""
    if b_retrace > 1.0:
        return "irregular"
    if b_retrace <= ZIGZAG_B_MAX and c_extension >= ZIGZAG_C_MIN:
        return "zigzag"
    if b_retrace >= FLAT_B_MIN and c_extension <= FLAT_C_MAX:
        return "flat"
    return "complex"


# ─── ABC validators ──────────────────────────────────────────────────────────

def _check_bullish_abc(
    df: pd.DataFrame,
    pivots: list[tuple[int, float, str]],
) -> Optional[dict]:
    """Look for a recent H-L-H-L pivot tail forming a completed downward
    correction inside an uptrend, and confirm the most recent close has
    started to lift off L₃."""
    if len(pivots) < 4:
        return None
    # Walk back from the most recent pivot looking for the first valid
    # H-L-H-L tail. We allow the tail's L₃ to be the *last* pivot only
    # (otherwise the correction is already invalidated by newer structure).
    if pivots[-1][2] != "L":
        return None
    tail = pivots[-4:]
    kinds = [p[2] for p in tail]
    if kinds != ["H", "L", "H", "L"]:
        return None

    h0, l1, h2, l3 = tail
    h0_idx, h0_price = h0[0], h0[1]
    l1_idx, l1_price = l1[0], l1[1]
    h2_idx, h2_price = h2[0], h2[1]
    l3_idx, l3_price = l3[0], l3[1]

    # Geometry sanity: A and C must be downward legs (H₀>L₁ and H₂>L₃),
    # and B must be a partial bounce (H₂ between L₁ and H₀).
    if not (h0_price > l1_price and h2_price > l3_price):
        return None
    if not (l1_price < h2_price <= h0_price):
        return None

    a_size = h0_price - l1_price
    if a_size <= 0:
        return None
    b_retrace   = (h2_price - l1_price) / a_size
    c_extension = (h2_price - l3_price) / a_size

    if not (B_RETRACE_MIN <= b_retrace <= B_RETRACE_MAX):
        return None
    if not (C_EXTENSION_MIN <= c_extension <= C_EXTENSION_MAX):
        return None

    if not _prior_trend_is_up(df, h0_idx):
        return None

    # Reversal trigger — last close must have started to lift off L₃.
    last_close = float(df["close"].iloc[-1])
    if last_close <= l3_price:
        return None

    subtype = _classify_subtype(b_retrace, c_extension)
    return {
        "name": "elliott_abc_long",
        "side": "Long",
        "details": (
            f"{subtype} ABC complete | "
            f"A={a_size:.4g}  B={b_retrace:.0%} of A  C={c_extension:.0%} of A | "
            f"trigger={last_close:.4g} > L3={l3_price:.4g}"
        ),
    }


def _check_bearish_abc(
    df: pd.DataFrame,
    pivots: list[tuple[int, float, str]],
) -> Optional[dict]:
    """Mirror of :func:`_check_bullish_abc`: L-H-L-H tail at end of downtrend,
    last close has dipped below H₃."""
    if len(pivots) < 4:
        return None
    if pivots[-1][2] != "H":
        return None
    tail = pivots[-4:]
    kinds = [p[2] for p in tail]
    if kinds != ["L", "H", "L", "H"]:
        return None

    l0, h1, l2, h3 = tail
    l0_idx, l0_price = l0[0], l0[1]
    h1_idx, h1_price = h1[0], h1[1]
    l2_idx, l2_price = l2[0], l2[1]
    h3_idx, h3_price = h3[0], h3[1]

    if not (h1_price > l0_price and h3_price > l2_price):
        return None
    if not (l0_price <= l2_price < h1_price):
        return None

    a_size = h1_price - l0_price
    if a_size <= 0:
        return None
    b_retrace   = (h1_price - l2_price) / a_size
    c_extension = (h3_price - l2_price) / a_size

    if not (B_RETRACE_MIN <= b_retrace <= B_RETRACE_MAX):
        return None
    if not (C_EXTENSION_MIN <= c_extension <= C_EXTENSION_MAX):
        return None

    if not _prior_trend_is_down(df, l0_idx):
        return None

    last_close = float(df["close"].iloc[-1])
    if last_close >= h3_price:
        return None

    subtype = _classify_subtype(b_retrace, c_extension)
    return {
        "name": "elliott_abc_short",
        "side": "Short",
        "details": (
            f"{subtype} ABC complete | "
            f"A={a_size:.4g}  B={b_retrace:.0%} of A  C={c_extension:.0%} of A | "
            f"trigger={last_close:.4g} < H3={h3_price:.4g}"
        ),
    }


# ─── Public entry ────────────────────────────────────────────────────────────

def detect_all(df: pd.DataFrame) -> list[dict]:
    """Run both bullish and bearish ABC checks and return all hits.

    Both directions are independent; in practice only one (or none) fires
    per bar — but the registry pattern is to return a flat list either way.
    """
    if df is None or len(df) < (PIVOT_ORDER * 2 + TREND_LOOKBACK + 5):
        return []

    # Limit pivot search to recent window so old structure doesn't pollute
    # the tail. We still need TREND_LOOKBACK extra bars for the trend filter.
    window = df.tail(LOOKBACK_BARS + TREND_LOOKBACK).reset_index(drop=True)
    try:
        pivots = _find_pivots(window)
    except Exception as e:
        logger.debug(f"_find_pivots failed: {e}")
        return []
    if not pivots:
        return []

    hits: list[dict] = []
    bull = _check_bullish_abc(window, pivots)
    if bull:
        hits.append(bull)
    bear = _check_bearish_abc(window, pivots)
    if bear:
        hits.append(bear)
    return hits

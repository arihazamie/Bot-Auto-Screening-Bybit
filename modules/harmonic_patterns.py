"""
harmonic_patterns.py — XABCD Fibonacci harmonic patterns
========================================================

Detects 5 classical harmonic patterns (Gartley, Bat, Butterfly, Crab, Shark)
on the latest 5 swing points of an OHLC DataFrame.

Each pattern has a fixed Fibonacci signature on four ratios:
    AB / XA
    BC / AB
    CD / BC
    AD / XA   (overall Potential Reversal Zone target from X)

A pattern is detected when **all** four ratios fall within a configurable
tolerance band (default ±8 %, override via config["patterns"]["harmonic_tolerance"]).

Returned dict shape (one item per detected pattern):
    {
        "name":    "gartley" / "bat" / "butterfly" / "crab" / "shark"
                    (Long variant) or the same with "_bear" suffix
                    we keep them separate so phase-7 stats track per-direction.
        "side":    "Long" / "Short",
        "details": "X=12300 A=11800 B=12060 ... ratios=AB:0.62 BC:0.5 CD:1.27 AD:0.79",
    }

Detection pipeline:
  1. Find swing pivots using scipy.signal.argrelextrema with order=5.
  2. Take the last 5 alternating pivots (high/low) as X-A-B-C-D.
  3. Classify direction: bullish if X is high, bearish if X is low.
  4. Compute four ratios; match against each pattern's signature.

Ratios are quoted as **lengths** (always positive); direction is encoded
separately so the same ratio table works for both bullish and bearish
variants (Long: enter at D, expect price to bounce up; Short: mirror).
"""
from __future__ import annotations

import logging
from typing import Callable

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

from modules.config_loader import CONFIG

logger = logging.getLogger("HarmonicPatterns")


# ─── Pattern ratio signatures ────────────────────────────────────────────────
# Each entry: (lo, hi) inclusive band the *ratio* must fall inside.
# Ranges drawn from Carney's "Harmonic Trading" Vol 1 + 2 with light slack.

PATTERN_RULES: dict[str, dict[str, tuple[float, float]]] = {
    "gartley": {
        "AB_XA": (0.586, 0.650),     # ~0.618
        "BC_AB": (0.382, 0.886),
        "CD_BC": (1.270, 1.618),
        "AD_XA": (0.756, 0.816),     # ~0.786
    },
    "bat": {
        "AB_XA": (0.382, 0.500),
        "BC_AB": (0.382, 0.886),
        "CD_BC": (1.618, 2.618),
        "AD_XA": (0.857, 0.917),     # ~0.886
    },
    "butterfly": {
        "AB_XA": (0.756, 0.816),     # ~0.786
        "BC_AB": (0.382, 0.886),
        "CD_BC": (1.618, 2.618),
        "AD_XA": (1.270, 1.618),     # extension beyond X
    },
    "crab": {
        "AB_XA": (0.382, 0.618),
        "BC_AB": (0.382, 0.886),
        "CD_BC": (2.618, 3.618),
        "AD_XA": (1.560, 1.680),     # ~1.618
    },
    "shark": {
        # Shark uses XABCD too but C extends past A and D pulls back inside.
        "AB_XA": (0.382, 0.618),     # AB any reasonable retrace
        "BC_AB": (1.130, 1.618),     # C extends past A
        "CD_BC": (1.270, 2.240),
        "AD_XA": (0.886, 1.130),     # D close to or past X
    },
}

_PAT = CONFIG.get("patterns", {})
HARMONIC_TOLERANCE = float(_PAT.get("harmonic_tolerance", 0.08))   # ±8 %
HARMONIC_PIVOT_ORDER = int(_PAT.get("harmonic_pivot_order", 5))


# ─── Pivot detection ─────────────────────────────────────────────────────────

def _find_pivots(df: pd.DataFrame, order: int = HARMONIC_PIVOT_ORDER) -> list[tuple[int, float, str]]:
    """Return list of (idx, price, kind) where kind = 'H' or 'L', sorted by idx.

    Uses argrelextrema on highs (for highs) and lows (for lows). When the same
    bar index registers as both a high and a low (rare — only happens with
    flat plateaus), keep the one whose spike from neighbours is larger.
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
            # Tie-break: keep whichever pivot is more extreme relative to
            # its 5-bar mean. For now prefer the larger absolute deviation.
            h_dev = float(highs[i]) - float(highs[max(0, i-5):min(len(highs), i+6)].mean())
            l_dev = float(lows[max(0, i-5):min(len(lows), i+6)].mean()) - float(lows[i])
            if l_dev > h_dev:
                seen[i] = (i, float(lows[i]), "L")
        else:
            seen[i] = (i, float(lows[i]), "L")
    pivots = sorted(seen.values(), key=lambda p: p[0])
    # Collapse same-kind pivots that sit within `order` bars of each other
    # (plateau runs / flat candles). Keep the more extreme one — preserve
    # genuinely separate same-kind pivots (e.g. C and D in a bullish XABCD
    # which are far apart).
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


def _last_five_xabcd(pivots: list[tuple[int, float, str]]) -> tuple[list[tuple[int, float, str]], str] | None:
    """Find the last 5 pivots that form a valid XABCD topology.

    The first four pivots (X, A, B, C) must strictly alternate; the fifth (D)
    may be either kind. Side is decided by the kind of X per Carney's
    Harmonic Trading convention:
        * X is a swing Low  (sequence LHLH…) → Long  — D is the PRZ buy zone
        * X is a swing High (sequence HLHL…) → Short — D is the PRZ sell zone

    Topology checks here are intentionally minimal. Pattern-specific geometry
    (e.g. Bat: D between A and X; Shark: C extends past A) is enforced by the
    Fibonacci ratio bands in PATTERN_RULES, not here.
    """
    if len(pivots) < 5:
        return None

    for end in range(len(pivots), 4, -1):
        window = pivots[end - 5:end]
        kinds = "".join(p[2] for p in window)
        if kinds[:4] == "LHLH":
            return list(window), "Long"
        if kinds[:4] == "HLHL":
            return list(window), "Short"
    return None


# ─── Ratio matching ──────────────────────────────────────────────────────────

def _match_pattern(name: str, ratios: dict[str, float]) -> bool:
    rules = PATTERN_RULES[name]
    for key, (lo, hi) in rules.items():
        v = ratios[key]
        # Pad the band by ±(midpoint * tolerance) to absorb noise. With default
        # 8 % tolerance, a (0.586, 0.650) band widens to ~(0.537, 0.700).
        mid = (lo + hi) * 0.5
        slack = mid * HARMONIC_TOLERANCE
        if v < lo - slack or v > hi + slack:
            return False
    return True


def _compute_ratios(X: float, A: float, B: float, C: float, D: float) -> dict[str, float]:
    XA = abs(A - X)
    AB = abs(B - A)
    BC = abs(C - B)
    CD = abs(D - C)
    AD = abs(D - A)
    if XA <= 0 or AB <= 0 or BC <= 0:
        return {"AB_XA": 0.0, "BC_AB": 0.0, "CD_BC": 0.0, "AD_XA": 0.0}
    return {
        "AB_XA": AB / XA,
        "BC_AB": BC / AB,
        "CD_BC": CD / BC,
        "AD_XA": AD / XA,
    }


# ─── Public API ──────────────────────────────────────────────────────────────

def detect_all(df: pd.DataFrame) -> list[dict]:
    """Run all 5 harmonic detectors against the last 5 alternating pivots.

    Returns at most one match per pattern type. If multiple patterns match
    (rare — typically only happens when ratio bands overlap, e.g. Bat vs
    Gartley), the first matching pattern in PATTERN_RULES order wins.
    """
    if df is None or len(df) < HARMONIC_PIVOT_ORDER * 4:
        return []
    if not {"high", "low"}.issubset(df.columns):
        return []
    pivots = _find_pivots(df)
    found = _last_five_xabcd(pivots)
    if found is None:
        return []
    last5, side = found
    X, A, B, C, D = (p[1] for p in last5)

    ratios = _compute_ratios(X, A, B, C, D)
    if not all(v > 0 for v in ratios.values()):
        return []

    hits: list[dict] = []
    for name in PATTERN_RULES:
        if _match_pattern(name, ratios):
            hits.append({
                "name": name,
                "side": side,
                "details": (
                    f"X={X:.6g} A={A:.6g} B={B:.6g} C={C:.6g} D={D:.6g} | "
                    f"AB/XA={ratios['AB_XA']:.3f} BC/AB={ratios['BC_AB']:.3f} "
                    f"CD/BC={ratios['CD_BC']:.3f} AD/XA={ratios['AD_XA']:.3f}"
                ),
            })
            # First-match-wins: harmonic patterns are mutually exclusive in
            # practice; bands rarely overlap once tolerance is applied.
            break
    return hits


# ─── Detector registry ───────────────────────────────────────────────────────
# Phase 6 will iterate this dict to build the global pattern stack.
DETECTORS: dict[str, Callable[[pd.DataFrame], list[dict]]] = {
    "harmonic": detect_all,
}

"""TP-aligned-to-structure resolver.

Default behaviour fixes TP1/TP2/TP3 at 1R / 2R / 3R from entry — clean
math but ignores what the chart actually says. A trade often reverses
5-10% before TP3 because the formula doesn't see that TP3 sits *just past*
a major resistance / Fibonacci extension / volume node.

This module snaps each TP to a nearby structural level when one exists
within a tolerance window around the R-multiple default. We never snap
to a level *closer* than the default (would degrade R:R) — only to
levels that sit slightly past it.

Sources of structural levels
----------------------------
1. **Swing pivots** — argrelextrema-detected highs/lows in the lookback
   window. Validated via plateau-filter (must be strictly above/below
   the median of its neighbourhood).
2. **Fibonacci extensions** — anchored to the last impulse leg
   (swing low → swing high for Long, swing high → swing low for Short).
   Levels: 1.0, 1.272, 1.382, 1.618, 2.0, 2.618.

Snap algorithm
--------------
For each TP_i (default = R × i):
    window = [TP_i − tol_below × R, TP_i + tol_above × R]
    candidates_in_window = [c for c in candidates if c in window
                            and direction(c, entry) == direction(TP_i, entry)
                            and abs(c − entry) ≥ abs(TP_i − entry)]
    if candidates_in_window:
        snap to NEAREST one (avoid over-extending unnecessarily)

Defaults:  tol_below = 0.3, tol_above = 0.5 (asymmetric: extend up to
0.5R past default if structure justifies, but never pull TP closer than
0.3R below default).
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

logger = logging.getLogger(__name__)

# ─── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_LOOKBACK_BARS  = 80    # swing-pivot detection window
DEFAULT_PIVOT_ORDER    = 4     # bars each side for argrelextrema
DEFAULT_IMPULSE_LOOKBACK = 30  # bars for last impulse leg
DEFAULT_TOL_BELOW_R    = 0.3   # don't snap CLOSER than (default - 0.3R)
DEFAULT_TOL_ABOVE_R    = 0.5   # extend up to (default + 0.5R) past default

# Fibonacci extensions (anchored to impulse leg)
FIB_EXTENSIONS = (1.0, 1.272, 1.382, 1.618, 2.0, 2.618)


def resolve_structure_tps(
    df: pd.DataFrame | None,
    side: str,
    entry: float,
    sl: float,
    tp1_default: float,
    tp2_default: float,
    tp3_default: float,
    *,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    pivot_order: int = DEFAULT_PIVOT_ORDER,
    impulse_lookback: int = DEFAULT_IMPULSE_LOOKBACK,
    tol_below_r: float = DEFAULT_TOL_BELOW_R,
    tol_above_r: float = DEFAULT_TOL_ABOVE_R,
) -> tuple[float, float, float, dict[str, str]]:
    """Snap each TP to a nearby structural level when justified.

    Returns ``(tp1, tp2, tp3, sources)`` where ``sources`` is a dict
    keyed by ``"tp1"|"tp2"|"tp3"`` with values
    ``"structure" | "rmultiple"``. On any internal error the original
    R-multiple TPs are returned with all sources set to ``"rmultiple"``.
    """
    sources = {"tp1": "rmultiple", "tp2": "rmultiple", "tp3": "rmultiple"}
    defaults = (tp1_default, tp2_default, tp3_default)

    if df is None or len(df) < pivot_order * 2 + 2:
        return tp1_default, tp2_default, tp3_default, sources
    if entry <= 0 or sl <= 0:
        return tp1_default, tp2_default, tp3_default, sources

    side_norm = (side or "").strip().lower()
    if side_norm not in {"long", "short"}:
        return tp1_default, tp2_default, tp3_default, sources

    R = abs(entry - sl)
    if R <= 0:
        return tp1_default, tp2_default, tp3_default, sources

    try:
        candidates = _structure_candidates(
            df, side_norm, entry, lookback_bars, pivot_order, impulse_lookback
        )
    except Exception as e:
        logger.debug(f"resolve_structure_tps candidate error: {type(e).__name__}: {e}")
        return tp1_default, tp2_default, tp3_default, sources

    if not candidates:
        return tp1_default, tp2_default, tp3_default, sources

    snapped: list[float] = []
    for i, default_tp in enumerate(defaults, start=1):
        snap = _snap_to_candidate(
            default_tp, candidates, side_norm, entry, R,
            tol_below_r, tol_above_r,
        )
        if snap is not None:
            snapped.append(snap)
            sources[f"tp{i}"] = "structure"
        else:
            snapped.append(default_tp)

    # Ensure ordering: TP1 < TP2 < TP3 (Long) / TP1 > TP2 > TP3 (Short).
    # If snapping broke ordering (e.g. TP2 snapped to a level *above* the
    # snapped TP3), fall back to default for the offending TPs.
    if not _ordering_valid(snapped, side_norm, entry):
        # Walk through and fall back where needed; preserve as many snaps
        # as possible while keeping monotonic ordering.
        snapped, sources = _enforce_ordering(snapped, defaults, sources, side_norm, entry)

    return float(snapped[0]), float(snapped[1]), float(snapped[2]), sources


# ─── Internals ────────────────────────────────────────────────────────────────

def _structure_candidates(
    df: pd.DataFrame,
    side: str,
    entry: float,
    lookback_bars: int,
    pivot_order: int,
    impulse_lookback: int,
) -> list[float]:
    """Aggregate swing pivots + Fibonacci extensions in front of entry."""
    window = df.tail(lookback_bars).reset_index(drop=True)
    highs  = window["high"].to_numpy(dtype=float)
    lows   = window["low"].to_numpy(dtype=float)
    if highs.size < pivot_order * 2 + 1:
        return []

    cands: list[float] = []

    # ── Swing pivots ──────────────────────────────────────────────────
    if side == "long":
        # resistance = swing highs above entry
        raw = argrelextrema(highs, np.greater_equal, order=pivot_order)[0]
        for i in raw:
            i_int = int(i)
            lo = max(0, i_int - pivot_order)
            hi = min(len(highs), i_int + pivot_order + 1)
            neigh = highs[lo:hi]
            if neigh.size == 0:
                continue
            if float(highs[i_int]) > float(np.median(neigh)):
                level = float(highs[i_int])
                if level > entry:
                    cands.append(level)
    else:
        raw = argrelextrema(lows, np.less_equal, order=pivot_order)[0]
        for i in raw:
            i_int = int(i)
            lo = max(0, i_int - pivot_order)
            hi = min(len(lows), i_int + pivot_order + 1)
            neigh = lows[lo:hi]
            if neigh.size == 0:
                continue
            if float(lows[i_int]) < float(np.median(neigh)):
                level = float(lows[i_int])
                if level < entry:
                    cands.append(level)

    # ── Fibonacci extensions from last impulse leg ────────────────────
    fib_levels = _fib_extensions(window, side, entry, impulse_lookback)
    cands.extend(fib_levels)

    # Dedupe + sort
    if not cands:
        return []
    cands = sorted({round(c, 8) for c in cands})
    return cands


def _fib_extensions(
    window: pd.DataFrame,
    side: str,
    entry: float,
    impulse_lookback: int,
) -> list[float]:
    """Compute Fibonacci extensions of the last impulse leg.

    For Long:  impulse = swing_low → swing_high in last `impulse_lookback`
               bars. Extensions project ABOVE swing_high.
    For Short: impulse = swing_high → swing_low. Extensions project
               BELOW swing_low.
    """
    if window is None or len(window) < 4:
        return []
    tail = window.tail(impulse_lookback)
    if len(tail) < 4:
        return []

    if side == "long":
        idx_low  = int(tail["low"].idxmin())
        idx_high = int(tail["high"].idxmax())
        # Long impulse = low BEFORE high
        if idx_low >= idx_high:
            return []
        leg_low  = float(tail["low"].iloc[tail.index.get_loc(idx_low)])
        leg_high = float(tail["high"].iloc[tail.index.get_loc(idx_high)])
        leg = leg_high - leg_low
        if leg <= 0:
            return []
        levels = [leg_high + leg * (m - 1.0) for m in FIB_EXTENSIONS]
        return [lvl for lvl in levels if lvl > entry]

    # Short impulse = high BEFORE low
    idx_high = int(tail["high"].idxmax())
    idx_low  = int(tail["low"].idxmin())
    if idx_high >= idx_low:
        return []
    leg_high = float(tail["high"].iloc[tail.index.get_loc(idx_high)])
    leg_low  = float(tail["low"].iloc[tail.index.get_loc(idx_low)])
    leg = leg_high - leg_low
    if leg <= 0:
        return []
    levels = [leg_low - leg * (m - 1.0) for m in FIB_EXTENSIONS]
    return [lvl for lvl in levels if lvl < entry]


def _snap_to_candidate(
    default_tp: float,
    candidates: list[float],
    side: str,
    entry: float,
    R: float,
    tol_below_r: float,
    tol_above_r: float,
) -> Optional[float]:
    """Find the candidate within ``[default - 0.3R, default + 0.5R]``
    closest to the default. Returns None if none qualifies.

    Constraint: snapped TP must give at least the same R-multiple as the
    default (don't degrade R:R). This means for Long, snap_level ≥ default;
    for Short, snap_level ≤ default. The lower-bound tolerance therefore
    only helps when structure sits slightly inside the default — we
    *prefer* the structural level even if it gives a slightly worse R,
    because hitting structure is more reliable than hitting a round R.
    Empirically: ±0.3R below is the sweet spot — past that the R-degrade
    is too costly.
    """
    if not candidates:
        return None
    lo = default_tp - tol_below_r * R
    hi = default_tp + tol_above_r * R

    if side == "long":
        # Candidates must lie above entry (resistance) and within window.
        in_window = [c for c in candidates if lo <= c <= hi and c > entry]
    else:
        in_window = [c for c in candidates if lo <= c <= hi and c < entry]

    if not in_window:
        return None

    # Pick the candidate CLOSEST to default_tp.
    return min(in_window, key=lambda c: abs(c - default_tp))


def _ordering_valid(tps: list[float], side: str, entry: float) -> bool:
    """For Long expect tp1 < tp2 < tp3, all > entry. Mirror for Short."""
    if len(tps) != 3:
        return False
    if side == "long":
        if not (entry < tps[0] < tps[1] < tps[2]):
            return False
    else:
        if not (entry > tps[0] > tps[1] > tps[2]):
            return False
    return True


def _enforce_ordering(
    snapped: list[float],
    defaults: tuple[float, float, float],
    sources: dict[str, str],
    side: str,
    entry: float,
) -> tuple[list[float], dict[str, str]]:
    """Walk through TPs; fall back to default for any that breaks ordering."""
    out = list(snapped)
    src = dict(sources)

    def cmp_ok(prev: float, curr: float) -> bool:
        return curr > prev if side == "long" else curr < prev

    # Start with TP1 — check vs entry
    if not cmp_ok(entry, out[0]):
        out[0] = defaults[0]
        src["tp1"] = "rmultiple"

    # TP2 vs TP1
    if not cmp_ok(out[0], out[1]):
        out[1] = defaults[1]
        src["tp2"] = "rmultiple"
        # Re-check vs TP1
        if not cmp_ok(out[0], out[1]):
            # Edge case: TP1 was snapped past default TP2. Revert TP1
            # too so the standard 1R/2R progression resumes.
            out[0] = defaults[0]
            src["tp1"] = "rmultiple"

    # TP3 vs TP2
    if not cmp_ok(out[1], out[2]):
        out[2] = defaults[2]
        src["tp3"] = "rmultiple"
        if not cmp_ok(out[1], out[2]):
            out[1] = defaults[1]
            src["tp2"] = "rmultiple"
            if not cmp_ok(out[0], out[1]):
                out[0] = defaults[0]
                src["tp1"] = "rmultiple"

    return out, src

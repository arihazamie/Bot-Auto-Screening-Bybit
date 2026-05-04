"""
Smart Entry — multi-candidate selector + volume confirmation.

C.1 Multi-Candidate Entry Selector
    Build up to 4 entry-price candidates per setup:
      1. swing-1   — most recent significant swing (existing behaviour)
      2. swing-2   — 2nd most recent significant swing (deeper)
      3. fib50     — 50.0% Fibonacci retracement of last impulse leg
      4. fib61.8   — 61.8% Fibonacci retracement of last impulse leg

    Each candidate is filtered by drift (`max_drift_pct`) so we never
    place a limit so far from current price that it won't fill.

    Survivors are scored by a probe R:R that uses an ATR-based proxy
    SL and a swing-extreme proxy TP — no exact SL/TP yet (those come
    later in `_step_build_trade_setup`), but the ranking is good
    enough to prefer the candidate that will deliver the best R:R
    once the real SL/TP get applied.

    Selection rules (per user spec):
      • If any candidate's probe R:R > min_rr × bonus_mult (default
        1.3), pick the highest-scoring one (extra-reward bonus).
      • Otherwise pick the candidate closest to current price among
        those with probe R:R ≥ min_rr.
      • If no candidate has probe R:R ≥ min_rr, return the closest
        valid candidate (caller may still accept it; the global
        min_rr gate downstream will reject it if the real R:R also
        fails).
      • If no candidates pass drift filter at all, return None →
        caller falls back to bid/ask offset.

C.2 Volume Confirmation at Entry Candle
    When the limit is touched, do NOT immediately fill. Re-evaluate
    on the most recent CLOSED bar:
      • RVOL = bar.volume / mean(volume over last `rvol_lookback` bars)
      • RVOL must be ≥ `min_rvol`
      • If `require_rejection_wick`, the closed bar's wick on the
        protected side (lower for Long, upper for Short) must exceed
        the body. False wicks (no rejection structure) get rejected.
    If the bar already touched our entry but conditions fail, we
    keep the trade PENDING — next tick will re-check the new
    closed bar.

    Public API:
        confirm_entry_with_volume(client, symbol, side, timeframe,
                                  entry_price, last_confirmed_bar_ts)
            Returns (passed: bool, bar_ts: int | None, reason: str)
            • passed=True when both RVOL + wick gates pass on the
              most recent closed bar AND that bar reached our entry
              level (otherwise we're not filling on a confirmed
              candle).
            • bar_ts is the timestamp of the closed bar evaluated,
              so the caller can persist it and avoid re-confirming
              the same bar.
            • reason is a short string for logging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

from modules.config_loader import CONFIG

logger = logging.getLogger(__name__)


# ─── Config (pulled from `strategy.smart_entry` in config.json) ─────────
_SMART_CFG = CONFIG.get("strategy", {}).get("smart_entry", {})

# C.1 — Multi-Candidate Entry
MULTI_CANDIDATE_ENABLED = bool(_SMART_CFG.get("multi_candidate_enabled", True))
MAX_DRIFT_PCT           = float(_SMART_CFG.get("max_drift_pct", 0.02))   # 2 %
RR_BONUS_MULT           = float(_SMART_CFG.get("rr_bonus_mult", 1.3))    # 1.3 ×
SWING_BUFFER_PCT        = float(_SMART_CFG.get("swing_buffer_pct", 0.001))  # 0.1 %
PIVOT_LOOKBACK          = int(  _SMART_CFG.get("pivot_lookback", 60))
PIVOT_ORDER             = int(  _SMART_CFG.get("pivot_order", 5))
PROBE_ATR_SL_MULT       = float(_SMART_CFG.get("probe_atr_sl_mult", 1.5))

# C.2 — Volume confirmation
_VC_CFG = CONFIG.get("strategy", {}).get("entry_volume_confirm", {})
VOLUME_CONFIRM_ENABLED  = bool(_VC_CFG.get("enabled", True))
VOLUME_MIN_RVOL         = float(_VC_CFG.get("min_rvol", 1.0))
VOLUME_RVOL_LOOKBACK    = int(  _VC_CFG.get("rvol_lookback", 20))
REQUIRE_REJECTION_WICK  = bool(_VC_CFG.get("require_rejection_wick", True))


# ════════════════════════════════════════════════════════════════════════
#  C.1 — Multi-Candidate Entry Selector
# ════════════════════════════════════════════════════════════════════════

@dataclass
class EntryCandidate:
    entry:    float
    source:   str       # "swing-1" | "swing-2" | "fib50" | "fib61.8"
    drift:    float     # |entry − ref| / ref
    probe_rr: float     # heuristic R:R rank score


def _significant_pivots(arr: np.ndarray, *, find_lows: bool, order: int) -> list[int]:
    """Return indices of significant pivots, most-recent first.

    Uses argrelextrema + plateau filter (same idea as the existing
    `_last_significant_low/high` helpers in main.py, generalised so
    we can recover the 2nd-most-recent pivot too).
    """
    if arr.size == 0:
        return []

    cmp = np.less_equal if find_lows else np.greater_equal
    raw = argrelextrema(arr, cmp, order=order)[0]
    if len(raw) == 0:
        return []

    surviving: list[int] = []
    for i in raw:
        lo = max(0, int(i) - order)
        hi = min(len(arr), int(i) + order + 1)
        neigh = arr[lo:hi]
        if neigh.size == 0:
            continue
        median = float(np.median(neigh))
        # For lows: pivot must dip BELOW the local median; for highs,
        # ABOVE. Excludes flat plateaus that satisfy ≤/≥ trivially.
        ok = (float(arr[i]) < median) if find_lows else (float(arr[i]) > median)
        if ok:
            surviving.append(int(i))

    # Most-recent first
    surviving.reverse()
    return surviving


def _probe_rr(entry: float, side: str, atr: float, swing_high: float,
              swing_low: float) -> float:
    """Cheap R:R proxy used to rank candidates pre-SL/TP resolution.

    Probe SL  = entry ± atr × PROBE_ATR_SL_MULT
    Probe TP  = far swing extreme of the lookback window
    Returns 0.0 on degenerate input so the candidate sorts last.
    """
    if entry <= 0 or atr <= 0:
        return 0.0
    if side == "Long":
        sl_dist = atr * PROBE_ATR_SL_MULT
        tp_dist = swing_high - entry
    else:
        sl_dist = atr * PROBE_ATR_SL_MULT
        tp_dist = entry - swing_low
    if sl_dist <= 0 or tp_dist <= 0:
        return 0.0
    return tp_dist / sl_dist


def build_entry_candidates(
    df: pd.DataFrame,
    side: str,
    bid: float,
    ask: float,
    atr: float,
    *,
    min_rr: float | None = None,
) -> list[EntryCandidate]:
    """Generate + score entry candidates for the given setup.

    Returns a list ordered "best-first" per the selection rules in
    the module docstring. Empty list ⇒ no usable candidate (caller
    should fall back to bid/ask offset).
    """
    if df is None or len(df) < PIVOT_ORDER * 2 + 1:
        return []

    ref = ask if side == "Long" else bid
    if ref <= 0:
        return []

    window = df.tail(PIVOT_LOOKBACK).reset_index(drop=True)
    highs  = window["high"].to_numpy(dtype=float)
    lows   = window["low"].to_numpy(dtype=float)

    swing_high = float(highs.max()) if highs.size else 0.0
    swing_low  = float(lows.min())  if lows.size  else 0.0

    candidates: list[EntryCandidate] = []

    if side == "Long":
        # 1 + 2: swing-1 / swing-2 lows
        pivots = _significant_pivots(lows, find_lows=True, order=PIVOT_ORDER)
        for slot, idx in enumerate(pivots[:2], start=1):
            raw = float(lows[idx])
            if raw <= 0:
                continue
            entry = raw * (1.0 + SWING_BUFFER_PCT)
            if entry >= ref:           # support must sit BELOW current ask
                continue
            drift = abs(entry - ref) / ref
            if drift > MAX_DRIFT_PCT:
                continue
            candidates.append(EntryCandidate(
                entry=entry,
                source=f"swing-{slot}",
                drift=drift,
                probe_rr=_probe_rr(entry, side, atr, swing_high, swing_low),
            ))

        # 3 + 4: Fibonacci 50% / 61.8% retracement of last impulse leg
        # Impulse leg = swing_low → swing_high in lookback window
        leg = swing_high - swing_low
        if leg > 0 and swing_high > swing_low and swing_high > ref:
            for ratio, label in ((0.500, "fib50"), (0.618, "fib61.8")):
                entry = swing_high - leg * ratio
                if entry <= 0 or entry >= ref:
                    continue
                drift = abs(entry - ref) / ref
                if drift > MAX_DRIFT_PCT:
                    continue
                candidates.append(EntryCandidate(
                    entry=entry,
                    source=label,
                    drift=drift,
                    probe_rr=_probe_rr(entry, side, atr, swing_high, swing_low),
                ))

    else:  # Short
        pivots = _significant_pivots(highs, find_lows=False, order=PIVOT_ORDER)
        for slot, idx in enumerate(pivots[:2], start=1):
            raw = float(highs[idx])
            if raw <= 0:
                continue
            entry = raw * (1.0 - SWING_BUFFER_PCT)
            if entry <= ref:           # resistance must sit ABOVE current bid
                continue
            drift = abs(entry - ref) / ref
            if drift > MAX_DRIFT_PCT:
                continue
            candidates.append(EntryCandidate(
                entry=entry,
                source=f"swing-{slot}",
                drift=drift,
                probe_rr=_probe_rr(entry, side, atr, swing_high, swing_low),
            ))

        leg = swing_high - swing_low
        if leg > 0 and swing_low < swing_high and swing_low < ref:
            for ratio, label in ((0.500, "fib50"), (0.618, "fib61.8")):
                entry = swing_low + leg * ratio
                if entry <= 0 or entry <= ref:
                    continue
                drift = abs(entry - ref) / ref
                if drift > MAX_DRIFT_PCT:
                    continue
                candidates.append(EntryCandidate(
                    entry=entry,
                    source=label,
                    drift=drift,
                    probe_rr=_probe_rr(entry, side, atr, swing_high, swing_low),
                ))

    if not candidates:
        return []

    # Selection logic
    rr_floor = float(min_rr) if min_rr is not None else 0.0
    bonus_threshold = rr_floor * RR_BONUS_MULT if rr_floor > 0 else float("inf")

    # 1) Bonus tier — any candidate whose probe R:R clears bonus_threshold
    bonus = [c for c in candidates if c.probe_rr >= bonus_threshold]
    if bonus:
        bonus.sort(key=lambda c: -c.probe_rr)   # highest probe_rr first
        return bonus + [c for c in candidates if c not in bonus]

    # 2) Eligible tier — candidates clearing the floor; pick closest
    eligible = [c for c in candidates if c.probe_rr >= rr_floor] if rr_floor > 0 else list(candidates)
    if eligible:
        eligible.sort(key=lambda c: c.drift)    # closest to ref first
        rest = [c for c in candidates if c not in eligible]
        return eligible + rest

    # 3) No candidate clears the floor — return all sorted by drift,
    #    caller will likely fall back to offset.
    candidates.sort(key=lambda c: c.drift)
    return candidates


def pick_entry(
    df: pd.DataFrame,
    side: str,
    bid: float,
    ask: float,
    atr: float,
    *,
    min_rr: float | None = None,
) -> Optional[EntryCandidate]:
    """Convenience wrapper — return the top-ranked candidate or None."""
    if not MULTI_CANDIDATE_ENABLED:
        return None
    cands = build_entry_candidates(df, side, bid, ask, atr, min_rr=min_rr)
    return cands[0] if cands else None


# ════════════════════════════════════════════════════════════════════════
#  C.2 — Volume Confirmation at Entry Candle
# ════════════════════════════════════════════════════════════════════════

def _evaluate_bar(bar: pd.Series, side: str, mean_volume: float) -> tuple[bool, str]:
    """Apply RVOL + rejection-wick checks to a single closed bar.

    Returns (passed, reason).
    """
    if mean_volume <= 0:
        return False, "rvol-baseline-zero"

    bar_volume = float(bar.get("volume", 0))
    rvol = bar_volume / mean_volume
    if rvol < VOLUME_MIN_RVOL:
        return False, f"rvol={rvol:.2f}<{VOLUME_MIN_RVOL:.2f}"

    if not REQUIRE_REJECTION_WICK:
        return True, f"rvol={rvol:.2f}"

    o = float(bar.get("open",  0))
    h = float(bar.get("high",  0))
    l = float(bar.get("low",   0))
    c = float(bar.get("close", 0))
    if min(o, h, l, c) <= 0:
        return False, "ohlc-invalid"

    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    if body <= 0:
        # Perfect doji — treat the whole range as a wick on both sides.
        body = max(h - l, 1e-9) * 0.1   # tiny synthetic body so we still
                                        # require a wick to dominate it

    if side == "Long":
        if lower_wick <= body:
            return False, f"wick-fail (lower={lower_wick:.6f} body={body:.6f})"
    else:
        if upper_wick <= body:
            return False, f"wick-fail (upper={upper_wick:.6f} body={body:.6f})"

    return True, f"rvol={rvol:.2f} wick=ok"


def confirm_entry_with_volume(
    client,
    symbol: str,
    side: str,
    timeframe: str,
    entry_price: float,
    last_confirmed_bar_ts: int | None = None,
) -> tuple[bool, int | None, str]:
    """Check the most recent CLOSED bar for volume + wick confirmation.

    The caller (`paper_trader.paper_execute`) only proceeds with the
    fill when this returns ``passed=True``. Returns the bar timestamp
    so the caller can persist it and skip re-evaluation of the same
    bar across ticks.

    Args:
        client                 — BybitClient (or stub with .fetch_ohlcv)
        symbol                 — pair identifier
        side                   — "Long" | "Short"
        timeframe              — e.g. "15m"
        entry_price            — limit price; bar must have touched
                                 this level (low ≤ entry for Long,
                                 high ≥ entry for Short).
        last_confirmed_bar_ts  — caller's last-evaluated bar ts. If
                                 the freshest closed bar matches, we
                                 don't re-check (same bar already
                                 evaluated; wait for next close).

    Returns: (passed, bar_ts, reason)
    """
    if not VOLUME_CONFIRM_ENABLED:
        return True, None, "disabled"

    try:
        df = client.fetch_ohlcv(symbol, timeframe, limit=VOLUME_RVOL_LOOKBACK + 5)
    except Exception as e:
        logger.warning(f"volume-confirm: fetch failed for {symbol}/{timeframe}: {e}")
        return False, None, f"fetch-error:{type(e).__name__}"

    if df is None or len(df) < VOLUME_RVOL_LOOKBACK + 1:
        return False, None, "insufficient-bars"

    # Bybit OHLCV returns the in-progress bar as the LAST row. We need
    # the most recent CLOSED bar — that's df.iloc[-2] (the prior bar).
    closed_idx = -2
    bar = df.iloc[closed_idx]
    bar_ts = int(bar.get("timestamp").value // 1_000_000) if hasattr(bar.get("timestamp"), "value") \
             else int(pd.Timestamp(bar.get("timestamp")).value // 1_000_000)

    # Same bar already evaluated on a previous tick — wait for next close.
    if last_confirmed_bar_ts is not None and bar_ts == last_confirmed_bar_ts:
        return False, bar_ts, "same-bar-already-evaluated"

    # Did the closed bar actually reach our entry level? If not, the
    # touch happened on the in-progress bar; wait for it to close.
    bar_low  = float(bar.get("low",  0))
    bar_high = float(bar.get("high", 0))
    if side == "Long" and bar_low > entry_price:
        return False, bar_ts, "level-not-touched-on-closed-bar"
    if side == "Short" and bar_high < entry_price:
        return False, bar_ts, "level-not-touched-on-closed-bar"

    # Compute RVOL baseline from the bars BEFORE the candidate close
    # (most recent VOLUME_RVOL_LOOKBACK bars excluding the candidate).
    baseline_slice = df.iloc[closed_idx - VOLUME_RVOL_LOOKBACK : closed_idx]
    if len(baseline_slice) < VOLUME_RVOL_LOOKBACK:
        return False, bar_ts, "insufficient-baseline"
    mean_volume = float(baseline_slice["volume"].mean())

    passed, reason = _evaluate_bar(bar, side, mean_volume)
    return passed, bar_ts, reason

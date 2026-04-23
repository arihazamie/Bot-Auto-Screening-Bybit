"""
smc.py — Smart Money Concepts Analysis v2
==========================================
10/10 SMC Pipeline:

  1. Market Structure   (HH / HL / LH / LL)
  2. BOS / CHoCH        (Break of Structure / Change of Character)
  3. Fresh Order Blocks — with Displacement Confirmation filter
  4. Fair Value Gaps    — size-weighted scoring
  5. Liquidity Sweep    — equal-high/low pool grabbed then reversed
  6. Premium / Discount Zone + OTE (61.8–78.6% Fibonacci retracement)
  7. Inducement (IDM)   — minor swing swept before key level

Upgrades from v1 (8.5 → 10/10):
  [A] Pivot detection: strict np.less / np.greater — eliminates false pivots
      at equal-value candle sequences (plateaus) that argrelextrema ≤/≥ returns.
  [B] Premium / Discount + OTE: price mapped to last swing range (0–1).
      Discount (<50%) favors Long, Premium (>50%) favors Short.
      OTE Long  = 61.8–78.6% retracement from high → pos 21.4–38.2%.
      OTE Short = 61.8–78.6% retracement from low  → pos 61.8–78.6%.
  [C] OB Displacement filter: OB receives +1 bonus only when the engulfing
      candle is impulsive — body > 1.5× avg body OR displacement creates a FVG.
      Weak OBs still detected but do NOT earn the bonus point.
  [D] Inducement (IDM): minor swing wick-pierced then closed back confirms
      institutional stop hunt before the real move.
  [E] FVG size scoring: gap >= 0.5% of mid-price earns +1 bonus.

Score table:
  Market structure alignment    : +2 (HL/LH)  or +1 (HH/LL)
  BOS confirmation              : +2
  CHoCH early signal            : +1
  Fresh OB (Demand/Supply)      : +2   [+1 bonus — Displacement confirmed]
  FVG in zone                   : +1   [+1 bonus — Large FVG >= 0.5%]
  Liquidity Sweep               : +2
  Correct zone (Disc/Prem)      : +1
  OTE zone (61.8-78.6% retrace) : +2   (overrides plain zone +1)
  Inducement (IDM) confirmed    : +1
  Contradicting BOS             : -1
"""

import logging
import numpy as np
from scipy.signal import argrelextrema

logger = logging.getLogger("SMC")


# ── Pivot helpers ─────────────────────────────────────────────────────────────

def find_pivots(df, order: int = 5):
    """
    Return (highs, lows) DataFrames with a 'price' column.

    [Upgrade A] Uses strict np.less / np.greater instead of np.less_equal /
    np.greater_equal.  The <= / >= variants tag every candle in a horizontal
    plateau as a pivot — strict comparison only tags the first candle of the
    cluster, which is the correct SMC swing point.
    """
    low_idx  = argrelextrema(df["low"].values,  np.less,    order=order)[0]
    high_idx = argrelextrema(df["high"].values, np.greater, order=order)[0]
    highs = df.iloc[high_idx][["high"]].rename(columns={"high": "price"})
    lows  = df.iloc[low_idx][["low"]].rename(columns={"low":  "price"})
    return highs, lows


# ── 1. Market Structure ───────────────────────────────────────────────────────

def get_market_structure(df) -> str:
    highs, lows = find_pivots(df)
    if len(highs) < 2 or len(lows) < 2:
        return "Neutral"
    last_h, prev_h = highs["price"].iloc[-1], highs["price"].iloc[-2]
    last_l, prev_l = lows["price"].iloc[-1],  lows["price"].iloc[-2]
    curr = df["close"].iloc[-1]
    if abs(curr - last_l) / last_l < 0.015:
        return "HL" if last_l > prev_l else "LL"
    if abs(curr - last_h) / last_h < 0.015:
        return "HH" if last_h > prev_h else "LH"
    return "Mid-Range"


# ── 2. BOS / CHoCH ────────────────────────────────────────────────────────────

def detect_bos_choch(df):
    """
    BOS   = close breaks last swing IN trend direction   -> continuation.
    CHoCH = close breaks last swing AGAINST trend        -> early reversal.
    Returns (bos: str|None, choch: str|None).
    """
    highs, lows = find_pivots(df)
    if len(highs) < 2 or len(lows) < 2:
        return None, None

    curr   = df["close"].iloc[-1]
    last_h = highs["price"].iloc[-1]; prev_h = highs["price"].iloc[-2]
    last_l = lows["price"].iloc[-1];  prev_l = lows["price"].iloc[-2]

    bos = choch = None

    if curr > last_h and last_h > prev_h:    bos   = "Bullish BOS"
    elif curr < last_l and last_l < prev_l:  bos   = "Bearish BOS"
    if curr > last_h and last_h < prev_h:    choch = "Bullish CHoCH"
    elif curr < last_l and last_l > prev_l:  choch = "Bearish CHoCH"

    return bos, choch


# ── 3. Fresh Order Blocks + Displacement Filter ───────────────────────────────

def _avg_body(df, window: int = 20) -> float:
    """Average absolute candle body over last `window` candles."""
    bodies = (df["close"] - df["open"]).abs()
    mean   = bodies.rolling(window).mean().iloc[-1]
    return float(mean) if (mean is not None and mean > 0) else 1e-10


def find_order_blocks(df, lookback: int = 50) -> dict:
    """
    Fresh OBs only -- mitigated OBs discarded (body-close test from fix #7).

    [Upgrade C] Displacement filter:
    After OB formation the engulfing candle must show institutional force:
      - body > 1.5x avg_body  (impulsive / explosive move), OR
      - displacement creates a FVG (gap between OB candle high and +2 candle low).
    OBs without displacement are still included (displaced=False) and still
    score +2, but they do NOT earn the additional +1 bonus point.

    Returns dict:
      { "bull": [(ob_low, ob_high, displaced), ...],
        "bear": [(ob_low, ob_high, displaced), ...] }
    """
    obs      = {"bull": [], "bear": []}
    n        = len(df)
    avg_body = _avg_body(df)

    for i in range(n - 3, max(n - lookback, 0), -1):
        ob_low  = df["low"].iloc[i]
        ob_high = df["high"].iloc[i]

        # Bullish OB: bearish candle followed by engulfing bullish move
        if df["close"].iloc[i] < df["open"].iloc[i]:
            if i + 1 < n and df["close"].iloc[i + 1] > ob_high:
                future_close = df["close"].iloc[i + 2:]
                if not (future_close <= ob_high).any():          # body-close mitigation check
                    eng_body  = abs(df["close"].iloc[i+1] - df["open"].iloc[i+1])
                    has_fvg   = (i + 2 < n and df["low"].iloc[i+2] > ob_high)
                    displaced = (eng_body > avg_body * 1.5) or has_fvg
                    obs["bull"].append((ob_low, ob_high, displaced))

        # Bearish OB: bullish candle followed by engulfing bearish move
        if df["close"].iloc[i] > df["open"].iloc[i]:
            if i + 1 < n and df["close"].iloc[i + 1] < ob_low:
                future_close = df["close"].iloc[i + 2:]
                if not (future_close >= ob_low).any():           # body-close mitigation check
                    eng_body  = abs(df["close"].iloc[i+1] - df["open"].iloc[i+1])
                    has_fvg   = (i + 2 < n and df["high"].iloc[i+2] < ob_low)
                    displaced = (eng_body > avg_body * 1.5) or has_fvg
                    obs["bear"].append((ob_low, ob_high, displaced))

    return obs


def check_zone(price: float, obs: dict) -> tuple:
    """
    Returns (zone: str, displaced: bool).
      zone     = "Demand" | "Supply" | "None"
      displaced = True if the OB was backed by impulsive displacement.
    """
    for lo, hi, disp in obs["bull"]:
        if lo * 0.999 <= price <= hi * 1.001:
            return "Demand", disp
    for lo, hi, disp in obs["bear"]:
        if lo * 0.999 <= price <= hi * 1.001:
            return "Supply", disp
    return "None", False


# ── 4. Fair Value Gaps (size-weighted) ────────────────────────────────────────

def find_fvg(df, lookback: int = 50) -> dict:
    """
    3-candle imbalance zones (unmitigated only).

    [Upgrade E] Each FVG carries gap_pct = gap_size / mid_price.
    Gaps >= 0.5% of price are considered 'large' and earn a scoring bonus.

    Bullish FVG : candle[i+2].low  > candle[i].high
    Bearish FVG : candle[i+2].high < candle[i].low
    """
    fvgs = {"bull": [], "bear": []}
    n    = len(df)

    for i in range(max(0, n - lookback), n - 2):
        h1 = df["high"].iloc[i];    l1 = df["low"].iloc[i]
        h3 = df["high"].iloc[i+2];  l3 = df["low"].iloc[i+2]

        if l3 > h1:                                              # Bullish FVG
            future    = df.iloc[i + 3:]
            mitigated = not future.empty and (future["low"] <= l3).any()
            if not mitigated:
                mid     = (h1 + l3) / 2
                gap_pct = (l3 - h1) / mid if mid > 0 else 0.0
                fvgs["bull"].append({"low": h1, "high": l3, "gap_pct": gap_pct})

        elif h3 < l1:                                            # Bearish FVG
            future    = df.iloc[i + 3:]
            mitigated = not future.empty and (future["high"] >= h3).any()
            if not mitigated:
                mid     = (h3 + l1) / 2
                gap_pct = (l1 - h3) / mid if mid > 0 else 0.0
                fvgs["bear"].append({"low": h3, "high": l1, "gap_pct": gap_pct})

    return fvgs


def check_fvg_zone(price: float, fvgs: dict) -> tuple:
    """
    Returns (zone: str, gap_pct: float).
      zone    = "Bullish FVG" | "Bearish FVG" | "None"
      gap_pct = relative gap size (threshold: 0.005 = 0.5%)
    """
    for g in fvgs["bull"]:
        if g["low"] * 0.999 <= price <= g["high"] * 1.001:
            return "Bullish FVG", g["gap_pct"]
    for g in fvgs["bear"]:
        if g["low"] * 0.999 <= price <= g["high"] * 1.001:
            return "Bearish FVG", g["gap_pct"]
    return "None", 0.0


# ── 5. Liquidity Sweep ────────────────────────────────────────────────────────

def detect_liquidity_sweep(df, side: str,
                            tolerance: float = 0.002,
                            lookback:  int   = 30) -> tuple:
    """
    Liquidity grab: equal highs/lows pool swept by a wick, price closes back.
    Returns (swept: bool, reason: str).
    """
    n     = len(df)
    start = max(0, n - lookback)

    if side == "Long":
        hist  = df["low"].iloc[start : n - 1].values
        wick  = df["low"].iloc[-1]
        close = df["close"].iloc[-1]
        for i in range(len(hist) - 1):
            for j in range(i + 1, min(i + 8, len(hist))):
                if abs(hist[i] - hist[j]) / (hist[j] + 1e-10) < tolerance:
                    pool = min(hist[i], hist[j])
                    if wick < pool * (1 - tolerance) and close > pool:
                        return True, "Liquidity Sweep (Buy-Side)"

    elif side == "Short":
        hist  = df["high"].iloc[start : n - 1].values
        wick  = df["high"].iloc[-1]
        close = df["close"].iloc[-1]
        for i in range(len(hist) - 1):
            for j in range(i + 1, min(i + 8, len(hist))):
                if abs(hist[i] - hist[j]) / (hist[j] + 1e-10) < tolerance:
                    pool = max(hist[i], hist[j])
                    if wick > pool * (1 + tolerance) and close < pool:
                        return True, "Liquidity Sweep (Sell-Side)"

    return False, ""


# ── 6. Premium / Discount Zone + OTE ─────────────────────────────────────────

def get_premium_discount(df) -> tuple:
    """
    [Upgrade B] Maps current price to last major swing range (0.0 - 1.0).

    Fibonacci reference levels:
      0.000 = swing low     (buy-side liquidity / EQL)
      0.214 = 78.6% retrace from high  -- OTE Long lower bound
      0.382 = 61.8% retrace from high  -- OTE Long upper bound
      0.500 = equilibrium / midpoint
      0.618 = 61.8% retrace from low   -- OTE Short lower bound
      0.786 = 78.6% retrace from low   -- OTE Short upper bound
      1.000 = swing high    (sell-side liquidity / EQH)

    Discount (<0.50): price below equilibrium -- ideal for Long.
    Premium  (>0.50): price above equilibrium -- ideal for Short.
    OTE     : inside 61.8-78.6% retracement band -- highest probability.

    Returns (zone: str, position: float 0-1, in_ote: bool).
    """
    try:
        highs, lows = find_pivots(df)
        if len(highs) < 1 or len(lows) < 1:
            return "Neutral", 0.5, False

        swing_high = float(highs["price"].max())
        swing_low  = float(lows["price"].min())
        curr       = float(df["close"].iloc[-1])

        rng = swing_high - swing_low
        if rng < 1e-10:
            return "Neutral", 0.5, False

        pos    = float(np.clip((curr - swing_low) / rng, 0.0, 1.0))
        zone   = "Discount" if pos < 0.5 else "Premium"
        in_ote = (0.214 <= pos <= 0.382) or (0.618 <= pos <= 0.786)

        return zone, pos, in_ote

    except Exception as e:
        logger.debug(f"get_premium_discount fallback: {e}")
        return "Neutral", 0.5, False


# ── 7. Inducement (IDM) ───────────────────────────────────────────────────────

def detect_inducement(df, side: str, lookback: int = 20) -> tuple:
    """
    [Upgrade D] Inducement: a minor swing level is wicked through then closed
    back on the opposite side -- confirms institutional stop hunt before move.

    Long IDM : wick below minor swing low + close above it.
               Retail shorts stopped out; institutions absorbing sell-side flow.
    Short IDM: wick above minor swing high + close below it.
               Retail longs stopped out; institutions distributing buy-side flow.

    Uses order=3 (minor pivots) within the last `lookback` candles.
    Returns (found: bool, reason: str).
    """
    try:
        n      = len(df)
        sub    = df.iloc[max(0, n - lookback):]
        recent = df.iloc[-5:]

        minor_highs, minor_lows = find_pivots(sub, order=3)

        if side == "Long" and len(minor_lows) >= 1:
            for _, row in minor_lows.iterrows():
                lvl = float(row["price"])
                if lvl <= 0:
                    continue
                swept  = bool((recent["low"]   < lvl * 0.999).any())
                closed = bool((recent["close"] > lvl).any())
                if swept and closed:
                    return True, f"IDM Sweep Low ({lvl:.5g})"

        elif side == "Short" and len(minor_highs) >= 1:
            for _, row in minor_highs.iterrows():
                lvl = float(row["price"])
                if lvl <= 0:
                    continue
                swept  = bool((recent["high"]  > lvl * 1.001).any())
                closed = bool((recent["close"] < lvl).any())
                if swept and closed:
                    return True, f"IDM Sweep High ({lvl:.5g})"

    except Exception as e:
        logger.debug(f"detect_inducement fallback: {e}")

    return False, ""


# ── Main Entry Point ──────────────────────────────────────────────────────────

def analyze_smc(df, side: str):
    """
    Full SMC analysis v2 (10/10).
    Returns (valid: bool, score: int, reasons: list[str]).

    Hard rejects (valid=False) only on structural contradiction:
      - Long at LH / LL market structure
      - Short at HL / HH market structure
      - Long price inside a Supply OB
      - Short price inside a Demand OB
    All other checks add to score; caller controls minimum via min_smc_score.
    """
    score, reasons = 0, []
    curr = df["close"].iloc[-1]

    # 1. Market Structure
    struct = get_market_structure(df)
    if side == "Long":
        if   struct == "HL":         score += 2; reasons.append("Higher Low (HL)")
        elif struct == "HH":         score += 1; reasons.append("HH Breakout")
        elif struct in ("LH", "LL"): return False, 0, [f"Avoid Long at {struct}"]
    elif side == "Short":
        if   struct == "LH":         score += 2; reasons.append("Lower High (LH)")
        elif struct == "LL":         score += 1; reasons.append("LL Breakdown")
        elif struct in ("HL", "HH"): return False, 0, [f"Avoid Short at {struct}"]

    # 2. BOS / CHoCH
    bos, choch = detect_bos_choch(df)
    if side == "Long":
        if bos   == "Bullish BOS":   score += 2; reasons.append("Bullish BOS v")
        if choch == "Bullish CHoCH": score += 1; reasons.append("Bullish CHoCH")
        if bos   == "Bearish BOS":   score -= 1
    elif side == "Short":
        if bos   == "Bearish BOS":   score += 2; reasons.append("Bearish BOS v")
        if choch == "Bearish CHoCH": score += 1; reasons.append("Bearish CHoCH")
        if bos   == "Bullish BOS":   score -= 1

    # 3. Fresh Order Blocks + Displacement bonus
    obs        = find_order_blocks(df)
    zone, disp = check_zone(curr, obs)
    if side == "Long":
        if zone == "Demand":
            score += 2; reasons.append("Fresh Bullish OB")
            if disp: score += 1; reasons.append("OB Displaced v")
        elif zone == "Supply":
            return False, 0, ["Avoid Long into Supply OB"]
    elif side == "Short":
        if zone == "Supply":
            score += 2; reasons.append("Fresh Bearish OB")
            if disp: score += 1; reasons.append("OB Displaced v")
        elif zone == "Demand":
            return False, 0, ["Avoid Short into Demand OB"]

    # 4. Fair Value Gap (size-weighted)
    fvgs              = find_fvg(df)
    fvg_zone, gap_pct = check_fvg_zone(curr, fvgs)
    if side == "Long" and fvg_zone == "Bullish FVG":
        score += 1; reasons.append("Bullish FVG")
        if gap_pct >= 0.005:
            score += 1; reasons.append(f"Large FVG ({gap_pct * 100:.2f}%)")
    if side == "Short" and fvg_zone == "Bearish FVG":
        score += 1; reasons.append("Bearish FVG")
        if gap_pct >= 0.005:
            score += 1; reasons.append(f"Large FVG ({gap_pct * 100:.2f}%)")

    # 5. Liquidity Sweep
    swept, sweep_reason = detect_liquidity_sweep(df, side)
    if swept:
        score += 2; reasons.append(sweep_reason)

    # 6. Premium / Discount Zone + OTE
    pd_zone, pos, in_ote = get_premium_discount(df)
    if side == "Long":
        if in_ote and pd_zone == "Discount":
            score += 2; reasons.append(f"OTE Discount ({pos * 100:.1f}%)")
        elif pd_zone == "Discount":
            score += 1; reasons.append(f"Discount Zone ({pos * 100:.1f}%)")
    elif side == "Short":
        if in_ote and pd_zone == "Premium":
            score += 2; reasons.append(f"OTE Premium ({pos * 100:.1f}%)")
        elif pd_zone == "Premium":
            score += 1; reasons.append(f"Premium Zone ({pos * 100:.1f}%)")

    # 7. Inducement (IDM)
    idm_found, idm_reason = detect_inducement(df, side)
    if idm_found:
        score += 1; reasons.append(idm_reason)

    return True, score, reasons
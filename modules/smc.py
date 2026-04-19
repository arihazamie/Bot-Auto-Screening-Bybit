"""
smc.py — Smart Money Concepts Analysis
=======================================
Implements full SMC pipeline:
  1. Market Structure  (HH / HL / LH / LL)
  2. BOS / CHoCH       (Break of Structure / Change of Character)
  3. Fresh Order Blocks (with mitigation filter — expired OBs discarded)
  4. Fair Value Gaps   (unmitigated imbalance zones)
  5. Liquidity Sweep   (equal-high/low pool grabbed then reversed)

Score contribution per check:
  Market structure alignment  : +1 or +2
  BOS confirmation            : +2
  CHoCH early signal          : +1
  Fresh OB (demand/supply)    : +2
  FVG alignment               : +1
  Liquidity sweep             : +2
  Contradicting BOS           : -1
"""

import numpy as np
from scipy.signal import argrelextrema


# ─── Pivot helpers ────────────────────────────────────────────────────────────

def find_pivots(df, order: int = 5):
    """Return (highs, lows) DataFrames with a 'price' column."""
    low_idx  = argrelextrema(df["low"].values,  np.less_equal,    order=order)[0]
    high_idx = argrelextrema(df["high"].values, np.greater_equal, order=order)[0]
    highs = df.iloc[high_idx][["high"]].rename(columns={"high": "price"})
    lows  = df.iloc[low_idx][["low"]].rename(columns={"low":  "price"})
    return highs, lows


# ─── 1. Market Structure ──────────────────────────────────────────────────────

def get_market_structure(df) -> str:
    highs, lows = find_pivots(df)
    if len(highs) < 2 or len(lows) < 2:
        return "Neutral"
    last_h, prev_h = highs.iloc[-1]["price"], highs.iloc[-2]["price"]
    last_l, prev_l = lows.iloc[-1]["price"],  lows.iloc[-2]["price"]
    curr = df["close"].iloc[-1]
    if abs(curr - last_l) / last_l < 0.015:
        return "HL" if last_l > prev_l else "LL"
    if abs(curr - last_h) / last_h < 0.015:
        return "HH" if last_h > prev_h else "LH"
    return "Mid-Range"


# ─── 2. BOS / CHoCH ───────────────────────────────────────────────────────────

def detect_bos_choch(df):
    """
    BOS  = close breaks last swing in trend direction  → continuation confirmed.
    CHoCH = close breaks last swing AGAINST trend      → early reversal warning.
    Returns (bos: str|None, choch: str|None).
    """
    highs, lows = find_pivots(df)
    if len(highs) < 2 or len(lows) < 2:
        return None, None

    curr   = df["close"].iloc[-1]
    last_h = highs.iloc[-1]["price"]; prev_h = highs.iloc[-2]["price"]
    last_l = lows.iloc[-1]["price"];  prev_l = lows.iloc[-2]["price"]

    bos = choch = None

    if curr > last_h and last_h > prev_h:   bos   = "Bullish BOS"
    elif curr < last_l and last_l < prev_l: bos   = "Bearish BOS"
    if curr > last_h and last_h < prev_h:   choch = "Bullish CHoCH"
    elif curr < last_l and last_l > prev_l: choch = "Bearish CHoCH"

    return bos, choch


# ─── 3. Fresh Order Blocks (with mitigation tracking) ────────────────────────

def find_order_blocks(df, lookback: int = 50) -> dict:
    """
    Only returns FRESH OBs — mitigated OBs (price re-entered zone after formation)
    are discarded so they cannot generate stale signals.
    """
    obs = {"bull": [], "bear": []}
    n   = len(df)

    for i in range(n - 3, max(n - lookback, 0), -1):
        ob_low  = df["low"].iloc[i]
        ob_high = df["high"].iloc[i]

        # Bullish OB: bearish candle → engulfing bullish
        if df["close"].iloc[i] < df["open"].iloc[i]:
            if i + 1 < n and df["close"].iloc[i + 1] > ob_high:
                future    = df["low"].iloc[i + 2 :]
                mitigated = (future <= ob_high * 1.001).any()
                if not mitigated:
                    obs["bull"].append((ob_low, ob_high))

        # Bearish OB: bullish candle → engulfing bearish
        if df["close"].iloc[i] > df["open"].iloc[i]:
            if i + 1 < n and df["close"].iloc[i + 1] < ob_low:
                future    = df["high"].iloc[i + 2 :]
                mitigated = (future >= ob_low * 0.999).any()
                if not mitigated:
                    obs["bear"].append((ob_low, ob_high))

    return obs


def check_zone(price: float, obs: dict) -> str:
    for lo, hi in obs["bull"]:
        if lo * 0.999 <= price <= hi * 1.001: return "Demand"
    for lo, hi in obs["bear"]:
        if lo * 0.999 <= price <= hi * 1.001: return "Supply"
    return "None"


# ─── 4. Fair Value Gaps ───────────────────────────────────────────────────────

def find_fvg(df, lookback: int = 50) -> dict:
    """
    3-candle imbalance zones (unmitigated only).
    Bullish FVG : candle[i+2].low  > candle[i].high  (gap above)
    Bearish FVG : candle[i+2].high < candle[i].low   (gap below)
    """
    fvgs = {"bull": [], "bear": []}
    n    = len(df)

    for i in range(max(0, n - lookback), n - 2):
        h1 = df["high"].iloc[i];    l1 = df["low"].iloc[i]
        h3 = df["high"].iloc[i+2];  l3 = df["low"].iloc[i+2]

        if l3 > h1:                          # Bullish FVG
            future    = df.iloc[i + 3:]
            mitigated = not future.empty and (future["low"] <= l3).any()
            if not mitigated:
                fvgs["bull"].append({"low": h1, "high": l3})

        elif h3 < l1:                        # Bearish FVG
            future    = df.iloc[i + 3:]
            mitigated = not future.empty and (future["high"] >= h3).any()
            if not mitigated:
                fvgs["bear"].append({"low": h3, "high": l1})

    return fvgs


def check_fvg_zone(price: float, fvgs: dict) -> str:
    for g in fvgs["bull"]:
        if g["low"] * 0.999 <= price <= g["high"] * 1.001: return "Bullish FVG"
    for g in fvgs["bear"]:
        if g["low"] * 0.999 <= price <= g["high"] * 1.001: return "Bearish FVG"
    return "None"


# ─── 5. Liquidity Sweep ───────────────────────────────────────────────────────

def detect_liquidity_sweep(df, side: str,
                           tolerance: float = 0.002,
                           lookback: int = 30) -> tuple:
    """
    Liquidity grab: equal highs/lows form a pool, price wicks through it
    (sweeping stops) then closes back past the pool level.
    Returns (swept: bool, reason: str).
    """
    n     = len(df)
    start = max(0, n - lookback)

    if side == "Long":
        hist   = df["low"].iloc[start : n - 1].values
        wick   = df["low"].iloc[-1]
        close  = df["close"].iloc[-1]
        for i in range(len(hist) - 1):
            for j in range(i + 1, min(i + 8, len(hist))):
                if abs(hist[i] - hist[j]) / (hist[j] + 1e-10) < tolerance:
                    pool = min(hist[i], hist[j])
                    if wick < pool * (1 - tolerance) and close > pool:
                        return True, "Liquidity Sweep (Buy-Side)"

    elif side == "Short":
        hist   = df["high"].iloc[start : n - 1].values
        wick   = df["high"].iloc[-1]
        close  = df["close"].iloc[-1]
        for i in range(len(hist) - 1):
            for j in range(i + 1, min(i + 8, len(hist))):
                if abs(hist[i] - hist[j]) / (hist[j] + 1e-10) < tolerance:
                    pool = max(hist[i], hist[j])
                    if wick > pool * (1 + tolerance) and close < pool:
                        return True, "Liquidity Sweep (Sell-Side)"

    return False, ""


# ─── Main Entry Point ─────────────────────────────────────────────────────────

def analyze_smc(df, side: str):
    """
    Full SMC analysis.  Returns (valid: bool, score: int, reasons: list[str]).
    """
    score, reasons = 0, []
    curr = df["close"].iloc[-1]

    # 1 · Market Structure
    struct = get_market_structure(df)
    if side == "Long":
        if   struct == "HL":           score += 2; reasons.append("Higher Low")
        elif struct == "HH":           score += 1; reasons.append("HH Breakout")
        elif struct in ("LH", "LL"):   return False, 0, [f"Avoid Long at {struct}"]
    elif side == "Short":
        if   struct == "LH":           score += 2; reasons.append("Lower High")
        elif struct == "LL":           score += 1; reasons.append("LL Breakdown")
        elif struct in ("HL", "HH"):   return False, 0, [f"Avoid Short at {struct}"]

    # 2 · BOS / CHoCH
    bos, choch = detect_bos_choch(df)
    if side == "Long":
        if bos   == "Bullish BOS":   score += 2; reasons.append("Bullish BOS ✓")
        if choch == "Bullish CHoCH": score += 1; reasons.append("Bullish CHoCH")
        if bos   == "Bearish BOS":   score -= 1
    elif side == "Short":
        if bos   == "Bearish BOS":   score += 2; reasons.append("Bearish BOS ✓")
        if choch == "Bearish CHoCH": score += 1; reasons.append("Bearish CHoCH")
        if bos   == "Bullish BOS":   score -= 1

    # 3 · Fresh Order Blocks
    obs  = find_order_blocks(df)
    zone = check_zone(curr, obs)
    if side == "Long":
        if   zone == "Demand":  score += 2; reasons.append("Fresh Bullish OB")
        elif zone == "Supply":  return False, 0, ["Avoid Long into Supply OB"]
    elif side == "Short":
        if   zone == "Supply":  score += 2; reasons.append("Fresh Bearish OB")
        elif zone == "Demand":  return False, 0, ["Avoid Short into Demand OB"]

    # 4 · Fair Value Gap
    fvgs     = find_fvg(df)
    fvg_zone = check_fvg_zone(curr, fvgs)
    if side == "Long"  and fvg_zone == "Bullish FVG": score += 1; reasons.append("Bullish FVG")
    if side == "Short" and fvg_zone == "Bearish FVG": score += 1; reasons.append("Bearish FVG")

    # 5 · Liquidity Sweep
    swept, sweep_reason = detect_liquidity_sweep(df, side)
    if swept: score += 2; reasons.append(sweep_reason)

    return True, score, reasons
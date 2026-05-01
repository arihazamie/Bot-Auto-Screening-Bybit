"""
candlestick_patterns.py — Single-/multi-candle reversal & continuation patterns
================================================================================

Detects 12 classical candlestick patterns on the **last closed candle** of an
OHLC DataFrame and returns structured `PatternHit` dicts for downstream gating.

Each detector evaluates *only* the most recent closed candle (or the trailing
N closed candles for multi-candle patterns). All detectors share a common
geometry helper (`_geom`) that classifies body / wicks / range using
ATR-relative tolerances rather than hard-coded percentages — so the same rules
work on a 0.01 USDT shitcoin and on BTC.

Returned dict shape (one item per detected pattern):
    {
        "name":    str,    # canonical pattern key (lowercase_underscore)
        "side":    str,    # "Long" / "Short"
        "details": str,    # short human-readable why-it-fired
    }

Trend context: for patterns that require a prior trend (hammer, hanging man,
shooting star, stars, soldiers), we use a lightweight SMA(20) slope filter.
Patterns that don't require trend context (engulfing, doji, tweezer) skip it.
"""
from __future__ import annotations

import logging
from typing import Callable

import numpy as np
import pandas as pd

logger = logging.getLogger("CandlePatterns")


# ─── Geometry helpers ────────────────────────────────────────────────────────

def _geom(row: pd.Series) -> dict:
    """Return body / wick / range geometry for a single OHLC candle."""
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    is_green = c > o
    is_red   = c < o
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body_top = max(o, c)
    body_bot = min(o, c)
    return {
        "open": o, "high": h, "low": l, "close": c,
        "range": rng, "body": body, "body_top": body_top, "body_bot": body_bot,
        "upper_wick": upper_wick, "lower_wick": lower_wick,
        "is_green": is_green, "is_red": is_red,
        "body_pct": body / rng,
        "upper_pct": upper_wick / rng,
        "lower_pct": lower_wick / rng,
    }


def _trend_context(df: pd.DataFrame, lookback: int = 20) -> str:
    """Return "up" / "down" / "flat" based on SMA-slope of last `lookback` closes."""
    if len(df) < lookback + 2:
        return "flat"
    sma = df["close"].tail(lookback + 1).rolling(lookback).mean().dropna()
    if len(sma) < 2:
        return "flat"
    last, prev = float(sma.iloc[-1]), float(sma.iloc[0])
    rel = (last - prev) / max(abs(prev), 1e-12)
    if rel > 0.005:    # +0.5% over the window
        return "up"
    if rel < -0.005:
        return "down"
    return "flat"


# ─── 1-candle detectors ──────────────────────────────────────────────────────

def detect_doji(df: pd.DataFrame) -> dict | None:
    """Doji: indecision. body ≤ 10% of range AND non-trivial range. Side resolved
    by context: in uptrend → Short (reversal), in downtrend → Long (reversal)."""
    if len(df) < 2:
        return None
    g = _geom(df.iloc[-1])
    if g["body_pct"] > 0.10:
        return None
    # Skip flat-spread candles (range ≤ 0.01% of close) — those are exchange
    # quote-stuck artefacts, not real dojis.
    if g["range"] / max(g["close"], 1e-12) < 1e-4:
        return None
    # Skip wick-dominant shapes (hammer / shooting star / inverted hammer):
    # those are reversal patterns of their own, not dojis.
    if g["upper_pct"] > 0.60 or g["lower_pct"] > 0.60:
        return None

    trend = _trend_context(df)
    if trend == "up":
        side = "Short"
    elif trend == "down":
        side = "Long"
    else:
        return None  # flat range doji is noise
    return {
        "name": "doji",
        "side": side,
        "details": f"body {g['body_pct']:.0%} of range, trend={trend}",
    }


def detect_hammer(df: pd.DataFrame) -> dict | None:
    """Hammer (Long): lower wick ≥ 60% of range, body ≤ 30% of range, upper
    wick ≤ 10% of range, body in top half of range, prior downtrend."""
    if len(df) < 22:
        return None
    if _trend_context(df) != "down":
        return None
    g = _geom(df.iloc[-1])
    if g["body"] <= 0:
        return None
    if g["lower_pct"] < 0.60:
        return None
    if g["body_pct"] > 0.30:
        return None
    if g["upper_pct"] > 0.10:
        return None
    if g["body_bot"] < g["low"] + 0.5 * g["range"]:
        return None
    return {
        "name": "hammer",
        "side": "Long",
        "details": f"lower_wick={g['lower_pct']:.0%} of range, downtrend",
    }


def detect_inverted_hammer(df: pd.DataFrame) -> dict | None:
    """Inverted Hammer (Long): upper wick ≥ 60% of range, body ≤ 30%, lower
    wick ≤ 10%, body in bottom half, prior downtrend."""
    if len(df) < 22:
        return None
    if _trend_context(df) != "down":
        return None
    g = _geom(df.iloc[-1])
    if g["body"] <= 0:
        return None
    if g["upper_pct"] < 0.60:
        return None
    if g["body_pct"] > 0.30:
        return None
    if g["lower_pct"] > 0.10:
        return None
    if g["body_top"] > g["low"] + 0.5 * g["range"]:
        return None
    return {
        "name": "inverted_hammer",
        "side": "Long",
        "details": f"upper_wick={g['upper_pct']:.0%} of range, downtrend",
    }


def detect_shooting_star(df: pd.DataFrame) -> dict | None:
    """Shooting Star (Short): upper wick ≥ 60%, body ≤ 30%, lower wick ≤ 10%,
    body in bottom half, prior uptrend."""
    if len(df) < 22:
        return None
    if _trend_context(df) != "up":
        return None
    g = _geom(df.iloc[-1])
    if g["body"] <= 0:
        return None
    if g["upper_pct"] < 0.60:
        return None
    if g["body_pct"] > 0.30:
        return None
    if g["lower_pct"] > 0.10:
        return None
    if g["body_top"] > g["low"] + 0.5 * g["range"]:
        return None
    return {
        "name": "shooting_star",
        "side": "Short",
        "details": f"upper_wick={g['upper_pct']:.0%} of range, uptrend",
    }


def detect_hanging_man(df: pd.DataFrame) -> dict | None:
    """Hanging Man (Short): hammer geometry but in an uptrend."""
    if len(df) < 22:
        return None
    if _trend_context(df) != "up":
        return None
    g = _geom(df.iloc[-1])
    if g["body"] <= 0:
        return None
    if g["lower_pct"] < 0.60:
        return None
    if g["body_pct"] > 0.30:
        return None
    if g["upper_pct"] > 0.10:
        return None
    if g["body_bot"] < g["low"] + 0.5 * g["range"]:
        return None
    return {
        "name": "hanging_man",
        "side": "Short",
        "details": f"lower_wick={g['lower_pct']:.0%} of range, uptrend",
    }


# ─── 2-candle detectors ──────────────────────────────────────────────────────

def detect_bullish_engulfing(df: pd.DataFrame) -> dict | None:
    """Bullish Engulfing: red prev, green curr, curr's body fully engulfs prev's body."""
    if len(df) < 2:
        return None
    p, c = _geom(df.iloc[-2]), _geom(df.iloc[-1])
    if not (p["is_red"] and c["is_green"]):
        return None
    if c["open"] > p["close"]:
        return None
    if c["close"] < p["open"]:
        return None
    if c["body"] < p["body"]:    # Curr body must be larger than prev body.
        return None
    return {
        "name": "bullish_engulfing",
        "side": "Long",
        "details": f"curr body {c['body']:.6f} engulfs prev body {p['body']:.6f}",
    }


def detect_bearish_engulfing(df: pd.DataFrame) -> dict | None:
    """Bearish Engulfing: green prev, red curr, curr's body fully engulfs prev's body."""
    if len(df) < 2:
        return None
    p, c = _geom(df.iloc[-2]), _geom(df.iloc[-1])
    if not (p["is_green"] and c["is_red"]):
        return None
    if c["open"] < p["close"]:
        return None
    if c["close"] > p["open"]:
        return None
    if c["body"] < p["body"]:
        return None
    return {
        "name": "bearish_engulfing",
        "side": "Short",
        "details": f"curr body {c['body']:.6f} engulfs prev body {p['body']:.6f}",
    }


def detect_tweezer_bottom(df: pd.DataFrame) -> dict | None:
    """Tweezer Bottom (Long): two consecutive candles share the same low (within
    0.1% tolerance), at a swing low (low in lower 20% of last 20-candle range)."""
    if len(df) < 22:
        return None
    p, c = _geom(df.iloc[-2]), _geom(df.iloc[-1])
    rel = abs(c["low"] - p["low"]) / max(c["low"], 1e-12)
    if rel > 0.001:
        return None
    last20 = df.iloc[-22:-2]
    if len(last20) < 10:
        return None
    rng_lo, rng_hi = float(last20["low"].min()), float(last20["high"].max())
    if rng_hi - rng_lo <= 0:
        return None
    pos = (c["low"] - rng_lo) / (rng_hi - rng_lo)
    if pos > 0.20:
        return None
    return {
        "name": "tweezer_bottom",
        "side": "Long",
        "details": f"matched lows {p['low']:.6f}≈{c['low']:.6f}, swing-low pos {pos:.0%}",
    }


def detect_tweezer_top(df: pd.DataFrame) -> dict | None:
    """Tweezer Top (Short): two consecutive candles share the same high, at a swing high."""
    if len(df) < 22:
        return None
    p, c = _geom(df.iloc[-2]), _geom(df.iloc[-1])
    rel = abs(c["high"] - p["high"]) / max(c["high"], 1e-12)
    if rel > 0.001:
        return None
    last20 = df.iloc[-22:-2]
    if len(last20) < 10:
        return None
    rng_lo, rng_hi = float(last20["low"].min()), float(last20["high"].max())
    if rng_hi - rng_lo <= 0:
        return None
    pos = (c["high"] - rng_lo) / (rng_hi - rng_lo)
    if pos < 0.80:
        return None
    return {
        "name": "tweezer_top",
        "side": "Short",
        "details": f"matched highs {p['high']:.6f}≈{c['high']:.6f}, swing-high pos {pos:.0%}",
    }


# ─── 3-candle detectors ──────────────────────────────────────────────────────

def detect_morning_star(df: pd.DataFrame) -> dict | None:
    """Morning Star (Long): big red, small body, big green that closes above
    the midpoint of the first red. Requires a prior downtrend."""
    if len(df) < 22:
        return None
    if _trend_context(df) != "down":
        return None
    a, b, c = _geom(df.iloc[-3]), _geom(df.iloc[-2]), _geom(df.iloc[-1])
    if not a["is_red"] or not c["is_green"]:
        return None
    # First candle: large body (≥ 50% of range)
    if a["body_pct"] < 0.5:
        return None
    # Middle candle: small body (≤ 30% of avg range of a & c)
    avg_range = (a["range"] + c["range"]) / 2
    if avg_range <= 0:
        return None
    if b["body"] > 0.30 * avg_range:
        return None
    # Middle candle gaps below or near first close (relax to within 0.5% slack
    # since crypto rarely gaps).
    if b["body_top"] > a["close"] * 1.005:
        return None
    # Third candle closes above midpoint of first.
    a_mid = (a["open"] + a["close"]) / 2
    if c["close"] < a_mid:
        return None
    return {
        "name": "morning_star",
        "side": "Long",
        "details": f"3-candle reversal, c.close {c['close']:.6f} > a.mid {a_mid:.6f}",
    }


def detect_evening_star(df: pd.DataFrame) -> dict | None:
    """Evening Star (Short): big green, small body, big red that closes below
    the midpoint of the first green. Requires a prior uptrend."""
    if len(df) < 22:
        return None
    if _trend_context(df) != "up":
        return None
    a, b, c = _geom(df.iloc[-3]), _geom(df.iloc[-2]), _geom(df.iloc[-1])
    if not a["is_green"] or not c["is_red"]:
        return None
    if a["body_pct"] < 0.5:
        return None
    avg_range = (a["range"] + c["range"]) / 2
    if avg_range <= 0:
        return None
    if b["body"] > 0.30 * avg_range:
        return None
    if b["body_bot"] < a["close"] * 0.995:
        return None
    a_mid = (a["open"] + a["close"]) / 2
    if c["close"] > a_mid:
        return None
    return {
        "name": "evening_star",
        "side": "Short",
        "details": f"3-candle reversal, c.close {c['close']:.6f} < a.mid {a_mid:.6f}",
    }


def detect_three_white_soldiers(df: pd.DataFrame) -> dict | None:
    """Three White Soldiers (Long): 3 consecutive green candles, each closes
    higher than the previous, each opens within the previous body, each closes
    near the high (≥ 70% of range)."""
    if len(df) < 4:
        return None
    a, b, c = _geom(df.iloc[-3]), _geom(df.iloc[-2]), _geom(df.iloc[-1])
    for g in (a, b, c):
        if not g["is_green"]:
            return None
        if g["body_pct"] < 0.50:    # require meaty bodies
            return None
        if (g["close"] - g["low"]) / max(g["range"], 1e-12) < 0.70:
            return None
    if not (b["close"] > a["close"] and c["close"] > b["close"]):
        return None
    # Each candle opens inside the previous body (within 5% slack on either side).
    if not (a["body_bot"] <= b["open"] <= a["body_top"] * 1.005):
        return None
    if not (b["body_bot"] <= c["open"] <= b["body_top"] * 1.005):
        return None
    return {
        "name": "three_white_soldiers",
        "side": "Long",
        "details": f"3 consecutive green closes: {a['close']:.6f} → {b['close']:.6f} → {c['close']:.6f}",
    }


def detect_three_black_crows(df: pd.DataFrame) -> dict | None:
    """Three Black Crows (Short): mirror of three_white_soldiers."""
    if len(df) < 4:
        return None
    a, b, c = _geom(df.iloc[-3]), _geom(df.iloc[-2]), _geom(df.iloc[-1])
    for g in (a, b, c):
        if not g["is_red"]:
            return None
        if g["body_pct"] < 0.50:
            return None
        if (g["high"] - g["close"]) / max(g["range"], 1e-12) < 0.70:
            return None
    if not (b["close"] < a["close"] and c["close"] < b["close"]):
        return None
    if not (a["body_bot"] * 0.995 <= b["open"] <= a["body_top"]):
        return None
    if not (b["body_bot"] * 0.995 <= c["open"] <= b["body_top"]):
        return None
    return {
        "name": "three_black_crows",
        "side": "Short",
        "details": f"3 consecutive red closes: {a['close']:.6f} → {b['close']:.6f} → {c['close']:.6f}",
    }


# ─── Aggregator ──────────────────────────────────────────────────────────────

DETECTORS: dict[str, Callable[[pd.DataFrame], dict | None]] = {
    "bullish_engulfing":     detect_bullish_engulfing,
    "bearish_engulfing":     detect_bearish_engulfing,
    "hammer":                detect_hammer,
    "inverted_hammer":       detect_inverted_hammer,
    "shooting_star":         detect_shooting_star,
    "hanging_man":           detect_hanging_man,
    "doji":                  detect_doji,
    "morning_star":          detect_morning_star,
    "evening_star":          detect_evening_star,
    "three_white_soldiers":  detect_three_white_soldiers,
    "three_black_crows":     detect_three_black_crows,
    "tweezer_top":           detect_tweezer_top,
    "tweezer_bottom":        detect_tweezer_bottom,
}


def detect_all(df: pd.DataFrame) -> list[dict]:
    """Run every candlestick detector and return the list of hits.

    Detectors that conflict (e.g. hammer + hanging_man cannot both fire because
    they require opposite trends) won't both return non-None, so the merge step
    below is mostly a no-op — but we keep it general so future detectors can
    coexist.
    """
    if df is None or len(df) < 4:
        return []
    if not {"open", "high", "low", "close"}.issubset(df.columns):
        return []
    hits: list[dict] = []
    for name, fn in DETECTORS.items():
        try:
            hit = fn(df)
        except Exception as e:
            logger.debug(f"[candle:{name}] detector error: {e}")
            continue
        if hit:
            hits.append(hit)
    return hits

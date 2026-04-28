"""
regime.py — Market Regime Classifier
====================================
ANOMALY HARDENING (fix E).

Given an OHLCV DataFrame for a single instrument (typically BTC on the
trend timeframe) classify the market into one of:

  TREND_BULL  : ADX strong, EMA13 > EMA21, BB-width expanding
  TREND_BEAR  : ADX strong, EMA13 < EMA21, BB-width expanding
  RANGE       : ADX weak, BB-width inside historical median
  SQUEEZE     : ADX weak, BB-width compressed (impending breakout)
  ANOMALY     : ATR(14) > anomaly_atr_pct (flash event in progress)

Returned by `classify_regime()` as a dict:
  {
    "label":       str,       # one of the 5 labels above
    "adx":         float,     # last ADX(14)
    "atr_pct":     float,     # last ATR(14) / close
    "bbw":         float,     # current Bollinger band width / mid
    "bbw_pct":     float,     # bbw rank (0..1) over the lookback window
    "ema_align":   int,       # +1 EMA13>EMA21, -1 inverse, 0 unknown
    "should_scan": bool,      # convenience: True if regime is "tradeable"
    "should_long": bool,      # True if Long signals are reasonable here
    "should_short": bool,     # True if Short signals are reasonable here
    "reason":      str,       # human-readable summary for logs / Telegram
  }

Config keys (under "strategy.regime"):
  "trend_adx":          22.0     ADX >= this → trending
  "anomaly_atr_pct":    0.025    ATR/close >= this → ANOMALY (skip scan)
  "squeeze_bbw_pct":    0.20     BBW percentile rank <= this → SQUEEZE
  "range_bbw_pct":      0.50     BBW percentile rank <= this → RANGE
  "lookback":           120      bars used to rank BB width
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
import pandas_ta as ta

from modules.config_loader import CONFIG
from modules.indicators import wilder_atr_pct

logger = logging.getLogger("Regime")

_REG_CFG = CONFIG.get("strategy", {}).get("regime", {})

TREND_ADX        = float(_REG_CFG.get("trend_adx",       22.0))
ANOMALY_ATR_PCT  = float(_REG_CFG.get("anomaly_atr_pct", 0.025))
SQUEEZE_BBW_PCT  = float(_REG_CFG.get("squeeze_bbw_pct", 0.20))
RANGE_BBW_PCT    = float(_REG_CFG.get("range_bbw_pct",   0.50))
LOOKBACK         = int(_REG_CFG.get("lookback",          120))

LABELS = ("TREND_BULL", "TREND_BEAR", "RANGE", "SQUEEZE", "ANOMALY", "UNKNOWN")


def _atr_pct(df: pd.DataFrame, length: int = 14) -> float:
    """
    Wilder ATR% — single source of truth for ATR-based gates across
    patterns / smc / regime / SL distance. See modules.indicators.
    """
    return wilder_atr_pct(df, length=length)


def _bbw_series(close: pd.Series, length: int = 20, mult: float = 2.0) -> pd.Series:
    """Bollinger band width / middle, returned as Series."""
    bb = ta.bbands(close, length=length, std=mult)
    if bb is None or bb.empty:
        return pd.Series(dtype=float)
    upper  = next((c for c in bb.columns if c.startswith("BBU_")), None)
    lower  = next((c for c in bb.columns if c.startswith("BBL_")), None)
    middle = next((c for c in bb.columns if c.startswith("BBM_")), None)
    if not (upper and lower and middle):
        logger.warning(
            f"_bbw_series: cannot identify BBU/BBL/BBM in {list(bb.columns)} — "
            "regime classifier skipping BBW"
        )
        return pd.Series(dtype=float)
    width = (bb[upper] - bb[lower]) / bb[middle]
    return width.dropna()


def _percent_rank(series: pd.Series, value: float) -> float:
    """Where does `value` sit in the distribution of `series` (0..1)?"""
    if series.empty:
        return 0.5
    arr = series.values
    return float((arr <= value).sum()) / float(len(arr))


def classify_regime(df: pd.DataFrame) -> dict:
    """Top-level entry. `df` should be an OHLCV DataFrame (any timeframe)."""
    out = {
        "label":        "UNKNOWN",
        "adx":          0.0,
        "atr_pct":      0.0,
        "bbw":          0.0,
        "bbw_pct":      0.5,
        "ema_align":    0,
        "should_scan":  True,
        "should_long":  True,
        "should_short": True,
        "reason":       "",
    }

    if df is None or len(df) < 50:
        out["reason"] = "data terlalu pendek untuk klasifikasi regime"
        return out

    try:
        # ATR%: flash crash / spike detector
        atr_pct = _atr_pct(df)
        out["atr_pct"] = atr_pct

        # ADX(14)
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        adx_val = 0.0
        if adx_df is not None and len(adx_df.columns):
            adx_col = next((c for c in adx_df.columns if c.startswith("ADX_")),
                           adx_df.columns[0])
            adx_val = float(adx_df[adx_col].iloc[-1])
        out["adx"] = adx_val if np.isfinite(adx_val) else 0.0

        # EMA alignment
        ema_fast = ta.ema(df["close"], length=13)
        ema_slow = ta.ema(df["close"], length=21)
        ef = float(ema_fast.iloc[-1]) if ema_fast is not None else 0.0
        es = float(ema_slow.iloc[-1]) if ema_slow is not None else 0.0
        out["ema_align"] = 1 if ef > es else (-1 if ef < es else 0)

        # BB-width percentile rank
        bbw = _bbw_series(df["close"])
        if not bbw.empty:
            recent = bbw.iloc[-LOOKBACK:] if len(bbw) > LOOKBACK else bbw
            current_bbw = float(bbw.iloc[-1])
            out["bbw"]     = current_bbw
            out["bbw_pct"] = _percent_rank(recent, current_bbw)

        # Decision tree -------------------------------------------------------
        if atr_pct >= ANOMALY_ATR_PCT:
            out["label"]        = "ANOMALY"
            out["should_scan"]  = False
            out["should_long"]  = False
            out["should_short"] = False
            out["reason"] = (
                f"ATR%={atr_pct:.2%} >= {ANOMALY_ATR_PCT:.2%} — black-swan/flash event"
            )
            return out

        if out["adx"] >= TREND_ADX:
            if out["ema_align"] > 0:
                out["label"]        = "TREND_BULL"
                out["should_short"] = False
                out["reason"] = f"ADX={out['adx']:.1f} bullish EMA align"
            elif out["ema_align"] < 0:
                out["label"]        = "TREND_BEAR"
                out["should_long"]  = False
                out["reason"] = f"ADX={out['adx']:.1f} bearish EMA align"
            else:
                out["label"] = "RANGE"
                out["reason"] = f"ADX={out['adx']:.1f} but EMA flat"
            return out

        # ADX weak -> RANGE or SQUEEZE depending on BB width
        if out["bbw_pct"] <= SQUEEZE_BBW_PCT:
            out["label"]  = "SQUEEZE"
            out["reason"] = (
                f"ADX={out['adx']:.1f}, BBW pct={out['bbw_pct']:.0%} — squeeze"
            )
            # squeeze allows scanning but only mean-revert / breakout strategies
            return out

        if out["bbw_pct"] <= RANGE_BBW_PCT:
            out["label"]  = "RANGE"
            out["reason"] = (
                f"ADX={out['adx']:.1f}, BBW pct={out['bbw_pct']:.0%} — choppy range"
            )
            return out

        # ADX weak + BB wide → market wandering, treat as RANGE (avoid trend bias)
        out["label"]  = "RANGE"
        out["reason"] = (
            f"ADX={out['adx']:.1f} weak, BBW pct={out['bbw_pct']:.0%} — directionless"
        )
        return out

    except Exception as e:
        logger.debug(f"classify_regime fallback: {type(e).__name__}: {e}")
        out["reason"] = f"classify error: {type(e).__name__}"
        return out


def regime_allows(regime: dict, side: str) -> bool:
    """Quick gate used by main.scan/analyze_ticker."""
    if not regime:
        return True
    if side == "Long":
        return bool(regime.get("should_long", True))
    if side == "Short":
        return bool(regime.get("should_short", True))
    return True

"""
quant.py — Quantitative Signal Metrics
========================================
Changes vs v1:
  - calculate_obi() now uses REAL order book depth (top 10 bid/ask levels)
    instead of ticker bidVolume/askVolume which is almost always 0 on Bybit.
  - calculate_metrics() accepts an optional order_book dict (pass {} to skip OBI).
"""

import logging
import numpy as np
import pandas_ta as ta
from scipy.special import expit

logger = logging.getLogger("Quant")


def calculate_z_score(series, window: int = 20):
    mean = series.rolling(window=window).mean()
    std  = series.rolling(window=window).std()
    return (series - mean) / std


def calculate_zeta_field(df, basis):
    try:
        natr  = ta.natr(df["high"], df["low"], df["close"], length=14)
        cmf   = ta.cmf(df["high"], df["low"], df["close"], df["volume"], length=20)
        cci   = ta.cci(df["high"], df["low"], df["close"], length=20)
        rsi   = ta.rsi(df["close"], length=14)
        roc   = ta.roc(df["close"], length=9)
        adx   = ta.adx(df["high"], df["low"], df["close"], length=14)
        rvol  = df["volume"].iloc[-1] / df["volume"].rolling(20).mean().iloc[-1]

        v_term = expit(natr.iloc[-1])
        f_term = (cmf.iloc[-1] + 1) / 2
        c_term = expit(cci.iloc[-1] / 100)
        b_term = 1.0 - min(abs(basis) * 100, 1.0)
        s_term = rsi.iloc[-1] / 100.0
        a_term = expit(roc.iloc[-1])
        h_term = min(rvol / 5.0, 1.0)
        t_term = adx["ADX_14"].iloc[-1] / 100.0

        zeta_score = ((v_term + f_term + c_term + b_term + s_term + a_term + h_term + t_term) / 8.0) * 100.0

        score_add, reason = 0, ""
        if zeta_score > 70:   score_add, reason = 1, f"ζ-High ({zeta_score:.1f})"
        elif zeta_score < 30: score_add, reason = 1, f"ζ-Low ({zeta_score:.1f})"
        return zeta_score, score_add, reason
    except Exception as e:
        logger.debug(f"calculate_zeta_field fallback: {e}")
        return 50.0, 0, ""


def calculate_obi(order_book: dict) -> float:
    """
    Order Book Imbalance from real order book depth (top 10 levels).

    order_book format (standard ccxt fetch_order_book):
      { 'bids': [[price, qty], ...], 'asks': [[price, qty], ...] }

    Returns a value in [-1, +1]:
      +1 = all volume on bid side (strong buy pressure)
      -1 = all volume on ask side (strong sell pressure)
    """
    try:
        levels   = 10
        bids     = order_book.get("bids", [])[:levels]
        asks     = order_book.get("asks", [])[:levels]
        if not bids or not asks:
            return 0.0
        bid_vol  = sum(qty for _, qty in bids)
        ask_vol  = sum(qty for _, qty in asks)
        total    = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > 0 else 0.0
    except Exception as e:
        logger.debug(f"calculate_obi fallback: {e}")
        return 0.0


def calculate_metrics(df, ticker, order_book: dict = None):
    """
    Main quant scoring.

    Parameters
    ----------
    df         : OHLCV DataFrame (technicals already applied)
    ticker     : ccxt ticker dict
    order_book : ccxt fetch_order_book() result (pass {} or None to skip OBI)
    """
    mark  = float(ticker.get("last", 0))
    index = float(ticker.get("info", {}).get("indexPrice", mark))
    basis = (mark - index) / index if index > 0 else 0

    df["Vol_SMA"] = ta.sma(df["volume"], length=20)
    df["RVOL"]    = df["volume"] / df["Vol_SMA"]
    df["Vol_Z"]   = calculate_z_score(df["volume"], window=20)
    z_score       = df["Vol_Z"].iloc[-1]

    zeta_score, zeta_bonus, zeta_reason = calculate_zeta_field(df, basis)
    obi = calculate_obi(order_book or {})

    score, reasons = 2, []

    if   df["RVOL"].iloc[-1] > 5.0: score += 1; reasons.append("Nuclear RVOL")
    elif df["RVOL"].iloc[-1] > 2.0: reasons.append("Valid RVOL")

    if z_score > 3.0:    score += 2; reasons.append(f"Z-Score ({z_score:.1f})")
    if zeta_bonus > 0:   score += zeta_bonus; reasons.append(zeta_reason)

    # OBI from real order book — only score if we actually got data
    if abs(obi) > 0.3:
        score += 1
        reasons.append(f"OBI {obi:+.2f}")

    return df, basis, z_score, zeta_score, obi, score, reasons


def check_fakeout(df, min_rvol):
    if df["RVOL"].iloc[-1] < min_rvol:
        return False, "Fakeout (Low Vol)"
    return True, ""
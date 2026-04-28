"""
technicals.py — base indicator stack used by the pipeline.

CORRECTNESS HARDENING:
  • Stoch RSI column matcher made version-robust. Old code used
    `c.upper().endswith('K_3')` — but pandas_ta's actual column name
    `STOCHRSIk_14_14_3_3` ends with `_3_3`, never `K_3`. The filter
    silently never matched, falling back to positional index. Worked
    by luck, but fragile if pandas_ta column ordering changes.
  • Dead indicators removed: EMA_Fast/EMA_Slow/MACD_h/stoch_rsi_d
    were populated but never read by any downstream consumer.
    Removing them lets `dropna()` keep ~20 more bars (ADX(14) is now
    the longest indicator instead of MACD-signal at 35).
  • get_technicals() now still computes ADX(14) — the gates in
    patterns.py / derivatives.py / smc.py / main.entry_quality
    depend on it.
"""

import pandas_ta as ta
import numpy as np
from scipy.signal import argrelextrema


def _stochrsi_kd_columns(stoch_df) -> tuple[str | None, str | None]:
    """
    Robustly identify the %K and %D columns from a pandas_ta stochrsi result.

    pandas_ta names columns like:
      STOCHRSIk_14_14_3_3   ← %K
      STOCHRSId_14_14_3_3   ← %D
    The differentiator is the lowercase 'k' / 'd' right after 'STOCHRSI'.
    Match on that substring (case-sensitive) so we don't depend on
    column ordering or the trailing parameter suffix.
    """
    if stoch_df is None or stoch_df.empty:
        return None, None
    cols = list(stoch_df.columns)
    k_col = next((c for c in cols if "STOCHRSIk" in c), None)
    d_col = next((c for c in cols if "STOCHRSId" in c), None)
    return k_col, d_col


def detect_divergence(df):
    score = 0
    reasons = []
    if "stoch_rsi_k" not in df.columns:
        return 0, ""
    close = df['close'].values
    k = df['stoch_rsi_k'].values

    high_idx = argrelextrema(close, np.greater, order=3)[0]
    low_idx = argrelextrema(close, np.less, order=3)[0]

    if len(high_idx) >= 2:
        if close[high_idx[-1]] > close[high_idx[-2]] and k[high_idx[-1]] < k[high_idx[-2]]:
            score -= 2; reasons.append("Bear Div")

    if len(low_idx) >= 2:
        if close[low_idx[-1]] < close[low_idx[-2]] and k[low_idx[-1]] > k[low_idx[-2]]:
            score += 2; reasons.append("Bull Div")

    return score, ", ".join(reasons)


def get_technicals(df):
    """
    Populate indicators required by downstream consumers:
      • df['stoch_rsi_k'] — used by detect_divergence()
      • df['adx']         — used by entry_quality, patterns, smc, derivatives

    Other indicators (EMA, MACD, %D) intentionally not populated here:
    they are recomputed on-demand by the consumers that need them
    (e.g. get_btc_bias recomputes EMA13/21+ADX) and pre-computing
    them only causes dropna() to throw away usable bars.
    """
    # Stoch RSI %K — required by detect_divergence.
    try:
        stoch = ta.stochrsi(df['close'], length=14, k=3, d=3)
        k_col, _ = _stochrsi_kd_columns(stoch)
        if k_col is not None:
            df['stoch_rsi_k'] = stoch[k_col]
    except Exception:
        # fail-soft: divergence detector handles missing column
        pass

    # ADX(14) — required by entry_quality_reject_reason, pattern ADX gate,
    # SMC BOS/CHoCH gate, and CVD divergence gate. Previously the column
    # was referenced before being populated, making those gates silent
    # no-ops.
    try:
        adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
        if adx_df is not None and len(adx_df.columns):
            adx_col = next((c for c in adx_df.columns if c.startswith('ADX_')),
                           adx_df.columns[0])
            df['adx'] = adx_df[adx_col]
    except Exception:
        # fail-soft: keep df['adx'] absent rather than crash on edge data
        pass

    # Buat DataFrame baru (tidak mutasi caller) dan reset index agar
    # konsisten di semua modul yang mengasumsikan index 0-based kontinu.
    return df.dropna().reset_index(drop=True)

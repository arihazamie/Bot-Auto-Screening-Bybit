"""
technicals.py — base indicator stack used by the pipeline.

ANOMALY HARDENING (fix A/B/F):
  • get_technicals() now also computes ADX(14). Previous code referenced
    df["adx"] in patterns.py / derivatives.py / main.py:entry_quality but
    nothing ever populated the column → ADX gates were silently no-ops.
"""

import pandas_ta as ta
import numpy as np
from scipy.signal import argrelextrema


def detect_divergence(df):
    score = 0
    reasons = []
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
    df['EMA_Fast'] = ta.ema(df['close'], length=13)
    df['EMA_Slow'] = ta.ema(df['close'], length=21)
    stoch = ta.stochrsi(df['close'], length=14, k=3, d=3)
    if stoch is not None:
        k_cols = [c for c in stoch.columns if c.upper().endswith('K_3')]
        d_cols = [c for c in stoch.columns if c.upper().endswith('D_3')]
        # fallback ke indeks jika nama kolom tidak match
        df['stoch_rsi_k'] = stoch[k_cols[0]] if k_cols else stoch.iloc[:, 0]
        df['stoch_rsi_d'] = stoch[d_cols[0]] if d_cols else stoch.iloc[:, 1]

    macd = ta.macd(df['close'])
    if macd is not None:
        df['MACD_h'] = macd[macd.columns[1]]

    # ✅ ADX(14) — required by entry_quality_reject_reason, pattern ADX gate
    # (fix A) and CVD divergence gate (fix D). Previously the column was
    # referenced but never populated, making the gates silently inert.
    try:
        adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
        if adx_df is not None and len(adx_df.columns):
            adx_col = next((c for c in adx_df.columns if c.startswith('ADX_')),
                           adx_df.columns[0])
            df['adx'] = adx_df[adx_col]
    except Exception:
        # fail-soft: keep df['adx'] absent rather than crash on edge data
        pass

    # ✅ Aman: buat DataFrame baru (tidak mutasi caller) dan reset index agar
    # konsisten di semua modul yang mengasumsikan index 0-based kontinu.
    return df.dropna().reset_index(drop=True)

"""
manual_market_context.py — Build a compact market-context snapshot for one symbol.

Reuses indikator yang sudah ada di repo (technicals, regime, derivatives) supaya
output yang dikirim ke OpenRouter konsisten dengan logika screening utama.

Output (dict) sengaja kecil — supaya prompt LLM ringkas + cost rendah:
  {
    "symbol": "BTCUSDT",
    "tf_entry":     "15m",            # tf yang user konfigurasi
    "tf_trend":     "1h",
    "regime_entry": {"label", "adx", "atr_pct", "ema_align", "reason"},
    "regime_trend": {...},
    "rsi_entry":    float,
    "rsi_trend":    float,
    "ema_align_trend": int,
    "funding_rate":    float,
    "cvd_slope_entry": float,
    "price_slope_entry": float,
    "last_close":   float,
  }

Key-key di atas timeframe-AGNOSTIC supaya konsumen (notifier, advisor) bisa
membaca `tf_entry` / `tf_trend` untuk display label tanpa hardcode '15m'/'1h'.

Tidak pernah memutuskan trade — hanya kumpulkan fakta numerik.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger("ManualContext")


def _safe_last(series, default=0.0) -> float:
    try:
        v = float(series.iloc[-1])
        return v if np.isfinite(v) else default
    except Exception:
        return default


def _to_ccxt_symbol(bybit_sym: str) -> str:
    """Convert 'BTCUSDT' (native Bybit) → 'BTC/USDT:USDT' (ccxt swap)."""
    s = bybit_sym.strip().upper()
    if "/" in s:
        return s if ":" in s else f"{s}:USDT"
    if s.endswith("USDT"):
        return f"{s[:-4]}/USDT:USDT"
    return s


def _slope(values) -> float:
    """Linear regression slope; 0.0 kalau gagal/data terlalu pendek."""
    try:
        if len(values) < 5:
            return 0.0
        from scipy.stats import linregress  # local import → cepat saat module load
        return float(linregress(np.arange(len(values)), np.asarray(values))[0])
    except Exception as e:
        logger.debug(f"_slope fallback: {e}")
        return 0.0


def _rsi_last(close_series, length: int = 14) -> float:
    try:
        import pandas_ta as ta
        rsi = ta.rsi(close_series, length=length)
        return _safe_last(rsi, 50.0)
    except Exception as e:
        logger.debug(f"_rsi_last fallback: {e}")
        return 50.0


def _ema_align(close_series) -> int:
    try:
        import pandas_ta as ta
        ef = ta.ema(close_series, length=13)
        es = ta.ema(close_series, length=21)
        a, b = _safe_last(ef), _safe_last(es)
        return 1 if a > b else (-1 if a < b else 0)
    except Exception:
        return 0


def build_context(
    bybit_symbol: str,
    bybit_client,                   # modules.exchange.BybitClient
    tf_entry: str = "15m",
    tf_trend: str = "1h",
    candles: int = 200,
) -> dict:
    """
    Bangun snapshot konteks market untuk satu posisi. Selalu return dict
    (tidak pernah raise) — kalau data kurang, isi default & kasih flag
    `degraded=True` supaya advisor tahu output kurang reliable.
    """
    sym_ccxt = _to_ccxt_symbol(bybit_symbol)

    out: dict = {
        "symbol":      bybit_symbol,
        "tf_entry":    tf_entry,
        "tf_trend":    tf_trend,
        "degraded":    False,
        "errors":      [],
    }

    # Lazy imports — supaya module ini bisa di-import tanpa pandas_ta tersedia
    # untuk testing pure-logic.
    try:
        from modules.regime import classify_regime
        from modules.technicals import get_technicals
    except Exception as e:
        out["degraded"] = True
        out["errors"].append(f"indicator import: {type(e).__name__}")
        return out

    # ── Fetch entry-TF candles ───────────────────────────────────────────────
    df_entry = _fetch_or_none(bybit_client, sym_ccxt, tf_entry, candles, out)
    df_trend = _fetch_or_none(bybit_client, sym_ccxt, tf_trend, candles, out)

    if df_entry is None or df_trend is None:
        out["degraded"] = True
        return out

    # ── Regime per TF (key timeframe-agnostic; tf-nya tersimpan di tf_entry/tf_trend) ──
    try:
        out["regime_entry"] = _slim_regime(classify_regime(df_entry))
    except Exception as e:
        out["regime_entry"] = {"label": "UNKNOWN", "reason": str(type(e).__name__)}
        out["errors"].append(f"regime_entry: {type(e).__name__}")

    try:
        out["regime_trend"] = _slim_regime(classify_regime(df_trend))
    except Exception as e:
        out["regime_trend"] = {"label": "UNKNOWN", "reason": str(type(e).__name__)}
        out["errors"].append(f"regime_trend: {type(e).__name__}")

    # ── Indicators ────────────────────────────────────────────────────────────
    out["rsi_entry"]       = _rsi_last(df_entry["close"])
    out["rsi_trend"]       = _rsi_last(df_trend["close"])
    out["ema_align_trend"] = _ema_align(df_trend["close"])
    out["last_close"]      = _safe_last(df_entry["close"])

    # Price slope (entry TF, last 30 closes) — gives advisor a numeric trend hint.
    try:
        out["price_slope_entry"] = _slope(df_entry["close"].iloc[-30:].values)
    except Exception:
        out["price_slope_entry"] = 0.0

    # CVD slope (proxy untuk pressure direction). Pakai persamaan sama dengan
    # modules.derivatives, tapi inline biar tidak butuh ticker info.
    try:
        out["cvd_slope_entry"] = _cvd_slope(df_entry, lookback=30)
    except Exception as e:
        out["cvd_slope_entry"] = 0.0
        out["errors"].append(f"cvd: {type(e).__name__}")

    # Funding rate — opsional, butuh ticker. Pakai bybit_client.fetch_ticker.
    out["funding_rate"] = _funding_rate(bybit_client, sym_ccxt)

    return out


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _fetch_or_none(client, sym, tf, limit, out):
    try:
        df = client.fetch_ohlcv(sym, tf, limit=limit)
        if df is None or len(df) < 50:
            out["errors"].append(f"ohlcv {tf}: too short")
            return None
        return df
    except Exception as e:
        out["errors"].append(f"ohlcv {tf}: {type(e).__name__}")
        logger.debug(f"fetch_ohlcv [{sym}/{tf}] failed: {e}")
        return None


def _slim_regime(reg: dict) -> dict:
    """Pertahankan hanya field yang berguna untuk LLM (hemat token)."""
    return {
        "label":     reg.get("label", "UNKNOWN"),
        "adx":       round(float(reg.get("adx", 0.0)), 2),
        "atr_pct":   round(float(reg.get("atr_pct", 0.0)), 5),
        "ema_align": int(reg.get("ema_align", 0)),
        "reason":    reg.get("reason", ""),
    }


def _cvd_slope(df, lookback: int = 30) -> float:
    """CVD = cum(buy_vol - sell_vol) per candle, formula sama dgn derivatives.py."""
    eps      = 1e-12
    hl       = (df["high"] - df["low"]) + eps
    buy      = df["volume"] * (df["close"] - df["low"]) / hl
    sell     = df["volume"] * (df["high"] - df["close"]) / hl
    cvd      = (buy - sell).cumsum()
    return _slope(cvd.iloc[-lookback:].values)


def _funding_rate(client, sym_ccxt) -> float:
    """Pull funding rate dari ticker. 0.0 kalau gagal."""
    try:
        t = client.fetch_ticker(sym_ccxt)
        info = t.get("info", {}) if isinstance(t, dict) else {}
        return float(info.get("fundingRate", 0) or 0)
    except Exception as e:
        logger.debug(f"funding fetch failed [{sym_ccxt}]: {e}")
        return 0.0

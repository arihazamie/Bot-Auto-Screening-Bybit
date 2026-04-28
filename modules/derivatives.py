"""
derivatives.py — Derivative Metrics: Funding Rate Gate, Basis, CVD Divergence

ANOMALY HARDENING (fix C, D):
  • Funding-rate thresholds re-scaled to match the actual Bybit format
    (fundingRate is sent as a fraction PER 8h period, e.g. 0.0001 = 0.01%).
    Previous defaults (0.02, 0.01, 0.005) effectively never triggered.
  • Added defensive auto-scaler that detects accidental percent-format input
    (e.g. user puts 0.05 meaning "5%") and rescales to fraction.
  • Funding kill-switch is now always evaluated; the cool/bonus thresholds
    are only awarded when funding signal is meaningful.
  • CVD divergence now requires:
      - longer slope window (default 30 bars, was 10)
      - confirmed trend strength (ADX >= min) so range chop does not produce
        spurious divergences.

Config keys (under "strategy"):
  "max_funding_long":   0.0008     reject Long  if fundingRate > this
  "min_funding_short": -0.0008     reject Short if fundingRate < this
  "cool_funding_abs":   0.0002     |funding| < this  → bonus "Cool Funding"
  "bonus_funding_min":  0.0003     |funding| > this  → bonus when favoring side
  "cvd_lookback":       30         bars used for CVD/price slope regression
  "cvd_min_adx":        20         require ADX(14) >= this for CVD div to count
"""

import logging
import numpy as np
from scipy.stats import linregress

from modules.config_loader import CONFIG

logger = logging.getLogger("Derivatives")

# ─── Config: threshold funding rate (dapat di-override di config.json) ────────
_STRAT = CONFIG.get("strategy", {})

# Default berskala 1e-4 (Bybit fundingRate is a fraction per 8h period).
MAX_FUNDING_LONG  = float(_STRAT.get("max_funding_long",   0.0008))   # reject Long  if >  this
MIN_FUNDING_SHORT = float(_STRAT.get("min_funding_short", -0.0008))   # reject Short if <  this
COOL_FUNDING_ABS  = float(_STRAT.get("cool_funding_abs",   0.0002))   # |f| < this → "Cool Funding"
BONUS_FUNDING_MIN = float(_STRAT.get("bonus_funding_min",  0.0003))   # |f| > this → bonus arah

# CVD divergence guards (fix D).
CVD_LOOKBACK = int(_STRAT.get("cvd_lookback", 30))
CVD_MIN_ADX  = float(_STRAT.get("cvd_min_adx", 20.0))


def get_slope(series):
    try:
        return linregress(np.arange(len(series)), np.array(series))[0]
    except Exception as e:
        logger.debug(f"get_slope fallback: {e}")
        return 0


def _normalize_funding(raw) -> float:
    """
    Defensive scaler.

    Bybit returns fundingRate as a fraction-per-period (e.g. 0.0001 == 0.01%).
    Some integrations accidentally pass percent-format values (e.g. 0.05 meaning
    5%) which would silently break the gate.  If |raw| > 0.05 we assume it is a
    percent number and rescale by 1/100.  Real funding rarely exceeds ±0.5%/8h
    even in extreme regimes, so 0.05 is a safe heuristic ceiling.
    """
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if abs(val) > 0.05:
        scaled = val / 100.0
        logger.debug(
            f"funding rate auto-scaled: {val:+.6f} → {scaled:+.6f} "
            f"(input looked like percent, expected fraction)"
        )
        return scaled
    return val


def analyze_derivatives(df, ticker, side):
    """
    Analyzes derivative metrics: Funding Rate Gate, CVD Divergence.

    Returns
    -------
    (valid: bool, score: int, reasons: list[str])
      valid=False  → signal di-reject (funding carry terlalu mahal)
      score        → tambahan ke total signal score
      reasons      → deskripsi singkat untuk log / Telegram
    """
    score   = 1
    reasons = []

    # ── 1. Funding Rate Gate (fix C) ─────────────────────────────────────────
    funding = _normalize_funding(ticker.get("info", {}).get("fundingRate", 0))

    # Guard Long: reject jika funding terlalu tinggi → Long bayar carry mahal
    if side == "Long" and funding > MAX_FUNDING_LONG:
        return False, 0, [
            f"Funding Hot Long ({funding:+.4f} > {MAX_FUNDING_LONG:+.4f}) — carry mahal"
        ]

    # Guard Short: reject jika funding terlalu negatif → Short bayar carry mahal
    if side == "Short" and funding < MIN_FUNDING_SHORT:
        return False, 0, [
            f"Funding Hot Short ({funding:+.4f} < {MIN_FUNDING_SHORT:+.4f}) — carry mahal"
        ]

    # Bonus: funding netral — carry cost minimal untuk semua pihak
    if abs(funding) < COOL_FUNDING_ABS:
        score += 1
        reasons.append(f"Cool Funding ({funding:+.4f})")

    # Bonus: funding aktif memihak arah posisi
    elif side == "Short" and funding > BONUS_FUNDING_MIN:
        # Funding positif → Short menerima pembayaran setiap 8 jam → net PnL lebih baik
        score += 1
        reasons.append(f"Funding Favors Short (+{funding:.4f})")

    elif side == "Long" and funding < -BONUS_FUNDING_MIN:
        # Funding negatif → Long menerima pembayaran setiap 8 jam → net PnL lebih baik
        score += 1
        reasons.append(f"Funding Favors Long ({funding:.4f})")

    # ── 2. CVD Calculation ────────────────────────────────────────────────────
    # VWAP-Weighted Delta — formula eksplisit berbasis wick.
    # buy_vol  = volume × (close − low)  / (high − low + ε)
    # sell_vol = volume × (high − close) / (high − low + ε)
    # Epsilon bersifat aditif (bukan clip) — konsisten dengan formula referensi.
    if "CVD" not in df.columns:
        eps          = 1e-12                                          # ε — guard div-by-zero
        hl_range     = df["high"] - df["low"] + eps                  # high − low + ε
        buy_vol      = df["volume"] * (df["close"] - df["low"])  / hl_range   # taker-buy pressure
        sell_vol     = df["volume"] * (df["high"] - df["close"]) / hl_range   # taker-sell pressure
        df["delta"]  = buy_vol - sell_vol
        df["CVD"]    = df["delta"].cumsum()

    # ── 3. CVD Divergence Analysis (fix D) ───────────────────────────────────
    # Range-aware: only count divergence if trend strength (ADX) is meaningful.
    # Otherwise CVD vs price slope mismatch is dominated by mean-reverting noise.
    adx_ok = True
    if "adx" in df.columns:
        try:
            last_adx = float(df["adx"].iloc[-1])
            # NaN-aware: NaN < threshold evaluates to False in Python which
            # would silently let bad-data candles through. Treat non-finite
            # ADX as fail-closed — divergence not counted.
            if not np.isfinite(last_adx):
                adx_ok = False
            else:
                adx_ok = last_adx >= CVD_MIN_ADX
        except Exception:
            adx_ok = True  # genuine read error → fail-open (preserve old behavior)

    lb = max(10, CVD_LOOKBACK)
    if len(df) >= lb and adx_ok:
        p_slope   = get_slope(df["close"].iloc[-lb:])
        cvd_slope = get_slope(df["CVD"].iloc[-lb:])

        # Bearish Divergence: Price naik tapi CVD turun → selling pressure tersembunyi
        if p_slope > 0 and cvd_slope < 0:
            if side == "Short":
                score += 2
                reasons.append("Bear CVD Div")
            elif side == "Long":
                score -= 2

        # Bullish Divergence: Price turun tapi CVD naik → buying pressure tersembunyi
        elif p_slope < 0 and cvd_slope > 0:
            if side == "Long":
                score += 2
                reasons.append("Bull CVD Div")
            elif side == "Short":
                score -= 2

    return True, score, reasons

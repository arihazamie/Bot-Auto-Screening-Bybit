"""
derivatives.py — Derivative Metrics: Funding Rate Gate, Basis, CVD Divergence

FIX #04 — Funding Rate Gate Simetris (Long & Short):
  • Long  di-reject jika fundingRate >  MAX_FUNDING_LONG  (default +0.02)
    → Long membayar funding mahal, carry cost tinggi
  • Short di-reject jika fundingRate <  MIN_FUNDING_SHORT (default -0.02)
    → Short membayar funding mahal saat funding sangat negatif
  • Bonus score jika funding memihak arah posisi:
    - Short +1 saat funding positif (Short menerima funding setiap 8 jam)
    - Long  +1 saat funding negatif (Long menerima funding setiap 8 jam)

Config override di bagian "strategy" dalam config.json:
  "max_funding_long":  0.02    ← reject Long  jika fundingRate > ini
  "min_funding_short": -0.02   ← reject Short jika fundingRate < ini
  "cool_funding_abs":  0.01    ← abs(funding) < ini → bonus "Cool Funding"
  "bonus_funding_min": 0.005   ← |funding| > ini → bonus arah
"""

import logging
import numpy as np
from scipy.stats import linregress

from modules.config_loader import CONFIG

logger = logging.getLogger("Derivatives")

# ─── Config: threshold funding rate (dapat di-override di config.json) ────────
_STRAT = CONFIG.get("strategy", {})

MAX_FUNDING_LONG  = float(_STRAT.get("max_funding_long",   0.02))   # reject Long  jika >  ini
MIN_FUNDING_SHORT = float(_STRAT.get("min_funding_short", -0.02))   # reject Short jika <  ini
COOL_FUNDING_ABS  = float(_STRAT.get("cool_funding_abs",   0.01))   # |f| < ini → "Cool Funding"
BONUS_FUNDING_MIN = float(_STRAT.get("bonus_funding_min",  0.005))  # |f| > ini → bonus arah


def get_slope(series):
    try:
        return linregress(np.arange(len(series)), np.array(series))[0]
    except Exception as e:
        logger.debug(f"get_slope fallback: {e}")
        return 0


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

    # ── 1. Funding Rate Gate (FIX #04) ───────────────────────────────────────
    funding = float(ticker.get("info", {}).get("fundingRate", 0))

    # Guard Long: reject jika funding terlalu tinggi → Long bayar carry mahal
    if side == "Long" and funding > MAX_FUNDING_LONG:
        return False, 0, [
            f"Funding Hot Long (>{MAX_FUNDING_LONG:.4g}) — carry mahal"
        ]

    # Guard Short (FIX #04 NEW): reject jika funding terlalu negatif → Short bayar carry mahal
    if side == "Short" and funding < MIN_FUNDING_SHORT:
        return False, 0, [
            f"Funding Hot Short (<{MIN_FUNDING_SHORT:.4g}) — carry mahal"
        ]

    # Bonus: funding netral — carry cost minimal untuk semua pihak
    if abs(funding) < COOL_FUNDING_ABS:
        score += 1
        reasons.append("Cool Funding")

    # Bonus: funding aktif memihak arah posisi (FIX #04 NEW)
    elif side == "Short" and funding > BONUS_FUNDING_MIN:
        # Funding positif → Short menerima pembayaran setiap 8 jam → net PnL lebih baik
        score += 1
        reasons.append(f"Funding Favors Short (+{funding:.4f})")

    elif side == "Long" and funding < -BONUS_FUNDING_MIN:
        # Funding negatif → Long menerima pembayaran setiap 8 jam → net PnL lebih baik
        score += 1
        reasons.append(f"Funding Favors Long ({funding:.4f})")

    # ── 2. CVD Calculation (Defensive Fix) ───────────────────────────────────
    if "CVD" not in df.columns:
        df["delta"] = np.where(df["close"] > df["open"], df["volume"], -df["volume"])
        df["CVD"]   = df["delta"].cumsum()

    # ── 3. CVD Divergence Analysis (Price Slope vs CVD Slope) ────────────────
    p_slope   = get_slope(df["close"].iloc[-10:])
    cvd_slope = get_slope(df["CVD"].iloc[-10:])

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
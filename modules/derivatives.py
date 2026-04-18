import logging
import numpy as np
from scipy.stats import linregress

logger = logging.getLogger("Derivatives")

def get_slope(series):
    try:
        return linregress(np.arange(len(series)), np.array(series))[0]
    except Exception as e:
        logger.debug(f"get_slope fallback: {e}")
        return 0

def analyze_derivatives(df, ticker, side):
    """
    Analyzes derivative metrics (Funding, Basis, CVD Divergence).
    """
    score = 1
    reasons = []

    # 1. Funding Rate Check
    funding = float(ticker.get('info', {}).get('fundingRate', 0))
    if side == "Long" and funding > 0.02:
        return False, 0, ["Funding Hot (>0.02%)"]

    if abs(funding) < 0.01:
        score += 1
        reasons.append("Cool Funding")

    # 2. Basis Calculation
    mark = float(ticker.get('last', 0))
    index = float(ticker.get('info', {}).get('indexPrice', mark))

    # 3. CVD Calculation (Defensive Fix)
    if 'CVD' not in df.columns:
        df['delta'] = np.where(df['close'] > df['open'], df['volume'], -df['volume'])
        df['CVD'] = df['delta'].cumsum()

    # 4. Divergence Analysis (Price Slope vs CVD Slope)
    p_slope   = get_slope(df['close'].iloc[-10:])
    cvd_slope = get_slope(df['CVD'].iloc[-10:])

    # Bearish Divergence: Price Rising, CVD Falling
    if p_slope > 0 and cvd_slope < 0:
        if side == "Short":
            score += 2
            reasons.append("Bear CVD Div")
        elif side == "Long":
            score -= 2

    # Bullish Divergence: Price Falling, CVD Rising
    elif p_slope < 0 and cvd_slope > 0:
        if side == "Long":
            score += 2
            reasons.append("Bull CVD Div")
        elif side == "Short":
            score -= 2

    return True, score, reasons
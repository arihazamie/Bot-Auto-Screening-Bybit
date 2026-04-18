import numpy as np
from scipy.stats import linregress

def get_slope(series):
    try: return linregress(np.arange(len(series)), np.array(series))[0]
    except: return 0

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
    # If 'CVD' is missing, we calculate it right here.
    if 'CVD' not in df.columns:
        df['delta'] = np.where(df['close'] > df['open'], df['volume'], -df['volume'])
        df['CVD'] = df['delta'].cumsum()

    # 4. Divergence Analysis (Price Slope vs CVD Slope)
    # Look at the last 10 candles
    p_slope = get_slope(df['close'].iloc[-10:])
    cvd_slope = get_slope(df['CVD'].iloc[-10:])
    
    # Bearish Divergence: Price Rising, CVD Falling (Sellers absorbing)
    if p_slope > 0 and cvd_slope < 0:
        if side == "Short":
            score += 2
            reasons.append("Bear CVD Div")
        elif side == "Long":
            score -= 2 # Penalty for longing into selling pressure

    # Bullish Divergence: Price Falling, CVD Rising (Buyers absorbing)
    elif p_slope < 0 and cvd_slope > 0:
        if side == "Long":
            score += 2
            reasons.append("Bull CVD Div")
        elif side == "Short":
            score -= 2 # Penalty for shorting into buying pressure

    return True, score, reasons
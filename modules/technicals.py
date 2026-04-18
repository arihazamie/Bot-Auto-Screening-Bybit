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
        df['stoch_rsi_k'] = stoch[stoch.columns[0]]
        df['stoch_rsi_d'] = stoch[stoch.columns[1]]
    
    macd = ta.macd(df['close'])
    if macd is not None:
        df['MACD_h'] = macd[macd.columns[1]]
        
    df.dropna(inplace=True)
    return df
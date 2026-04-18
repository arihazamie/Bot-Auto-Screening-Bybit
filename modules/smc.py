import pandas as pd
import numpy as np
from scipy.signal import argrelextrema

def find_pivots(df, order=5):
    df['min_local'] = df.iloc[argrelextrema(df.low.values, np.less_equal, order=order)[0]]['low']
    df['max_local'] = df.iloc[argrelextrema(df.high.values, np.greater_equal, order=order)[0]]['high']
    highs = df[df['max_local'].notna()][['max_local']].rename(columns={'max_local': 'price'})
    lows = df[df['min_local'].notna()][['min_local']].rename(columns={'min_local': 'price'})
    return highs, lows

def get_market_structure(df):
    highs, lows = find_pivots(df)
    if len(highs) < 2 or len(lows) < 2: return "Neutral"
    
    last_h = highs.iloc[-1]['price']
    last_l = lows.iloc[-1]['price']
    curr = df['close'].iloc[-1]
    
    # Simple proximity check (1.5%)
    if abs(curr - last_l)/last_l < 0.015: return "HL" if last_l > lows.iloc[-2]['price'] else "LL"
    if abs(curr - last_h)/last_h < 0.015: return "HH" if last_h > highs.iloc[-2]['price'] else "LH"
    return "Mid-Range"

def find_order_blocks(df):
    obs = {'bull': [], 'bear': []}
    for i in range(len(df)-3, len(df)-50, -1):
        if df['close'].iloc[i] < df['open'].iloc[i]: # Red
            if df['close'].iloc[i+1] > df['high'].iloc[i]: # Engulf
                obs['bull'].append((df['low'].iloc[i], df['high'].iloc[i]))
        if df['close'].iloc[i] > df['open'].iloc[i]: # Green
            if df['close'].iloc[i+1] < df['low'].iloc[i]: # Engulf
                obs['bear'].append((df['low'].iloc[i], df['high'].iloc[i]))
    return obs

def check_zone(price, obs):
    for l, h in obs['bull']:
        if l*0.999 <= price <= h*1.001: return "Demand"
    for l, h in obs['bear']:
        if l*0.999 <= price <= h*1.001: return "Supply"
    return "None"

def analyze_smc(df, side):
    score = 0
    reasons = []
    curr = df['close'].iloc[-1]
    
    # 1. Structure
    struct = get_market_structure(df)
    if side == "Long":
        if struct == "HL": score += 2; reasons.append("Higher Low")
        elif struct in ["HH", "LL"]: return False, 0, [f"Avoid Long at {struct}"]
    if side == "Short":
        if struct == "LH": score += 2; reasons.append("Lower High")
        elif struct in ["HH", "LL"]: return False, 0, [f"Avoid Short at {struct}"]

    # 2. Zones
    obs = find_order_blocks(df)
    zone = check_zone(curr, obs)
    if side == "Long":
        if zone == "Demand": score += 2; reasons.append("In Bullish OB")
        elif zone == "Supply": return False, 0, ["Avoid Long into Supply"]
    if side == "Short":
        if zone == "Supply": score += 2; reasons.append("In Bearish OB")
        elif zone == "Demand": return False, 0, ["Avoid Short into Demand"]
        
    return True, score, reasons
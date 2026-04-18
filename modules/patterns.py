import numpy as np
from scipy.signal import argrelextrema
from scipy.stats import linregress
from modules.config_loader import CONFIG

def get_slope(values):
    try: return linregress(np.arange(len(values)), values)[0]
    except: return 0.0

def check_alignment(values):
    if len(values) < 2: return False
    tol = CONFIG['patterns'].get('tolerance', 0.015)
    avg = np.mean(values)
    return all(abs(v - avg) / avg < tol for v in values)

def find_pattern(df):
    if len(df) < 50: return None
    df_idx = df.reset_index(drop=True)
    n = 3
    df_idx['min_local'] = df_idx.iloc[argrelextrema(df_idx.low.values, np.less_equal, order=n)[0]]['low']
    df_idx['max_local'] = df_idx.iloc[argrelextrema(df_idx.high.values, np.greater_equal, order=n)[0]]['high']
    peaks = df_idx[df_idx['max_local'].notnull()]['max_local'].values
    valleys = df_idx[df_idx['min_local'].notnull()]['min_local'].values
    if len(peaks) < 3 or len(valleys) < 3: return None
    
    enabled = CONFIG['patterns']
    s_high, s_low = get_slope(peaks[-4:]), get_slope(valleys[-4:])
    
    if enabled.get('ascending_triangle') and abs(s_high) < 0.0005 and s_low > 0.0002: return 'ascending_triangle'
    if enabled.get('descending_triangle') and abs(s_low) < 0.0005 and s_high < -0.0002: return 'descending_triangle'
    if enabled.get('double_bottom') and check_alignment(valleys[-2:]): return 'double_bottom'
    if enabled.get('double_top') and check_alignment(peaks[-2:]): return 'double_top'
    if enabled.get('bull_flag') and -0.002 < s_high < -0.0002 and -0.002 < s_low < -0.0002: return 'bull_flag'
    if enabled.get('bullish_rectangle') and abs(s_high) < 0.0005 and abs(s_low) < 0.0005: return 'bullish_rectangle'
    return None
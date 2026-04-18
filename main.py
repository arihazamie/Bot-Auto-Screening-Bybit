import ccxt
import time
import schedule
import random
import os
import pandas as pd
import pandas_ta as ta
import numpy as np
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from modules.config_loader import CONFIG
from modules.database import init_db, get_active_signals, save_signal_to_db
from modules.technicals import get_technicals, detect_divergence
from modules.quant import calculate_metrics, check_fakeout
from modules.derivatives import analyze_derivatives
from modules.smc import analyze_smc
from modules.patterns import find_pattern
from modules.telegram_bot import send_alert, run_fast_update, send_scan_completion
from modules.watchlist import refresh_watchlist, get_watchlist, get_watchlist_info

# ─────────────────────────────────────────────
# 🔧 MODE CHECK
# ─────────────────────────────────────────────
AUTO_TRADE_ENABLED = CONFIG.get('auto_trade', False)

if AUTO_TRADE_ENABLED:
    print("🤖 Mode: AUTO TRADE — Sinyal akan dikirim & order otomatis dieksekusi di Bybit.")
else:
    print("📡 Mode: SIGNAL ONLY — Sinyal akan dikirim via Telegram, tidak ada order yang dibuka.")

exchange = ccxt.bybit({
    'apiKey': CONFIG['api']['bybit_key'],
    'secret': CONFIG['api']['bybit_secret'],
    'options': {'defaultType': 'swap'}
})

# ─────────────────────────────────────────────
# ⏱️ MULTI-TIMEFRAME CONFIG
#   ENTRY_TF  → 15m  (entry signal analysis)
#   TREND_TF  → 1h   (trend confirmation filter)
# ─────────────────────────────────────────────
ENTRY_TF = CONFIG['system'].get('entry_timeframe', '15m')
TREND_TF  = CONFIG['system'].get('trend_timeframe', '1h')

print(f"📐 Timeframes — Entry: {ENTRY_TF} | Trend Confirmation: {TREND_TF}")


def get_btc_bias():
    """Global BTC directional bias using the trend confirmation timeframe (1h)."""
    try:
        bars = exchange.fetch_ohlcv('BTC/USDT', TREND_TF, limit=100)
        if not bars:
            return "Sideways"
        df = pd.DataFrame(bars, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        df['ema13'] = ta.ema(df['c'], length=13)
        df['ema21'] = ta.ema(df['c'], length=21)
        curr = df.iloc[-1]
        return "Bullish" if curr['ema13'] > curr['ema21'] else "Bearish"
    except:
        return "Sideways"


def get_symbol_trend(symbol):
    """
    Per-symbol trend confirmation on the 1h timeframe (TREND_TF).
    Returns 'Bullish', 'Bearish', or 'Sideways'.

    This is the higher-timeframe filter: the 15m entry signal must align
    with the prevailing 1h trend direction.
    """
    try:
        bars = exchange.fetch_ohlcv(symbol, TREND_TF, limit=100)
        if not bars or len(bars) < 50:
            return "Sideways"
        df = pd.DataFrame(bars, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        df['ema13'] = ta.ema(df['c'], length=13)
        df['ema21'] = ta.ema(df['c'], length=21)
        curr = df.iloc[-1]
        if curr['ema13'] > curr['ema21']:
            return "Bullish"
        elif curr['ema13'] < curr['ema21']:
            return "Bearish"
        return "Sideways"
    except:
        return "Sideways"


def calculate_rr(entry, sl, tp3):
    if entry <= 0 or sl <= 0 or tp3 <= 0:
        return 0.0
    risk = abs(entry - sl)
    return round(abs(tp3 - entry) / risk, 2) if risk > 0 else 0.0


def analyze_ticker(symbol, btc_bias, active_signals, counters):
    """
    Multi-Timeframe analysis per symbol:
      Step 1 — Duplicate check (keyed to ENTRY_TF)
      Step 2 — 1h trend confirmation via get_symbol_trend()
      Step 3 — 15m entry signal: technicals, pattern, SMC, quant, deriv
      Step 4 — MTF alignment check: 15m signal must agree with 1h trend
      Step 5 — BTC global bias filter
      Step 6 — Setup calculation and R:R filter
    """

    # 1. DUPLICATE CHECK
    if (symbol, ENTRY_TF) in active_signals:
        counters['duplicate'] += 1
        return None

    try:
        ticker_info = exchange.fetch_ticker(symbol)
        if "ST" in ticker_info.get('info', {}).get('symbol', ''):
            return None

        symbol_trend = get_symbol_trend(symbol)

        min_candles = CONFIG['system'].get('min_candles_analysis', 150)
        bars = exchange.fetch_ohlcv(symbol, ENTRY_TF, limit=min_candles + 50)
        if not bars or len(bars) < min_candles:
            counters['no_candles'] += 1
            return None

        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        df = get_technicals(df)
        pattern = find_pattern(df)
        if not pattern:
            counters['no_pattern'] += 1
            return None

        side = CONFIG['pattern_signals'].get(pattern)

        # MTF check
        if symbol_trend == "Bearish" and side == "Long":
            counters['mtf_conflict'] += 1
            return None
        if symbol_trend == "Bullish" and side == "Short":
            counters['mtf_conflict'] += 1
            return None

        # BTC bias filter
        if "Bearish" in btc_bias and side == "Long":
            counters['btc_filter'] += 1
            return None
        if "Bullish" in btc_bias and side == "Short":
            counters['btc_filter'] += 1
            return None

        # SMC
        valid_smc, smc_score, smc_reasons = analyze_smc(df, side)
        if not valid_smc or smc_score < CONFIG['strategy'].get('min_smc_score', 0):
            counters['smc_fail'] += 1
            return None

        # Quant & Deriv
        df, basis, z_score, zeta_score, obi, quant_score, quant_reasons = calculate_metrics(df, ticker_info)
        valid_deriv, deriv_score, deriv_reasons = analyze_derivatives(df, ticker_info, side)
        if not valid_deriv:
            counters['deriv_fail'] += 1
            return None
        if deriv_score < CONFIG['strategy'].get('min_deriv_score', 0):
            counters['deriv_fail'] += 1
            return None

        # Score
        div_score, div_msg = detect_divergence(df)
        tech_score = 3 + div_score
        tech_reasons = [f"Pattern: {pattern}", div_msg] + smc_reasons
        total_score = tech_score + smc_score + quant_score + deriv_score

        valid_fo, fo_msg = check_fakeout(df, CONFIG['indicators']['min_rvol'])
        if not valid_fo:
            counters['low_rvol'] += 1
            return None

        if tech_score < CONFIG['strategy']['min_tech_score']:
            counters['low_tech_score'] += 1
            return None

        # ── 9. Setup Calculation ─────────────────────────────────────────
        s = CONFIG['setup']
        swing_high = df['high'].iloc[-50:].max()
        swing_low  = df['low'].iloc[-50:].min()
        rng = swing_high - swing_low

        if side == 'Long':
            entry = (swing_high - (rng * s['fib_entry_start']) + swing_high - (rng * s['fib_entry_end'])) / 2
            sl    = swing_low  - (rng * s['fib_sl'])
            tp1, tp2, tp3 = swing_low + rng, swing_low + (rng * 1.618), swing_low + (rng * 2.618)
        else:
            entry = (swing_low + (rng * s['fib_entry_start']) + swing_low + (rng * s['fib_entry_end'])) / 2
            sl    = swing_high + (rng * s['fib_sl'])
            tp1, tp2, tp3 = swing_high - rng, swing_high - (rng * 1.618), swing_high - (rng * 2.618)

        rr = calculate_rr(entry, sl, tp3)
        if rr < CONFIG['strategy'].get('risk_reward_min', 2.0):
            counters['low_rr'] += 1
            return None

        df['funding'] = float(ticker_info.get('info', {}).get('fundingRate', 0))

        # ── 10. Return Result ─────────────────────────────────────────────
        return {
            "Symbol": symbol,
            "Side": side,
            "Timeframe": ENTRY_TF,
            "Trend_TF": TREND_TF,
            "Symbol_Trend": symbol_trend,
            "Pattern": pattern,
            "Entry": float(entry), "SL": float(sl),
            "TP1": float(tp1), "TP2": float(tp2), "TP3": float(tp3), "RR": float(rr),
            "Tech_Score": int(tech_score), "Quant_Score": int(quant_score),
            "Deriv_Score": int(deriv_score), "SMC_Score": int(smc_score),
            "Basis": float(basis), "Z_Score": float(z_score),
            "Zeta_Score": float(zeta_score), "OBI": float(obi),
            "BTC_Bias": btc_bias, "Reason": pattern,
            "Tech_Reasons": ", ".join(tech_reasons),
            "Quant_Reasons": ", ".join(quant_reasons),
            "SMC_Reasons": ", ".join([r for r in smc_reasons if r]),
            "Deriv_Reasons": ", ".join(deriv_reasons),
            "df": df
        }

    except:
        return None


def scan():
    start_time = time.time()
    mode_label = "AUTO TRADE 🤖" if AUTO_TRADE_ENABLED else "SIGNAL ONLY 📡"
    print(f"\n[{pd.Timestamp.now()}] 🔭 Scanning... Mode: {mode_label} | Env: {os.getenv('BOT_ENV', 'PROD')}")
    print(f"📐 Entry TF: {ENTRY_TF} | Trend Confirmation TF: {TREND_TF}")

    btc_bias = get_btc_bias()
    print(f"📊 BTC Bias ({TREND_TF}): {btc_bias}")

    active_signals = get_active_signals()
    print(f"🛡️ Active Signals Ignored: {len(active_signals)}")

    signal_count = 0

    try:
        # ── Ambil pair dari watchlist, fallback ke semua pair jika belum ada ──
        syms = get_watchlist()

        if syms:
            info = get_watchlist_info()
            print(f"📋 Watchlist: {len(syms)} pairs (updated: {info.get('updated_at', '?')[:16]})")
        else:
            print("⚠️  Watchlist belum ada, fetch semua pair dari Bybit sebagai fallback...")
            mkts = exchange.load_markets()
            STABLECOINS = {
                'USDC', 'USDT', 'DAI', 'FDUSD', 'USDD', 'USDE',
                'TUSD', 'BUSD', 'PYUSD', 'USDS', 'EUR', 'USD'
            }
            syms = [
                s for s in mkts
                if mkts[s].get('swap')
                and mkts[s]['quote'] == 'USDT'
                and mkts[s].get('active')
                and mkts[s]['base'] not in STABLECOINS
            ]

        random.shuffle(syms)
        print(f"🔍 Scanning {len(syms)} pairs...")

        # ── Debug counters ──
        from collections import defaultdict
        counters = defaultdict(int)

        # ── Single-pass: analyze each symbol with MTF (15m entry / 1h trend) ──
        with ThreadPoolExecutor(max_workers=CONFIG['system']['max_threads']) as ex:
            futures = [ex.submit(analyze_ticker, s, btc_bias, active_signals, counters) for s in syms]
            for f in as_completed(futures):
                res = f.result()
                if res:
                    success = send_alert(res, auto_trade=AUTO_TRADE_ENABLED)
                    if success:
                        signal_count += 1
                        if AUTO_TRADE_ENABLED:
                            save_signal_to_db(res)
                        else:
                            print(f"   📡 Signal Only: {res['Symbol']} {res['Side']} [{res['Timeframe']}]")

        # ── Print filter breakdown ──
        total_filtered = sum(counters.values())
        if total_filtered > 0:
            print(f"\n📊 Filter Breakdown ({total_filtered} pairs filtered):")
            labels = {
                'no_pattern':    'No pattern detected',
                'mtf_conflict':  'MTF conflict (1h vs 15m)',
                'btc_filter':    'BTC bias conflict',
                'smc_fail':      'SMC score fail',
                'deriv_fail':    'Deriv score fail',
                'low_rvol':      f"Low RVOL (< {CONFIG['indicators']['min_rvol']}x)",
                'low_tech_score':f"Low tech score (< {CONFIG['strategy']['min_tech_score']})",
                'low_rr':        f"Low R:R (< {CONFIG['strategy'].get('risk_reward_min', 2.0)})",
                'no_candles':    'Insufficient candles',
                'duplicate':     'Duplicate signal',
            }
            for key, label in labels.items():
                if counters[key] > 0:
                    bar = '█' * min(counters[key], 30)
                    print(f"   {label:<35} {counters[key]:>4}  {bar}")

    except Exception as e:
        print(f"Scan Error: {e}")

    finally:
        duration = time.time() - start_time
        print(f"\n✅ Scan Finished in {duration:.2f}s. Signals: {signal_count} | Mode: {mode_label}")
        send_scan_completion(signal_count, duration, btc_bias, auto_trade=AUTO_TRADE_ENABLED)


def refresh_daily_watchlist():
    """Refresh watchlist setiap hari jam 7 pagi."""
    print(f"\n[{pd.Timestamp.now()}] 🔄 Daily watchlist refresh starting...")
    symbols = refresh_watchlist(exchange, top_n=CONFIG['system'].get('watchlist_top_n', 100))
    if symbols:
        print(f"✅ Watchlist refreshed: {len(symbols)} pairs siap untuk scan berikutnya.")
    else:
        print("⚠️  Watchlist refresh gagal, scan berikutnya akan pakai cache lama atau fallback.")


if __name__ == "__main__":
    init_db()

    # ── Fetch watchlist saat startup jika belum ada ──
    if not get_watchlist():
        print("📋 Watchlist belum ada — fetch sekarang...")
        refresh_watchlist(exchange, top_n=CONFIG['system'].get('watchlist_top_n', 100))

    scan()

    schedule.every(CONFIG['system']['check_interval_hours']).hours.do(scan)
    schedule.every(1).minutes.do(run_fast_update)
    schedule.every().day.at("07:00").do(refresh_daily_watchlist)

    print("🚀 Bot Started.")
    print(f"🕖 Watchlist akan direfresh otomatis setiap hari jam 07:00.")
    while True:
        schedule.run_pending()
        time.sleep(1)
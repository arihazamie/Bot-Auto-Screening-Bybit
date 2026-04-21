"""
╔══════════════════════════════════════════════════════════════════════╗
║         BACKTEST ENGINE — Bot Auto Screening Bybit                  ║
║                                                                     ║
║  Semua strategy di-route ke modul asli project:                     ║
║    modules/smc.py          → analyze_smc()                          ║
║    modules/patterns.py     → find_pattern()                         ║
║    modules/technicals.py   → get_technicals(), detect_divergence()  ║
║    modules/quant.py        → calculate_metrics(), check_fakeout()   ║
║    modules/derivatives.py  → analyze_derivatives()                  ║
║    modules/config_loader.py→ CONFIG (dari config.json)              ║
║                                                                     ║
║  Dataset : dataset/BTCUSDT_15m.csv  (entry timeframe)              ║
║             dataset/BTCUSDT_1h.csv  (trend timeframe)              ║
╚══════════════════════════════════════════════════════════════════════╝

Cara pakai:
    cd Bot-Auto-Screening-Bybit-backtest
    python backtest.py
"""

import warnings
warnings.filterwarnings("ignore")

import os, sys

# ── Route ke root project agar semua import `modules.*` resolve dengan benar
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)          # config_loader mencari config.json di cwd

# ── Import strategy dari modul asli (TIDAK ada salinan kode) ─────────────────
from modules.config_loader   import CONFIG
from modules.smc             import analyze_smc
from modules.patterns        import find_pattern
from modules.technicals      import get_technicals, detect_divergence
from modules.quant           import calculate_metrics, check_fakeout
from modules.derivatives     import analyze_derivatives

import pandas as pd
import numpy as np
import pandas_ta as ta
from scipy.signal import argrelextrema
from datetime import datetime

# ══════════════════════════════════════════════════════════════════════
# CONSTANTS — sepenuhnya dari CONFIG / config.json
# ══════════════════════════════════════════════════════════════════════

LEVERAGE        = CONFIG["risk"]["target_leverage"]
RISK_PCT        = CONFIG["risk"]["risk_percent"]
INITIAL_BAL     = CONFIG["risk"]["paper_balance"]
MAX_DAILY_LOSS  = CONFIG["risk"]["max_daily_loss_pct"]
PENDING_EXPIRE  = CONFIG["risk"]["pending_expire_hours"]
MIN_RVOL        = CONFIG["indicators"]["min_rvol"]
MIN_TECH        = CONFIG["strategy"]["min_tech_score"]
MIN_SMC         = CONFIG["strategy"]["min_smc_score"]
MIN_DERIV       = CONFIG["strategy"]["min_deriv_score"]
MIN_RR          = CONFIG["strategy"]["risk_reward_min"]
MAX_DRIFT       = CONFIG["strategy"]["max_entry_drift_pct"]
MIN_CANDLES     = CONFIG["system"]["min_candles_analysis"]
ENTRY_TF        = CONFIG["system"]["entry_timeframe"]
TREND_TF        = CONFIG["system"]["trend_timeframe"]
PATTERN_SIGNALS = CONFIG["pattern_signals"]
FIB             = CONFIG["setup"]

BYBIT_FEE = 0.00055     # 0.055% taker fee per sisi (open + close) — Bybit

# Paper trader menggunakan 30/30/40, bukan tp_split config
# (tp_split config hanya untuk real order placement — lihat auto_trades.py)
TP1_PCT, TP2_PCT, TP3_PCT = 0.30, 0.30, 0.40

DATASET_15M = os.path.join(PROJECT_ROOT, "dataset", "BTCUSDT_15m.csv")
DATASET_1H  = os.path.join(PROJECT_ROOT, "dataset", "BTCUSDT_1h.csv")
OUTPUT_CSV  = os.path.join(PROJECT_ROOT, "backtest_results.csv")


# ══════════════════════════════════════════════════════════════════════
# MULTI-TIMEFRAME HELPERS
# (Replikasi get_btc_bias + get_symbol_trend dari main.py)
# ══════════════════════════════════════════════════════════════════════

def btc_bias_at(df1h: pd.DataFrame, ts) -> str:
    """EMA 13/21 pada 1h sampai ts — identik get_btc_bias() di main.py."""
    sub = df1h[df1h.index <= ts].tail(100)
    if len(sub) < 30:
        return "Sideways"
    ema13 = ta.ema(sub["close"], length=13)
    ema21 = ta.ema(sub["close"], length=21)
    if ema13 is None or ema21 is None or pd.isna(ema13.iloc[-1]):
        return "Sideways"
    return "Bullish" if ema13.iloc[-1] > ema21.iloc[-1] else "Bearish"


def symbol_trend_at(df1h: pd.DataFrame, ts) -> str:
    """Supertrend(10, 3.5) pada 1h sampai ts — identik get_symbol_trend() di main.py."""
    sub = df1h[df1h.index <= ts].tail(100)
    if len(sub) < 50:
        return "Sideways"
    try:
        st = ta.supertrend(sub["high"], sub["low"], sub["close"],
                           length=10, multiplier=3.5)
        if st is None or st.empty:
            return "Sideways"
        dir_col = [c for c in st.columns if c.startswith("SUPERTd")]
        if not dir_col:
            return "Sideways"
        d = st[dir_col[0]].iloc[-1]
        if pd.isna(d):
            return "Sideways"
        return "Bullish" if d == 1 else ("Bearish" if d == -1 else "Sideways")
    except Exception:
        return "Sideways"


# ══════════════════════════════════════════════════════════════════════
# SETUP HELPERS
# (Replikasi blok "Setup & R:R" dari main.py)
# ══════════════════════════════════════════════════════════════════════

def calc_setup(df: pd.DataFrame, side: str):
    """
    Fibonacci entry/SL/TP — identik blok "Setup & R:R" di main.py.
    Mengembalikan (entry, sl, tp1, tp2, tp3) atau None jika range=0.
    """
    swing_high = df["high"].iloc[-50:].max()
    swing_low  = df["low"].iloc[-50:].min()
    rng        = swing_high - swing_low
    if rng <= 0:
        return None

    if side == "Long":
        entry = swing_high - rng * (1 - FIB["fib_entry_start"])
        sl    = swing_low  - rng * FIB["fib_sl"]
        tp1   = swing_high + rng * (FIB["fib_tp_1"]  - 1)
        tp2   = swing_high + rng * (FIB["fib_tp_2"]  - 1)
        tp3   = swing_high + rng * (FIB["fib_tp_3"]  - 1)
    else:  # Short
        entry = swing_low  + rng * (1 - FIB["fib_entry_start"])
        sl    = swing_high + rng * FIB["fib_sl"]
        tp1   = swing_low  - rng * (FIB["fib_tp_1"]  - 1)
        tp2   = swing_low  - rng * (FIB["fib_tp_2"]  - 1)
        tp3   = swing_low  - rng * (FIB["fib_tp_3"]  - 1)

    return entry, sl, tp1, tp2, tp3


def calc_rr(entry, sl, tp3) -> float:
    risk = abs(entry - sl)
    return round(abs(tp3 - entry) / risk, 2) if risk > 0 else 0.0


# ══════════════════════════════════════════════════════════════════════
# PnL FORMULA
# (Identik _calc_pnl() dari paper_trader.py)
# ══════════════════════════════════════════════════════════════════════

def calc_pnl(side: str, entry: float, exit_px: float, qty: float) -> float:
    """
    Identik _calc_pnl() di paper_trader.py.
      pnl (Long)  = (exit - entry) × qty
      pnl (Short) = (entry - exit) × qty
      fee         = taker 0.055% open + 0.055% close
    qty sudah mengandung leverage — TIDAK perlu × lev lagi.
    """
    raw = (exit_px - entry) * qty if side == "Long" else (entry - exit_px) * qty
    fee = (entry * qty * BYBIT_FEE) + (exit_px * qty * BYBIT_FEE)
    return round(raw - fee, 6)


def calc_qty(balance: float, entry: float) -> float:
    """margin = balance × risk% → pos_val = margin × lev → qty = pos_val / entry."""
    margin  = balance * RISK_PCT
    pos_val = margin * LEVERAGE
    return round(pos_val / entry, 8)


def calc_margin(entry: float, qty: float) -> float:
    return round((entry * qty) / LEVERAGE, 4)


def remaining_qty(qty: float, tp1_hit: bool, tp2_hit: bool) -> float:
    """
    Identik _remaining_qty() di paper_trader.py.
    TP1 → jual 30% → sisa 70%
    TP2 → jual 30% → sisa 40%
    """
    rem = qty
    if tp1_hit: rem -= qty * TP1_PCT
    if tp2_hit: rem -= qty * TP2_PCT
    return round(rem, 8)


def effective_sl(trade: dict) -> float:
    """Identik _effective_sl() di paper_trader.py."""
    if trade["tp2_hit"]:  return trade["tp1"]    # trail ke TP1
    if trade["sl_moved"]: return trade["entry"]  # breakeven
    return trade["sl"]                           # original SL


# ══════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════════

class BacktestEngine:

    def __init__(self, df15: pd.DataFrame, df1h: pd.DataFrame):
        self.df15    = df15
        self.df1h    = df1h
        self.balance = INITIAL_BAL
        self.active  = None             # posisi aktif saat ini
        self.trades  = []               # log semua trade yang selesai
        self._daily_pnl: dict = {}      # date → pnl hari itu

    # ── daily loss guard ──────────────────────────────────────────────

    def _daily_loss_reached(self, date) -> bool:
        max_loss = INITIAL_BAL * MAX_DAILY_LOSS
        return self._daily_pnl.get(date, 0.0) < -max_loss

    def _add_daily(self, date, pnl: float):
        self._daily_pnl[date] = self._daily_pnl.get(date, 0.0) + pnl

    # ── partial TP handlers (identik paper_trader.py) ─────────────────

    def _hit_tp1(self, trade: dict, candle) -> float:
        partial = trade["qty"] * TP1_PCT
        pnl     = calc_pnl(trade["side"], trade["entry"], trade["tp1"], partial)
        self.balance      += pnl
        trade["tp1_hit"]   = True
        trade["sl_moved"]  = True       # SL → breakeven (entry)
        trade["_pnl_tp1"]  = pnl
        return pnl

    def _hit_tp2(self, trade: dict, candle) -> float:
        partial = trade["qty"] * TP2_PCT
        pnl     = calc_pnl(trade["side"], trade["entry"], trade["tp2"], partial)
        self.balance      += pnl
        trade["tp2_hit"]   = True       # SL → TP1
        trade["_pnl_tp2"]  = pnl
        return pnl

    # ── close trade (identik _close_paper_trade di paper_trader.py) ───

    def _close(self, trade: dict, candle, exit_px: float,
                final_pnl: float, reason: str):
        self.balance += final_pnl
        date = candle.name.date()
        self._add_daily(date, final_pnl)

        total_pnl = trade.get("_pnl_tp1", 0) + trade.get("_pnl_tp2", 0) + final_pnl
        margin    = calc_margin(trade["entry"], trade["qty"])
        roi_m     = (total_pnl / margin * 100) if margin > 0 else 0.0
        roi_b     = (total_pnl / INITIAL_BAL * 100)

        self.trades.append({
            "id":           trade["id"],
            "side":         trade["side"],
            "pattern":      trade["pattern"],
            "entry_ts":     trade["entry_ts"],
            "exit_ts":      candle.name,
            "entry":        round(trade["entry"], 4),
            "exit":         round(exit_px, 4),
            "sl":           round(trade["sl"], 4),
            "tp1":          round(trade["tp1"], 4),
            "tp2":          round(trade["tp2"], 4),
            "tp3":          round(trade["tp3"], 4),
            "qty":          trade["qty"],
            "leverage":     LEVERAGE,
            "margin":       margin,
            "tp1_hit":      trade["tp1_hit"],
            "tp2_hit":      trade["tp2_hit"],
            "reason":       reason,
            "pnl_tp1":      round(trade.get("_pnl_tp1", 0), 6),
            "pnl_tp2":      round(trade.get("_pnl_tp2", 0), 6),
            "pnl_final":    round(final_pnl, 6),
            "total_pnl":    round(total_pnl, 6),
            "roi_margin":   round(roi_m, 2),
            "roi_balance":  round(roi_b, 4),
            "balance_after":round(self.balance, 4),
            "rr":           trade.get("rr", 0),
            "smc_score":    trade.get("smc_score", 0),
            "tech_score":   trade.get("tech_score", 0),
            "quant_score":  trade.get("quant_score", 0),
            "deriv_score":  trade.get("deriv_score", 0),
            "btc_bias":     trade.get("btc_bias", ""),
            "sym_trend":    trade.get("sym_trend", ""),
            "is_win":       total_pnl >= 0,
        })
        self.active = None

    # ── monitor open trade (identik paper_monitor di paper_trader.py) ─

    def _monitor(self, trade: dict, candle) -> bool:
        """Return True jika trade ditutup pada candle ini."""
        side   = trade["side"]
        hi, lo = candle["high"], candle["low"]
        tp1, tp2, tp3 = trade["tp1"], trade["tp2"], trade["tp3"]
        eff_sl = effective_sl(trade)

        if side == "Long":
            sl_hit  = lo  <= eff_sl
            tp1_hit = hi  >= tp1
            tp2_hit = hi  >= tp2
            tp3_hit = hi  >= tp3
        else:
            sl_hit  = hi  >= eff_sl
            tp1_hit = lo  <= tp1
            tp2_hit = lo  <= tp2
            tp3_hit = lo  <= tp3

        # SL sebelum TP1 (belum ada profit lock) — worst-case
        if sl_hit and not trade["tp1_hit"]:
            rem = remaining_qty(trade["qty"], False, False)
            pnl = calc_pnl(side, trade["entry"], eff_sl, rem)
            self._close(trade, candle, eff_sl, pnl, "SL")
            return True

        # TP3 hit → cascade partials lalu close
        if tp3_hit:
            if not trade["tp1_hit"]: self._hit_tp1(trade, candle)
            if not trade["tp2_hit"]: self._hit_tp2(trade, candle)
            rem = remaining_qty(trade["qty"], True, True)
            pnl = calc_pnl(side, trade["entry"], tp3, rem)
            self._close(trade, candle, tp3, pnl, "TP3")
            return True

        # SL trailing (setelah TP1 / TP2)
        if sl_hit and trade["tp1_hit"]:
            rem    = remaining_qty(trade["qty"], trade["tp1_hit"], trade["tp2_hit"])
            pnl    = calc_pnl(side, trade["entry"], eff_sl, rem)
            reason = "SL Trail (TP1)" if trade["tp2_hit"] else "Breakeven SL"
            self._close(trade, candle, eff_sl, pnl, reason)
            return True

        # TP2 partial
        if tp2_hit and not trade["tp2_hit"]:
            if not trade["tp1_hit"]: self._hit_tp1(trade, candle)
            self._hit_tp2(trade, candle)
            return False

        # TP1 partial
        if tp1_hit and not trade["tp1_hit"]:
            self._hit_tp1(trade, candle)
            return False

        return False

    # ── signal generation (identik analyze_ticker di main.py) ─────────

    def _generate_signal(self, i: int, candle, trade_id: int) -> dict | None:
        ts     = candle.name
        window = self.df15.iloc[max(0, i - MIN_CANDLES + 1): i + 1].copy()

        if len(window) < MIN_CANDLES:
            return None

        # ① Technicals (modules/technicals.py)
        df = get_technicals(window)
        if len(df) < 50:
            return None

        # ② Pattern (modules/patterns.py — uses CONFIG["patterns"] internally)
        pattern = find_pattern(df)
        if not pattern:
            return None
        side = PATTERN_SIGNALS.get(pattern)
        if not side:
            return None

        # ③ MTF alignment — Supertrend 1h (identik get_symbol_trend di main.py)
        sym_trend = symbol_trend_at(self.df1h, ts)
        if sym_trend == "Bearish" and side == "Long":  return None
        if sym_trend == "Bullish" and side == "Short": return None

        # ④ BTC bias — EMA13/21 1h (identik get_btc_bias di main.py)
        bias = btc_bias_at(self.df1h, ts)
        if "Bearish" in bias and side == "Long":  return None
        if "Bullish" in bias and side == "Short": return None

        # ⑤ SMC (modules/smc.py)
        valid_smc, smc_score, _ = analyze_smc(df, side)
        if not valid_smc or smc_score < MIN_SMC:
            return None

        # ⑥ Quant metrics (modules/quant.py)
        #    ticker & order_book tidak tersedia offline → pass dummy values
        dummy_ticker = {"last": float(candle["close"]), "info": {}}
        df, basis, z_score, zeta_score, obi, quant_score, _ = \
            calculate_metrics(df, dummy_ticker, order_book={})

        # ⑦ Derivatives — CVD divergence (modules/derivatives.py)
        _, deriv_score, _ = analyze_derivatives(df, dummy_ticker, side)
        if deriv_score < MIN_DERIV:
            return None

        # ⑧ Divergence + Tech score (identik main.py)
        div_score, _ = detect_divergence(df)
        tech_score   = 3 + div_score + min(smc_score, 2)
        if tech_score < MIN_TECH:
            return None

        # ⑨ RVOL (modules/quant.py)
        valid_fo, _ = check_fakeout(df, MIN_RVOL)
        if not valid_fo:
            return None

        # ⑩ Fibonacci setup (identik main.py)
        setup = calc_setup(df, side)
        if setup is None:
            return None
        entry, sl, tp1, tp2, tp3 = setup

        # ⑪ R:R filter
        rr = calc_rr(entry, sl, tp3)
        if rr < MIN_RR:
            return None

        # ⑫ Entry proximity (identik main.py)
        drift = abs(float(candle["close"]) - entry) / float(candle["close"])
        if drift > MAX_DRIFT:
            return None

        return {
            "id":          trade_id,
            "side":        side,
            "pattern":     pattern,
            "entry_ts":    ts,
            "status":      "PENDING",
            "entry":       entry,
            "sl":          sl,
            "tp1":         tp1,
            "tp2":         tp2,
            "tp3":         tp3,
            "qty":         calc_qty(self.balance, entry),
            "tp1_hit":     False,
            "tp2_hit":     False,
            "sl_moved":    False,
            "_pnl_tp1":    0.0,
            "_pnl_tp2":    0.0,
            "rr":          rr,
            "smc_score":   smc_score,
            "tech_score":  tech_score,
            "quant_score": quant_score,
            "deriv_score": deriv_score,
            "btc_bias":    bias,
            "sym_trend":   sym_trend,
        }

    # ── main loop ──────────────────────────────────────────────────────

    def run(self):
        expire_bars = int(PENDING_EXPIRE * (60 / 15))   # jam × bar per jam
        trade_id    = 0
        pending_at  = None
        signals_ok  = 0
        signals_rej = 0

        print("=" * 65)
        print("   🚀 BACKTEST ENGINE — Bot Auto Screening Bybit")
        print("=" * 65)
        print(f"   Config        : {PROJECT_ROOT}/config.json")
        print(f"   Dataset 15m   : {DATASET_15M}")
        print(f"   Dataset 1h    : {DATASET_1H}")
        print(f"   Date range    : {self.df15.index[0]} → {self.df15.index[-1]}")
        print(f"   Candles       : {len(self.df15)} (15m)  |  {len(self.df1h)} (1h)")
        print(f"   Balance awal  : ${INITIAL_BAL:.2f}")
        print(f"   Leverage      : {LEVERAGE}x  |  Risk/trade : {RISK_PCT*100:.1f}%")
        print(f"   Fee Bybit     : {BYBIT_FEE*100:.3f}% taker per sisi")
        print(f"   TP split      : {TP1_PCT*100:.0f}% / {TP2_PCT*100:.0f}% / {TP3_PCT*100:.0f}%")
        print(f"   Min R:R       : {MIN_RR}  |  Max drift : {MAX_DRIFT*100:.0f}%")
        print(f"   Min SMC       : {MIN_SMC}  |  Min RVOL  : {MIN_RVOL}x")
        print("=" * 65)

        for i in range(MIN_CANDLES, len(self.df15)):
            candle = self.df15.iloc[i]
            date   = candle.name.date()

            # ── Monitor trade aktif ──────────────────────────────────
            if self.active is not None:

                if self.active["status"] == "PENDING":
                    # Fill check (limit order)
                    s, ep = self.active["side"], self.active["entry"]
                    filled = (s == "Long" and candle["low"] <= ep) or \
                             (s == "Short" and candle["high"] >= ep)
                    # Expire
                    if (i - pending_at) > expire_bars:
                        self.active = None
                        pending_at  = None
                        continue
                    if filled:
                        self.active["qty"]     = calc_qty(self.balance, ep)
                        self.active["status"]  = "OPEN"
                        self.active["entry_ts"]= candle.name

                elif self.active["status"] == "OPEN":
                    self._monitor(self.active, candle)

                continue     # satu posisi aktif — jangan buka baru

            # ── Daily loss guard ─────────────────────────────────────
            if self._daily_loss_reached(date):
                continue

            # ── Coba buka sinyal baru ────────────────────────────────
            trade_id += 1
            sig = self._generate_signal(i, candle, trade_id)
            if sig:
                self.active = sig
                pending_at  = i
                signals_ok += 1
            else:
                signals_rej += 1

        # ── Tutup posisi open yang tersisa (mark-to-market) ──────────
        if self.active and self.active["status"] == "OPEN":
            last  = self.df15.iloc[-1]
            ep    = self.active["entry"]
            rem   = remaining_qty(self.active["qty"],
                                  self.active["tp1_hit"],
                                  self.active["tp2_hit"])
            pnl   = calc_pnl(self.active["side"], ep, last["close"], rem)
            self._close(self.active, last, last["close"], pnl, "MTM Close")

        print(f"\n   Signals valid   : {signals_ok}")
        print(f"   Signals rejected: {signals_rej}")
        print(f"   Trades executed : {len(self.trades)}")
        print("=" * 65)


# ══════════════════════════════════════════════════════════════════════
# REPORT
# ══════════════════════════════════════════════════════════════════════

def print_report(trades: list, initial: float, final: float):
    if not trades:
        print("\n⚠️  Tidak ada trade yang dieksekusi — coba longgarkan filter di config.json")
        return

    df = pd.DataFrame(trades).sort_values("exit_ts")

    total  = len(df)
    wins   = df[df["is_win"]]
    losses = df[~df["is_win"]]
    wr     = len(wins) / total * 100

    net_pnl   = df["total_pnl"].sum()
    net_ret   = (final - initial) / initial * 100
    avg_win   = wins["total_pnl"].mean()   if len(wins)   else 0
    avg_loss  = losses["total_pnl"].mean() if len(losses) else 0
    pf        = abs(wins["total_pnl"].sum() / losses["total_pnl"].sum()) \
                if losses["total_pnl"].sum() != 0 else float("inf")
    exp       = (wr / 100 * avg_win) + ((1 - wr / 100) * avg_loss)

    # Drawdown
    bal_curve = [initial] + list(df["balance_after"])
    peak, max_dd, max_dd_pct = bal_curve[0], 0.0, 0.0
    for b in bal_curve:
        if b > peak: peak = b
        dd_pct = (peak - b) / peak * 100 if peak else 0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            max_dd     = peak - b

    # Sharpe
    sharpe = 0.0
    if len(df) > 1 and df["total_pnl"].std() > 0:
        sharpe = (df["total_pnl"].mean() / df["total_pnl"].std()) * (252 ** 0.5)

    # Consecutive
    maxw = maxl = cw = cl = 0
    for w in df["is_win"]:
        if w:   cw += 1; cl = 0
        else:   cl += 1; cw = 0
        maxw = max(maxw, cw); maxl = max(maxl, cl)

    SEP  = "═" * 65
    sep  = "─" * 65

    print(f"\n{SEP}")
    print(f"   📊  BACKTEST REPORT — Bot Auto Screening Bybit")
    print(f"   Config : {PROJECT_ROOT}/config.json")
    print(SEP)

    print(f"\n{'PERFORMANCE SUMMARY':^65}")
    print(sep)
    print(f"  {'Initial Balance':<35} ${initial:.2f}")
    print(f"  {'Final Balance':<35} ${final:.4f}")
    print(f"  {'Net PnL':<35} ${net_pnl:+.4f}")
    print(f"  {'Net Return':<35} {net_ret:+.2f}%")
    print(f"  {'Max Drawdown':<35} ${max_dd:.4f}  ({max_dd_pct:.2f}%)")
    print(sep)

    print(f"\n{'TRADE STATISTICS':^65}")
    print(sep)
    print(f"  {'Total Trades':<35} {total}")
    print(f"  {'Wins / Losses':<35} {len(wins)} / {len(losses)}")
    print(f"  {'Win Rate':<35} {wr:.1f}%")
    print(f"  {'Avg Win (all partials)':<35} ${avg_win:+.4f}")
    print(f"  {'Avg Loss':<35} ${avg_loss:+.4f}")
    print(f"  {'Profit Factor':<35} {pf:.2f}")
    print(f"  {'Expectancy per Trade':<35} ${exp:+.4f}")
    print(f"  {'Sharpe Ratio (simplified)':<35} {sharpe:.2f}")
    print(f"  {'Max Consecutive Wins':<35} {maxw}")
    print(f"  {'Max Consecutive Losses':<35} {maxl}")
    print(sep)

    print(f"\n{'OUTCOME BREAKDOWN':^65}")
    print(sep)
    for reason, cnt in df["reason"].value_counts().items():
        pnl_r = df[df["reason"] == reason]["total_pnl"].sum()
        print(f"  {reason:<22} {cnt:>3} trades ({cnt/total*100:4.1f}%)  "
              f"PnL: ${pnl_r:+.4f}")
    print(sep)

    print(f"\n{'PATTERN BREAKDOWN':^65}")
    print(sep)
    print(f"  {'Pattern':<26} {'Cnt':>4}  {'WR%':>5}  {'Total PnL':>10}  {'Avg PnL':>9}")
    print(f"  {'-'*26} {'-'*4}  {'-'*5}  {'-'*10}  {'-'*9}")
    for pat, grp in df.groupby("pattern"):
        w_  = grp["is_win"].sum()
        wr_ = w_ / len(grp) * 100
        tp_ = grp["total_pnl"].sum()
        ap_ = grp["total_pnl"].mean()
        print(f"  {pat:<26} {len(grp):>4}  {wr_:>4.1f}%  ${tp_:>+9.4f}  ${ap_:>+8.4f}")
    print(sep)

    print(f"\n{'SIDE BREAKDOWN':^65}")
    print(sep)
    for side in ["Long", "Short"]:
        g = df[df["side"] == side]
        if g.empty: continue
        print(f"  {side:<8}  {len(g):>3} trades  "
              f"WR: {g['is_win'].sum()/len(g)*100:4.1f}%  "
              f"PnL: ${g['total_pnl'].sum():+.4f}")
    print(sep)

    print(f"\n{'TRADE LOG':^65}")
    print(sep)
    print(f"  {'#':>3}  {'Side':<6}  {'Pattern':<22}  {'Entry':>9}  "
          f"{'Exit':>9}  {'Total PnL':>10}  {'Outcome':<14}  R:R")
    print(f"  {'─'*3}  {'─'*6}  {'─'*22}  {'─'*9}  {'─'*9}  {'─'*10}  {'─'*14}  {'─'*4}")
    for _, row in df.iterrows():
        icon = "✅" if row["is_win"] else "❌"
        print(f"  {int(row['id']):>3}  {row['side']:<6}  {row['pattern']:<22}  "
              f"{row['entry']:>9.2f}  {row['exit']:>9.2f}  "
              f"${row['total_pnl']:>+9.4f}  "
              f"{icon} {row['reason']:<12}  {row['rr']:.1f}")
    print(SEP)


# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["timestamp_open"], unit="ms")
    df = df.set_index("datetime").sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def main():
    print("\n📂 Loading dataset...")
    df15 = load_csv(DATASET_15M)
    df1h = load_csv(DATASET_1H)
    print(f"   15m : {len(df15)} candles  {df15.index[0]} → {df15.index[-1]}")
    print(f"   1h  : {len(df1h)} candles  {df1h.index[0]} → {df1h.index[-1]}")

    engine = BacktestEngine(df15, df1h)
    print("\n⚙️  Running backtest...\n")
    engine.run()

    print_report(engine.trades, INITIAL_BAL, engine.balance)

    if engine.trades:
        pd.DataFrame(engine.trades).sort_values("exit_ts").to_csv(OUTPUT_CSV, index=False)
        print(f"\n💾 Trade log disimpan ke : {OUTPUT_CSV}")

    print("\n✅ Backtest selesai.\n")


if __name__ == "__main__":
    main()

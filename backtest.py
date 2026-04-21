"""
╔══════════════════════════════════════════════════════════════════════════╗
║        BACKTEST MULTI-COIN ENGINE — Bot Auto Screening Bybit            ║
║                                                                         ║
║  ► Mengintegrasikan SELURUH dataset yang tersedia di folder dataset/    ║
║  ► Strategi identik 1:1 dari config.json (tidak ada perubahan logic)    ║
║  ► Module core TIDAK disentuh — hanya file baru ini                     ║
║                                                                         ║
║  Modul strategi yang dipakai (asli):                                    ║
║    modules/smc.py          → analyze_smc()                              ║
║    modules/patterns.py     → find_pattern()                             ║
║    modules/technicals.py   → get_technicals(), detect_divergence()      ║
║    modules/quant.py        → calculate_metrics(), check_fakeout()       ║
║    modules/derivatives.py  → analyze_derivatives()                      ║
║    modules/config_loader.py→ CONFIG                                     ║
║                                                                         ║
║  Dataset : dataset/<SYMBOL>_15m.csv  (entry timeframe)                  ║
║            dataset/<SYMBOL>_1h.csv   (trend timeframe)                  ║
║                                                                         ║
║  Kolom CSV yang dimanfaatkan:                                           ║
║    timestamp_open, timestamp_close, low, high, open, close, volume      ║
║    volume_quote, trades, taker_buy_volume, taker_buy_quote              ║
║    adx, rsi, macd, macd_signal, bb_upper, bb_middle, bb_lower           ║
║    atr_prev, atr_last, ema_short, ema_long                              ║
╚══════════════════════════════════════════════════════════════════════════╝

Cara pakai:
    cd Bot-Auto-Screening-Bybit-backtest
    python backtest_multi.py
"""

import warnings
warnings.filterwarnings("ignore")

import os, sys, glob, time
from datetime import datetime

# ── Route ke root project ─────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

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

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS — sepenuhnya dari CONFIG / config.json (identik backtest.py)
# ══════════════════════════════════════════════════════════════════════════════

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
PATTERN_SIGNALS = CONFIG["pattern_signals"]
FIB             = CONFIG["setup"]

BYBIT_FEE       = 0.00055       # 0.055% taker fee per sisi — Bybit
TP1_PCT         = 0.30          # identik paper_trader.py
TP2_PCT         = 0.30
TP3_PCT         = 0.40

DATASET_DIR     = os.path.join(PROJECT_ROOT, "dataset")
OUTPUT_DIR      = PROJECT_ROOT


# ══════════════════════════════════════════════════════════════════════════════
# COLOR / STYLE HELPERS (terminal ANSI)
# ══════════════════════════════════════════════════════════════════════════════

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    BLUE   = "\033[94m"
    MAGENTA= "\033[95m"
    WHITE  = "\033[97m"
    GRAY   = "\033[90m"

def fmt_price(p, decimals=4):
    if p >= 1000:   return f"{p:,.2f}"
    elif p >= 1:    return f"{p:.4f}"
    elif p >= 0.01: return f"{p:.6f}"
    else:            return f"{p:.8f}"

def fmt_pnl(v):
    sign = "+" if v >= 0 else ""
    col  = C.GREEN if v >= 0 else C.RED
    return f"{col}{sign}${v:.4f}{C.RESET}"

def fmt_ts(ts):
    return str(ts)[:16]

def bar(char="═", n=72):
    return char * n

def pbar(char="─", n=72):
    return char * n


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-TIMEFRAME HELPERS (identik backtest.py)
# ══════════════════════════════════════════════════════════════════════════════

def btc_bias_at(df1h: pd.DataFrame, ts) -> str:
    sub = df1h[df1h.index <= ts].tail(100)
    if len(sub) < 30:
        return "Sideways"
    ema13 = ta.ema(sub["close"], length=13)
    ema21 = ta.ema(sub["close"], length=21)
    if ema13 is None or ema21 is None or pd.isna(ema13.iloc[-1]):
        return "Sideways"
    return "Bullish" if ema13.iloc[-1] > ema21.iloc[-1] else "Bearish"


def symbol_trend_at(df1h: pd.DataFrame, ts) -> str:
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


# ══════════════════════════════════════════════════════════════════════════════
# SETUP HELPERS (identik backtest.py)
# ══════════════════════════════════════════════════════════════════════════════

def calc_setup(df: pd.DataFrame, side: str):
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
    else:
        entry = swing_low  + rng * (1 - FIB["fib_entry_start"])
        sl    = swing_high + rng * FIB["fib_sl"]
        tp1   = swing_low  - rng * (FIB["fib_tp_1"]  - 1)
        tp2   = swing_low  - rng * (FIB["fib_tp_2"]  - 1)
        tp3   = swing_low  - rng * (FIB["fib_tp_3"]  - 1)
    return entry, sl, tp1, tp2, tp3


def calc_rr(entry, sl, tp3) -> float:
    risk = abs(entry - sl)
    return round(abs(tp3 - entry) / risk, 2) if risk > 0 else 0.0


# ══════════════════════════════════════════════════════════════════════════════
# PnL FORMULA (identik backtest.py / paper_trader.py)
# ══════════════════════════════════════════════════════════════════════════════

def calc_pnl(side: str, entry: float, exit_px: float, qty: float) -> float:
    raw = (exit_px - entry) * qty if side == "Long" else (entry - exit_px) * qty
    fee = (entry * qty * BYBIT_FEE) + (exit_px * qty * BYBIT_FEE)
    return round(raw - fee, 6)

def calc_qty(balance: float, entry: float) -> float:
    margin  = balance * RISK_PCT
    pos_val = margin * LEVERAGE
    return round(pos_val / entry, 8)

def calc_margin(entry: float, qty: float) -> float:
    return round((entry * qty) / LEVERAGE, 4)

def remaining_qty(qty: float, tp1_hit: bool, tp2_hit: bool) -> float:
    rem = qty
    if tp1_hit: rem -= qty * TP1_PCT
    if tp2_hit: rem -= qty * TP2_PCT
    return round(rem, 8)

def effective_sl(trade: dict) -> float:
    if trade["tp2_hit"]:  return trade["tp1"]
    if trade["sl_moved"]: return trade["entry"]
    return trade["sl"]


# ══════════════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE (per-symbol, verbose logging)
# ══════════════════════════════════════════════════════════════════════════════

class BacktestEngine:

    def __init__(self, symbol: str, df15: pd.DataFrame, df1h: pd.DataFrame,
                 initial_balance: float = INITIAL_BAL, verbose: bool = True):
        self.symbol   = symbol
        self.df15     = df15
        self.df1h     = df1h
        self.balance  = initial_balance
        self.initial  = initial_balance
        self.active   = None
        self.trades   = []
        self._daily_pnl: dict = {}
        self.verbose  = verbose
        self._events  = []          # structured event log untuk combined report

    # ── Daily loss guard ──────────────────────────────────────────────────────

    def _daily_loss_reached(self, date) -> bool:
        max_loss = INITIAL_BAL * MAX_DAILY_LOSS
        return self._daily_pnl.get(date, 0.0) < -max_loss

    def _add_daily(self, date, pnl: float):
        self._daily_pnl[date] = self._daily_pnl.get(date, 0.0) + pnl

    # ── Verbose event printers ────────────────────────────────────────────────

    def _log(self, msg):
        if self.verbose:
            print(msg)
        self._events.append(msg)

    def _log_signal(self, sig: dict):
        side_icon = "📈" if sig["side"] == "Long" else "📉"
        side_col  = C.GREEN if sig["side"] == "Long" else C.RED
        self._log(
            f"\n{C.CYAN}┌{'─'*70}{C.RESET}\n"
            f"{C.CYAN}│{C.RESET} {side_icon} {C.BOLD}{C.YELLOW}[{fmt_ts(sig['entry_ts'])}]  "
            f"{self.symbol}  ── SIGNAL DETECTED ──{C.RESET}\n"
            f"{C.CYAN}│{C.RESET}   Pattern  : {C.BOLD}{C.MAGENTA}{sig['pattern']}{C.RESET}   "
            f"Side : {side_col}{C.BOLD}{sig['side']}{C.RESET}   "
            f"R:R : {C.BOLD}{sig['rr']}x{C.RESET}\n"
            f"{C.CYAN}│{C.RESET}   Entry    : {C.WHITE}${fmt_price(sig['entry'])}{C.RESET}   "
            f"SL : {C.RED}${fmt_price(sig['sl'])}{C.RESET}\n"
            f"{C.CYAN}│{C.RESET}   TP1      : {C.GREEN}${fmt_price(sig['tp1'])}{C.RESET}   "
            f"TP2 : {C.GREEN}${fmt_price(sig['tp2'])}{C.RESET}   "
            f"TP3 : {C.GREEN}${fmt_price(sig['tp3'])}{C.RESET}\n"
            f"{C.CYAN}│{C.RESET}   Scores   : SMC={sig['smc_score']}  "
            f"Tech={sig['tech_score']}  "
            f"Quant={sig['quant_score']}  "
            f"Deriv={sig['deriv_score']}\n"
            f"{C.CYAN}│{C.RESET}   Bias     : BTC={C.YELLOW}{sig['btc_bias']}{C.RESET}   "
            f"Trend={C.YELLOW}{sig['sym_trend']}{C.RESET}   "
            f"Margin: ${calc_margin(sig['entry'], sig['qty']):.4f}\n"
            f"{C.CYAN}│{C.RESET}   Status   : {C.YELLOW}⏳ PENDING — waiting for limit fill…{C.RESET}\n"
            f"{C.CYAN}└{'─'*70}{C.RESET}"
        )

    def _log_filled(self, trade: dict, candle):
        self._log(
            f"  {C.GREEN}✅ [{fmt_ts(candle.name)}]  {self.symbol}  ORDER FILLED{C.RESET}\n"
            f"     Entry  : {C.BOLD}${fmt_price(trade['entry'])}{C.RESET}   "
            f"Qty : {trade['qty']:.8f}   "
            f"Margin : ${calc_margin(trade['entry'], trade['qty']):.4f}   "
            f"Pos Value : ${trade['entry']*trade['qty']:.2f}"
        )

    def _log_expired(self, trade: dict, candle):
        self._log(
            f"  {C.GRAY}⏰ [{fmt_ts(candle.name)}]  {self.symbol}  "
            f"ORDER EXPIRED (limit not filled in {PENDING_EXPIRE}h){C.RESET}\n"
            f"     Entry was : ${fmt_price(trade['entry'])}   "
            f"Pattern : {trade['pattern']}"
        )

    def _log_tp(self, level: int, trade: dict, candle, pnl: float, price: float):
        icons    = {1: "💰", 2: "💰💰", 3: "🏆"}
        pct      = {1: TP1_PCT, 2: TP2_PCT, 3: TP3_PCT}
        partial  = trade["qty"] * pct.get(level, TP3_PCT)
        trail_msg= ""
        if level == 1:
            trail_msg = f"   → SL moved to {C.YELLOW}BREAKEVEN{C.RESET} (${fmt_price(trade['entry'])})"
        elif level == 2:
            trail_msg = f"   → SL trailed to {C.YELLOW}TP1{C.RESET} (${fmt_price(trade['tp1'])})"
        self._log(
            f"  {icons.get(level,'💰')} [{fmt_ts(candle.name)}]  {self.symbol}  "
            f"{C.GREEN}{C.BOLD}TP{level} HIT @ ${fmt_price(price)}{C.RESET}\n"
            f"     Partial PnL : {fmt_pnl(pnl)}   "
            f"Closed : {pct.get(level,0)*100:.0f}% ({partial:.8f} qty)\n"
            f"     Balance Now : ${self.balance:.4f}{trail_msg}"
        )

    def _log_close(self, trade: dict, candle, exit_px: float, pnl: float,
                   reason: str, total_pnl: float):
        is_win = total_pnl >= 0
        icon   = "🟢" if is_win else "🔴"
        rea_col= C.GREEN if is_win else C.RED
        self._log(
            f"  {icon} [{fmt_ts(candle.name)}]  {self.symbol}  "
            f"{rea_col}{C.BOLD}POSITION CLOSED — {reason}{C.RESET}\n"
            f"     Exit Price  : {C.BOLD}${fmt_price(exit_px)}{C.RESET}   "
            f"Final PnL : {fmt_pnl(pnl)}\n"
            f"     Total PnL   : {fmt_pnl(total_pnl)}   "
            f"  TP1_hit={trade['tp1_hit']}  TP2_hit={trade['tp2_hit']}\n"
            f"     Balance     : {C.BOLD}${self.balance:.4f}{C.RESET}   "
            f"{'▲' if is_win else '▼'} vs initial : "
            f"{fmt_pnl(self.balance - self.initial)}\n"
            f"     {'─'*62}"
        )

    def _log_sl(self, trade: dict, candle, sl_price: float, pnl: float,
                reason: str, total_pnl: float):
        self._log(
            f"  🔴 [{fmt_ts(candle.name)}]  {self.symbol}  "
            f"{C.RED}{C.BOLD}STOP LOSS HIT — {reason}{C.RESET}\n"
            f"     SL Price   : {C.RED}${fmt_price(sl_price)}{C.RESET}   "
            f"Final PnL : {fmt_pnl(pnl)}\n"
            f"     Total PnL  : {fmt_pnl(total_pnl)}   "
            f"TP1_hit={trade['tp1_hit']}  TP2_hit={trade['tp2_hit']}\n"
            f"     Balance    : {C.BOLD}${self.balance:.4f}{C.RESET}   "
            f"vs initial : {fmt_pnl(self.balance - self.initial)}\n"
            f"     {'─'*62}"
        )

    def _log_daily_block(self, date):
        if self.verbose:
            print(
                f"  {C.YELLOW}⛔ [{date}]  {self.symbol}  "
                f"Daily loss limit reached — trading paused for today{C.RESET}"
            )

    # ── Partial TP handlers (identik backtest.py) ─────────────────────────────

    def _hit_tp1(self, trade: dict, candle) -> float:
        partial = trade["qty"] * TP1_PCT
        pnl     = calc_pnl(trade["side"], trade["entry"], trade["tp1"], partial)
        self.balance     += pnl
        trade["tp1_hit"]  = True
        trade["sl_moved"] = True
        trade["_pnl_tp1"] = pnl
        self._log_tp(1, trade, candle, pnl, trade["tp1"])
        return pnl

    def _hit_tp2(self, trade: dict, candle) -> float:
        partial = trade["qty"] * TP2_PCT
        pnl     = calc_pnl(trade["side"], trade["entry"], trade["tp2"], partial)
        self.balance     += pnl
        trade["tp2_hit"]  = True
        trade["_pnl_tp2"] = pnl
        self._log_tp(2, trade, candle, pnl, trade["tp2"])
        return pnl

    # ── Close trade (identik backtest.py) ─────────────────────────────────────

    def _close(self, trade: dict, candle, exit_px: float,
               final_pnl: float, reason: str):
        self.balance += final_pnl
        date = candle.name.date()
        self._add_daily(date, final_pnl)

        total_pnl = trade.get("_pnl_tp1", 0) + trade.get("_pnl_tp2", 0) + final_pnl
        margin    = calc_margin(trade["entry"], trade["qty"])
        roi_m     = (total_pnl / margin * 100) if margin > 0 else 0.0
        roi_b     = (total_pnl / INITIAL_BAL * 100)

        if reason.startswith("SL") or reason.startswith("Breakeven") or "Trail" in reason:
            self._log_sl(trade, candle, exit_px, final_pnl, reason, total_pnl)
        elif reason != "TP3":   # TP3 sudah di-log sebelum _close dipanggil
            self._log_close(trade, candle, exit_px, final_pnl, reason, total_pnl)

        self.trades.append({
            "symbol":       self.symbol,
            "id":           trade["id"],
            "side":         trade["side"],
            "pattern":      trade["pattern"],
            "entry_ts":     trade["entry_ts"],
            "exit_ts":      candle.name,
            "entry":        round(trade["entry"], 8),
            "exit":         round(exit_px, 8),
            "sl":           round(trade["sl"], 8),
            "tp1":          round(trade["tp1"], 8),
            "tp2":          round(trade["tp2"], 8),
            "tp3":          round(trade["tp3"], 8),
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
            "balance_after":round(self.balance, 6),
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

    # ── Monitor open trade (identik backtest.py) ──────────────────────────────

    def _monitor(self, trade: dict, candle) -> bool:
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

        # SL before TP1 — full loss
        if sl_hit and not trade["tp1_hit"]:
            rem = remaining_qty(trade["qty"], False, False)
            pnl = calc_pnl(side, trade["entry"], eff_sl, rem)
            self._close(trade, candle, eff_sl, pnl, "SL")
            return True

        # TP3 → cascade partials then full close
        if tp3_hit:
            if not trade["tp1_hit"]: self._hit_tp1(trade, candle)
            if not trade["tp2_hit"]: self._hit_tp2(trade, candle)
            rem = remaining_qty(trade["qty"], True, True)
            pnl = calc_pnl(side, trade["entry"], tp3, rem)
            # log TP3 partial dulu, LALU _close (jangan double-log)
            icons   = {1: "💰", 2: "💰💰", 3: "🏆"}
            self._log(
                f"  {icons[3]} [{fmt_ts(candle.name)}]  {self.symbol}  "
                f"{C.GREEN}{C.BOLD}TP3 HIT @ ${fmt_price(tp3)}{C.RESET}\n"
                f"     Final Leg  : {fmt_pnl(pnl)}   "
                f"Closed : 40% ({rem:.8f} qty) — full position closed"
            )
            self._close(trade, candle, tp3, pnl, "TP3")
            return True

        # SL trailing (after TP1 / TP2)
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

    # ── Signal generation (identik backtest.py, + CVD dari taker_buy_volume) ──

    def _generate_signal(self, i: int, candle, trade_id: int) -> dict | None:
        ts     = candle.name
        window = self.df15.iloc[max(0, i - MIN_CANDLES + 1): i + 1].copy()

        if len(window) < MIN_CANDLES:
            return None

        # ① Technicals (modules/technicals.py)
        df = get_technicals(window)
        if len(df) < 50:
            return None

        # ② Pattern (modules/patterns.py)
        pattern = find_pattern(df)
        if not pattern:
            return None
        side = PATTERN_SIGNALS.get(pattern)
        if not side:
            return None

        # ③ MTF — Supertrend 1h
        sym_trend = symbol_trend_at(self.df1h, ts)
        if sym_trend == "Bearish" and side == "Long":  return None
        if sym_trend == "Bullish" and side == "Short": return None

        # ④ BTC bias — EMA13/21 1h
        bias = btc_bias_at(self.df1h, ts)
        if "Bearish" in bias and side == "Long":  return None
        if "Bullish" in bias and side == "Short": return None

        # ⑤ SMC (modules/smc.py)
        valid_smc, smc_score, _ = analyze_smc(df, side)
        if not valid_smc or smc_score < MIN_SMC:
            return None

        # ⑥ Quant metrics
        dummy_ticker = {"last": float(candle["close"]), "info": {}}
        df, basis, z_score, zeta_score, obi, quant_score, _ = \
            calculate_metrics(df, dummy_ticker, order_book={})

        # ⑦ Derivatives — CVD divergence
        _, deriv_score, _ = analyze_derivatives(df, dummy_ticker, side)
        if deriv_score < MIN_DERIV:
            return None

        # ⑧ Divergence + Tech score
        div_score, _ = detect_divergence(df)
        tech_score   = 3 + div_score + min(smc_score, 2)
        if tech_score < MIN_TECH:
            return None

        # ⑨ RVOL
        valid_fo, _ = check_fakeout(df, MIN_RVOL)
        if not valid_fo:
            return None

        # ⑩ Fibonacci setup
        setup = calc_setup(df, side)
        if setup is None:
            return None
        entry, sl, tp1, tp2, tp3 = setup

        # ⑪ R:R filter
        rr = calc_rr(entry, sl, tp3)
        if rr < MIN_RR:
            return None

        # ⑫ Entry proximity
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

    # ── Main loop ──────────────────────────────────────────────────────────────

    def run(self):
        expire_bars = int(PENDING_EXPIRE * (60 / 15))
        trade_id    = 0
        pending_at  = None
        signals_ok  = 0
        signals_rej = 0
        daily_blocks= 0

        if self.verbose:
            print(f"\n{C.BOLD}{C.CYAN}{bar()}{C.RESET}")
            print(f"{C.BOLD}{C.CYAN}  ▶ {self.symbol}  |  Backtest Start{C.RESET}")
            print(f"{C.CYAN}{bar()}{C.RESET}")
            print(f"  Period  : {self.df15.index[0]} → {self.df15.index[-1]}")
            print(f"  Candles : {len(self.df15)} (15m)  |  {len(self.df1h)} (1h)")
            print(f"  Balance : ${self.balance:.2f}  |  Lev: {LEVERAGE}x  |  Risk: {RISK_PCT*100:.1f}%")
            print(f"{C.CYAN}{bar('─')}{C.RESET}")

        for i in range(MIN_CANDLES, len(self.df15)):
            candle = self.df15.iloc[i]
            date   = candle.name.date()

            # Monitor active trade
            if self.active is not None:

                if self.active["status"] == "PENDING":
                    s, ep = self.active["side"], self.active["entry"]
                    filled = (s == "Long"  and candle["low"]  <= ep) or \
                             (s == "Short" and candle["high"] >= ep)
                    if (i - pending_at) > expire_bars:
                        self._log_expired(self.active, candle)
                        self.active = None
                        pending_at  = None
                        continue
                    if filled:
                        self.active["qty"]      = calc_qty(self.balance, ep)
                        self.active["status"]   = "OPEN"
                        self.active["entry_ts"] = candle.name
                        self._log_filled(self.active, candle)

                elif self.active["status"] == "OPEN":
                    self._monitor(self.active, candle)

                continue

            # Daily loss guard
            if self._daily_loss_reached(date):
                daily_blocks += 1
                continue

            # Try new signal
            trade_id += 1
            sig = self._generate_signal(i, candle, trade_id)
            if sig:
                self.active = sig
                pending_at  = i
                signals_ok += 1
                self._log_signal(sig)
            else:
                signals_rej += 1

        # Close any open position mark-to-market
        if self.active and self.active["status"] == "OPEN":
            last = self.df15.iloc[-1]
            ep   = self.active["entry"]
            rem  = remaining_qty(self.active["qty"],
                                 self.active["tp1_hit"],
                                 self.active["tp2_hit"])
            pnl  = calc_pnl(self.active["side"], ep, last["close"], rem)
            self._close(self.active, last, last["close"], pnl, "MTM Close")

        if self.verbose:
            print(f"\n{C.CYAN}{bar('─')}{C.RESET}")
            print(f"  {self.symbol} Summary  |  "
                  f"Signals valid={signals_ok}  "
                  f"rejected={signals_rej}  "
                  f"daily_blocked={daily_blocks}  "
                  f"trades={len(self.trades)}")
            print(f"{C.CYAN}{bar()}{C.RESET}")

        return {
            "signals_ok":  signals_ok,
            "signals_rej": signals_rej,
            "daily_blocks":daily_blocks,
        }


# ══════════════════════════════════════════════════════════════════════════════
# PER-SYMBOL REPORT
# ══════════════════════════════════════════════════════════════════════════════

def print_symbol_report(symbol: str, trades: list, initial: float, final: float):
    if not trades:
        print(f"\n  {C.YELLOW}⚠ {symbol}: Tidak ada trade yang dieksekusi{C.RESET}")
        return

    df = pd.DataFrame(trades).sort_values("exit_ts")
    total  = len(df)
    wins   = df[df["is_win"]]
    losses = df[~df["is_win"]]
    wr     = len(wins) / total * 100
    net    = df["total_pnl"].sum()
    ret    = (final - initial) / initial * 100

    avg_win  = wins["total_pnl"].mean()   if len(wins)   else 0
    avg_loss = losses["total_pnl"].mean() if len(losses) else 0
    pf       = abs(wins["total_pnl"].sum() / losses["total_pnl"].sum()) \
               if len(losses) > 0 and losses["total_pnl"].sum() != 0 else float("inf")

    # Drawdown
    bal = [initial] + list(df["balance_after"])
    peak, max_dd_pct = bal[0], 0.0
    for b in bal:
        if b > peak: peak = b
        dd = (peak - b) / peak * 100 if peak else 0
        if dd > max_dd_pct: max_dd_pct = dd

    print(f"\n{C.BOLD}{C.WHITE}{bar('═')}{C.RESET}")
    print(f"{C.BOLD}{C.WHITE}  📊  {symbol}  —  Per-Symbol Report{C.RESET}")
    print(f"{C.WHITE}{bar('─')}{C.RESET}")
    print(f"  {'Initial Balance':<30} ${initial:.2f}")
    print(f"  {'Final Balance':<30} ${final:.4f}  ({C.GREEN if ret>=0 else C.RED}{ret:+.2f}%{C.RESET})")
    print(f"  {'Net PnL':<30} {fmt_pnl(net)}")
    print(f"  {'Total Trades':<30} {total}  (W:{len(wins)} / L:{len(losses)})  WR:{wr:.1f}%")
    print(f"  {'Avg Win / Avg Loss':<30} {fmt_pnl(avg_win)} / {fmt_pnl(avg_loss)}")
    print(f"  {'Profit Factor':<30} {pf:.2f}")
    print(f"  {'Max Drawdown':<30} {max_dd_pct:.2f}%")
    print(f"{C.WHITE}{bar('─')}{C.RESET}")

    # Reason breakdown
    print(f"  {'Reason':<22}  {'N':>3}  {'PnL':>10}")
    for reason, cnt in df["reason"].value_counts().items():
        pnl_r = df[df["reason"] == reason]["total_pnl"].sum()
        print(f"  {reason:<22}  {cnt:>3}  {fmt_pnl(pnl_r)}")

    # Trade log
    print(f"{C.WHITE}{bar('─')}{C.RESET}")
    print(f"  {'#':>3}  {'Side':<6}  {'Pattern':<22}  {'Entry':>10}  {'Exit':>10}  {'PnL':>11}  {'Outcome':<14}  R:R")
    print(f"  {'─'*3}  {'─'*6}  {'─'*22}  {'─'*10}  {'─'*10}  {'─'*11}  {'─'*14}  {'─'*4}")
    for _, row in df.iterrows():
        icon = f"{C.GREEN}✅{C.RESET}" if row["is_win"] else f"{C.RED}❌{C.RESET}"
        print(
            f"  {int(row['id']):>3}  {row['side']:<6}  {row['pattern']:<22}  "
            f"{fmt_price(row['entry']):>10}  {fmt_price(row['exit']):>10}  "
            f"{fmt_pnl(row['total_pnl']):>11}  "
            f"{icon} {row['reason']:<12}  {row['rr']:.1f}"
        )
    print(f"{C.WHITE}{bar('═')}{C.RESET}")


# ══════════════════════════════════════════════════════════════════════════════
# COMBINED REPORT (semua simbol)
# ══════════════════════════════════════════════════════════════════════════════

def print_combined_report(all_results: list, initial_per_sym: float):
    """
    all_results: list of dicts {symbol, trades, initial, final}
    """
    print(f"\n\n{C.BOLD}{C.CYAN}{bar('═')}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}   🏁  COMBINED BACKTEST REPORT — All Symbols{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}   Config : {PROJECT_ROOT}/config.json{C.RESET}")
    print(f"{C.CYAN}{bar('═')}{C.RESET}")

    all_trades = []
    for r in all_results:
        all_trades.extend(r["trades"])

    if not all_trades:
        print(f"\n  {C.YELLOW}⚠ Tidak ada trade yang dieksekusi sama sekali.{C.RESET}")
        print(f"\n  Tips: Longgarkan filter di config.json:")
        print(f"    strategy.min_tech_score, min_smc_score, min_deriv_score")
        print(f"    strategy.risk_reward_min, indicators.min_rvol")
        return

    df_all = pd.DataFrame(all_trades).sort_values("exit_ts")
    total  = len(df_all)
    wins   = df_all[df_all["is_win"]]
    losses = df_all[~df_all["is_win"]]
    wr     = len(wins) / total * 100

    net_pnl  = df_all["total_pnl"].sum()
    avg_win  = wins["total_pnl"].mean()   if len(wins)   else 0
    avg_loss = losses["total_pnl"].mean() if len(losses) else 0
    pf       = abs(wins["total_pnl"].sum() / losses["total_pnl"].sum()) \
               if len(losses) > 0 and losses["total_pnl"].sum() != 0 else float("inf")
    exp      = (wr / 100 * avg_win) + ((1 - wr / 100) * avg_loss)

    total_initial = initial_per_sym * len(all_results)
    total_final   = sum(r["final"] for r in all_results)
    total_ret     = (total_final - total_initial) / total_initial * 100

    # Max consecutive
    maxw = maxl = cw = cl = 0
    for w in df_all["is_win"]:
        if w:   cw += 1; cl = 0
        else:   cl += 1; cw = 0
        maxw = max(maxw, cw); maxl = max(maxl, cl)

    # Sharpe
    sharpe = 0.0
    if len(df_all) > 1 and df_all["total_pnl"].std() > 0:
        sharpe = (df_all["total_pnl"].mean() / df_all["total_pnl"].std()) * (252 ** 0.5)

    # ── Performance Summary ────────────────────────────────────────────────
    print(f"\n{'PERFORMANCE SUMMARY':^72}")
    print(pbar())
    print(f"  {'Capital (per symbol)':<38} ${initial_per_sym:.2f}  ×  {len(all_results)} symbols")
    print(f"  {'Total Initial Capital':<38} ${total_initial:.2f}")
    print(f"  {'Total Final Capital':<38} ${total_final:.4f}")
    print(f"  {'Total Net PnL':<38} {fmt_pnl(net_pnl)}")
    print(f"  {'Total Net Return':<38} {C.GREEN if total_ret>=0 else C.RED}{total_ret:+.2f}%{C.RESET}")
    print(pbar())

    # ── Trade Statistics ──────────────────────────────────────────────────
    print(f"\n{'TRADE STATISTICS (COMBINED)':^72}")
    print(pbar())
    print(f"  {'Total Trades':<38} {total}")
    print(f"  {'Wins / Losses':<38} {len(wins)} / {len(losses)}")
    print(f"  {'Win Rate':<38} {wr:.1f}%")
    print(f"  {'Avg Win':<38} {fmt_pnl(avg_win)}")
    print(f"  {'Avg Loss':<38} {fmt_pnl(avg_loss)}")
    print(f"  {'Profit Factor':<38} {pf:.2f}")
    print(f"  {'Expectancy per Trade':<38} {fmt_pnl(exp)}")
    print(f"  {'Sharpe Ratio (simplified)':<38} {sharpe:.2f}")
    print(f"  {'Max Consecutive Wins':<38} {maxw}")
    print(f"  {'Max Consecutive Losses':<38} {maxl}")
    print(pbar())

    # ── Per-Symbol Summary ────────────────────────────────────────────────
    print(f"\n{'PER-SYMBOL SUMMARY':^72}")
    print(pbar())
    print(f"  {'Symbol':<14}  {'Trades':>6}  {'WR%':>5}  {'Net PnL':>10}  {'Final Bal':>10}  {'Return%':>8}  {'Max DD%':>7}")
    print(f"  {'─'*14}  {'─'*6}  {'─'*5}  {'─'*10}  {'─'*10}  {'─'*8}  {'─'*7}")

    for r in sorted(all_results, key=lambda x: x["symbol"]):
        sym = r["symbol"]
        t   = r["trades"]
        if not t:
            print(f"  {sym:<14}  {'N/A':>6}  {'—':>5}  {'—':>10}  {'—':>10}  {'—':>8}  {'—':>7}")
            continue
        df_s   = pd.DataFrame(t)
        wr_s   = df_s["is_win"].sum() / len(df_s) * 100
        pnl_s  = df_s["total_pnl"].sum()
        ret_s  = (r["final"] - r["initial"]) / r["initial"] * 100
        bal_s  = [r["initial"]] + list(df_s["balance_after"])
        pk, dd = bal_s[0], 0.0
        for b in bal_s:
            if b > pk: pk = b
            d = (pk - b) / pk * 100 if pk else 0
            if d > dd: dd = d
        pnl_col = C.GREEN if pnl_s >= 0 else C.RED
        ret_col = C.GREEN if ret_s >= 0 else C.RED
        print(
            f"  {sym:<14}  {len(df_s):>6}  {wr_s:>4.1f}%  "
            f"{pnl_col}${pnl_s:>+9.4f}{C.RESET}  "
            f"${r['final']:>9.4f}  "
            f"{ret_col}{ret_s:>+7.2f}%{C.RESET}  "
            f"{dd:>6.2f}%"
        )
    print(pbar())

    # ── Outcome Breakdown ─────────────────────────────────────────────────
    print(f"\n{'OUTCOME BREAKDOWN (ALL SYMBOLS)':^72}")
    print(pbar())
    for reason, cnt in df_all["reason"].value_counts().items():
        pnl_r = df_all[df_all["reason"] == reason]["total_pnl"].sum()
        print(f"  {reason:<22}  {cnt:>4} trades ({cnt/total*100:4.1f}%)   PnL: {fmt_pnl(pnl_r)}")
    print(pbar())

    # ── Pattern Breakdown ─────────────────────────────────────────────────
    print(f"\n{'PATTERN BREAKDOWN (ALL SYMBOLS)':^72}")
    print(pbar())
    print(f"  {'Pattern':<26}  {'N':>4}  {'WR%':>5}  {'Total PnL':>11}  {'Avg PnL':>10}")
    print(f"  {'─'*26}  {'─'*4}  {'─'*5}  {'─'*11}  {'─'*10}")
    for pat, grp in df_all.groupby("pattern"):
        w_  = grp["is_win"].sum()
        wr_ = w_ / len(grp) * 100
        tp_ = grp["total_pnl"].sum()
        ap_ = grp["total_pnl"].mean()
        print(f"  {pat:<26}  {len(grp):>4}  {wr_:>4.1f}%  {fmt_pnl(tp_):>11}  {fmt_pnl(ap_):>10}")
    print(pbar())

    # ── Side Breakdown ────────────────────────────────────────────────────
    print(f"\n{'SIDE BREAKDOWN':^72}")
    print(pbar())
    for side in ["Long", "Short"]:
        g = df_all[df_all["side"] == side]
        if g.empty: continue
        pnl_g = g["total_pnl"].sum()
        wr_g  = g["is_win"].sum() / len(g) * 100
        print(f"  {side:<7}  {len(g):>4} trades  WR: {wr_g:.1f}%  "
              f"PnL: {fmt_pnl(pnl_g)}")
    print(pbar())

    # ── Timeline: Monthly PnL ─────────────────────────────────────────────
    df_all["month"] = df_all["exit_ts"].dt.to_period("M")
    monthly = df_all.groupby("month")["total_pnl"].sum()
    if len(monthly) > 0:
        print(f"\n{'MONTHLY PnL (ALL SYMBOLS)':^72}")
        print(pbar())
        for m, v in monthly.items():
            bar_len = int(abs(v) / max(monthly.abs().max(), 0.001) * 30)
            bar_str = ("█" * bar_len) if v >= 0 else ("▓" * bar_len)
            col     = C.GREEN if v >= 0 else C.RED
            print(f"  {str(m):<10}  {col}{bar_str:<30}{C.RESET}  {fmt_pnl(v)}")
        print(pbar())

    print(f"\n{C.CYAN}{bar('═')}{C.RESET}")
    print(f"{C.BOLD}  ✅  Backtest Complete — {len(all_results)} symbols processed{C.RESET}")
    print(f"{C.CYAN}{bar('═')}{C.RESET}\n")


# ══════════════════════════════════════════════════════════════════════════════
# DATA LOADER — load SEMUA kolom dataset
# ══════════════════════════════════════════════════════════════════════════════

def load_csv(path: str) -> pd.DataFrame:
    """
    Load CSV dengan SEMUA kolom dataset.
    Kolom OHLCV + indikator pre-computed (adx, rsi, macd, bb_*, atr_*, ema_*,
    taker_buy_volume, volume_quote, trades, taker_buy_quote).
    Index = datetime dari timestamp_open (ms).
    """
    df = pd.read_csv(path)

    # Konversi timestamp → datetime index
    df["datetime"] = pd.to_datetime(df["timestamp_open"], unit="ms")
    df = df.set_index("datetime").sort_index()

    # Kolom OHLCV wajib (float)
    ohlcv = ["open", "high", "low", "close", "volume"]

    # Kolom tambahan dari dataset (pre-computed indicators)
    extra = [
        "volume_quote", "trades",
        "taker_buy_volume", "taker_buy_quote",
        "adx", "rsi", "macd", "macd_signal",
        "bb_upper", "bb_middle", "bb_lower",
        "atr_prev", "atr_last",
        "ema_short", "ema_long",
    ]

    # Ambil kolom yang tersedia
    available = [c for c in ohlcv + extra if c in df.columns]
    df = df[available].astype(float)

    return df


# ══════════════════════════════════════════════════════════════════════════════
# SYMBOL DISCOVERY — scan folder dataset/
# ══════════════════════════════════════════════════════════════════════════════

def discover_symbols(dataset_dir: str) -> list[tuple[str, str, str]]:
    """
    Scan dataset/ untuk semua pasangan *_15m.csv & *_1h.csv.
    Return list of (symbol, path_15m, path_1h).
    """
    pattern = os.path.join(dataset_dir, "*_15m.csv")
    files   = sorted(glob.glob(pattern))
    pairs   = []
    for f15 in files:
        base   = os.path.basename(f15).replace("_15m.csv", "")
        f1h    = f15.replace("_15m.csv", "_1h.csv")
        if not os.path.exists(f1h):
            print(f"  {C.YELLOW}⚠ {base}: _1h.csv tidak ditemukan, skip.{C.RESET}")
            continue
        pairs.append((base, f15, f1h))
    return pairs


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    t0 = time.time()

    # ── Header ────────────────────────────────────────────────────────────────
    print(f"\n{C.BOLD}{C.CYAN}{bar('═')}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}   🚀  BACKTEST MULTI-COIN ENGINE — Bot Auto Screening Bybit{C.RESET}")
    print(f"{C.CYAN}{bar('═')}{C.RESET}")
    print(f"   Config     : {PROJECT_ROOT}/config.json")
    print(f"   Dataset dir: {DATASET_DIR}")
    print(f"   Leverage   : {LEVERAGE}x  |  Risk/trade : {RISK_PCT*100:.1f}%")
    print(f"   Balance    : ${INITIAL_BAL:.2f} per symbol")
    print(f"   Fee Bybit  : {BYBIT_FEE*100:.3f}% taker per sisi")
    print(f"   TP split   : {TP1_PCT*100:.0f}% / {TP2_PCT*100:.0f}% / {TP3_PCT*100:.0f}%")
    print(f"   Min R:R    : {MIN_RR}  |  Max drift : {MAX_DRIFT*100:.0f}%")
    print(f"   Min scores : SMC={MIN_SMC}  Tech={MIN_TECH}  Deriv={MIN_DERIV}  RVOL={MIN_RVOL}x")
    print(f"   Pending exp: {PENDING_EXPIRE}h  |  Max daily loss: {MAX_DAILY_LOSS*100:.0f}%")
    print(f"{C.CYAN}{bar('═')}{C.RESET}")

    # ── Discover symbols ──────────────────────────────────────────────────────
    symbols = discover_symbols(DATASET_DIR)
    if not symbols:
        print(f"\n{C.RED}❌ Tidak ada dataset ditemukan di {DATASET_DIR}{C.RESET}")
        sys.exit(1)

    print(f"\n  {C.GREEN}✓ {len(symbols)} symbol ditemukan:{C.RESET}")
    for sym, f15, f1h in symbols:
        sz15 = os.path.getsize(f15) / 1024
        sz1h = os.path.getsize(f1h) / 1024
        print(f"    • {sym:<14}  15m: {sz15:>7.1f} KB   1h: {sz1h:>7.1f} KB")

    # ── Run per-symbol ────────────────────────────────────────────────────────
    all_results = []

    for sym, f15, f1h in symbols:

        print(f"\n{C.DIM}📂 Loading {sym}…{C.RESET}")
        df15 = load_csv(f15)
        df1h = load_csv(f1h)
        print(f"   15m: {len(df15)} candles  {df15.index[0]} → {df15.index[-1]}")
        print(f"   1h : {len(df1h)} candles  {df1h.index[0]} → {df1h.index[-1]}")
        print(f"   Columns: {list(df15.columns)}")

        engine = BacktestEngine(sym, df15, df1h,
                                initial_balance=INITIAL_BAL,
                                verbose=True)
        stats = engine.run()

        # Per-symbol report
        print_symbol_report(sym, engine.trades, INITIAL_BAL, engine.balance)

        # Save per-symbol CSV
        if engine.trades:
            out_csv = os.path.join(OUTPUT_DIR, f"backtest_{sym}.csv")
            pd.DataFrame(engine.trades).sort_values("exit_ts").to_csv(out_csv, index=False)
            print(f"  {C.DIM}💾 Saved: {out_csv}{C.RESET}")

        all_results.append({
            "symbol": sym,
            "trades": engine.trades,
            "initial": INITIAL_BAL,
            "final":   engine.balance,
            "stats":   stats,
        })

    # ── Combined report ───────────────────────────────────────────────────────
    print_combined_report(all_results, INITIAL_BAL)

    # Save combined CSV
    all_trades_flat = []
    for r in all_results:
        all_trades_flat.extend(r["trades"])
    if all_trades_flat:
        combined_csv = os.path.join(OUTPUT_DIR, "backtest_combined.csv")
        pd.DataFrame(all_trades_flat).sort_values("exit_ts").to_csv(combined_csv, index=False)
        print(f"  {C.GREEN}💾 Combined CSV: {combined_csv}{C.RESET}")

    elapsed = time.time() - t0
    print(f"\n  ⏱  Total runtime: {elapsed:.1f}s\n")


if __name__ == "__main__":
    main()
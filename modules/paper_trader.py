"""
Paper Trader — simulates order fills, TP hits, SL hits, and PnL
without placing any real orders on OKX.

PnL Formula (BENAR — seperti exchange asli):
    margin         = balance * risk%               ← uang yang dipertaruhkan
    position_value = margin × leverage             ← notional size
    quantity       = position_value / entry_price  ← jumlah koin (sudah bake-in leverage)
    pnl (Long)     = (exit - entry) × quantity     ← TIDAK perlu × leverage lagi!
    pnl (Short)    = (entry - exit) × quantity

Contoh — Balance $100, Risk 1%, Lev 50x, LINK entry 9.6084, SL 9.4868:
    margin   = $100 × 1%     = $1.00
    pos val  = $1 × 50       = $50.00
    qty      = $50 / 9.6084  = 5.203 LINK
    pnl (SL) = (9.4868 - 9.6084) × 5.203 = -$0.63

Partial TP Logic:
    TP1 hit → jual 30%, balance update realtime, SL → Breakeven (entry)
    TP2 hit → jual 30%, balance update realtime, SL → TP1 price
    TP3 hit → tutup sisa 40%, balance update, trade CLOSED

TP generation:
    Target TP diasumsikan sudah berbasis fixed R multiple dari SL:
      TP1 = 1R, TP2 = 2R, TP3 = 3R
    Jika payload lama tidak membawa level TP yang valid, helper fallback
    akan menghitung ulang dari entry dan SL.

Cascade TP (skip detection):
    Jika harga loncat langsung ke TP3 (skip TP1/TP2), proses TP1 & TP2 partial
    terlebih dahulu (dengan harga TP masing-masing), baru tutup di TP3.
"""

import logging
from datetime import datetime, timezone
from modules.config_loader import CONFIG
from modules.database import (
    get_paper_balance,
    update_paper_balance,
    add_paper_balance,
    update_active_trade,
    get_active_trades_by_status,
    record_trade_close_outcomes,
)
from modules.notifier import send_reply
from modules.smart_entry import (
    VOLUME_CONFIRM_ENABLED as ENTRY_VOLUME_CONFIRM_ENABLED,
    confirm_entry_with_volume,
)

logger = logging.getLogger("PaperTrader")

# FIX #2: Rename dari TP1_PCT → TP1_CLOSE_PCT untuk kejelasan.
#
# Masalah lama: nama TP1_PCT ambigu — bisa dibaca sebagai "target price TP1"
# padahal artinya "porsi posisi yang DIJUAL saat TP1 hit".
# Config tp_split: [0.4, 0.3, 0.3] = tutup 40% di TP1, 30% di TP2, 30% di TP3.
# Total harus 100% (0.4 + 0.3 + 0.3 = 1.0).
TP1_CLOSE_PCT, TP2_CLOSE_PCT, TP3_CLOSE_PCT = CONFIG["risk"].get("tp_split", [0.4, 0.3, 0.3])

# ─── ADVANCED 4/4: Chandelier trail post-TP2 ─────────────────────────────────
# After TP2 fills the remaining 30% runs as a runner. Default trail to TP1
# (existing behaviour) was correct but capped profit at 2R even when the move
# was 5R+. Chandelier exit lets the runner ride extended moves: SL re-anchors
# to the highest-high (Long) / lowest-low (Short) seen since entry, minus a
# multiple of the trade's risk distance R.
#
#   trail_distance = chandelier_trail_r_mult × |entry − sl_original|
#   Long  trail SL = highest_since_entry − trail_distance
#   Short trail SL = lowest_since_entry  + trail_distance
#
# Ratchet rule: trail SL only moves in the profit direction.
#   Long  → trail SL only goes UP   (max with previous trail and tp1 floor)
#   Short → trail SL only goes DOWN (min with previous trail and tp1 ceiling)
#
# Default `chandelier_trail_r_mult = 1.33` ≈ 2 × ATR when SL = 1.5 × ATR
# (industry standard chandelier exit). Configurable via strategy config.
_PAPER_STRATEGY            = CONFIG.get("strategy", {})
CHANDELIER_TRAIL_ENABLED   = bool(_PAPER_STRATEGY.get("chandelier_trail_enabled", True))
CHANDELIER_TRAIL_R_MULT    = float(_PAPER_STRATEGY.get("chandelier_trail_r_mult", 1.33))

# ─── ANOMALY HARDENING (fix G + H) ────────────────────────────────────────────
# Real OKX fills are not at the exact mark price. Simulate a configurable
# slippage + spread cost so paper PnL is not optimistic vs production.
# Defaults: 5 bps slippage, 2 bps spread, reject fill if spread > 50 bps.
_PAPER_RISK            = CONFIG.get("risk", {})
PAPER_SLIPPAGE_BPS     = float(_PAPER_RISK.get("paper_slippage_bps",    5.0))
PAPER_SPREAD_BPS       = float(_PAPER_RISK.get("paper_spread_bps",     2.0))
PAPER_MAX_SPREAD_BPS   = float(_PAPER_RISK.get("paper_max_spread_bps", 50.0))


def _bps(p: float, b: float) -> float:
    """Return absolute price equivalent of `b` basis points on price `p`."""
    return p * (b / 10_000.0)


def _apply_entry_slippage(side: str, price: float) -> float:
    """
    Fix G — Long entries fill slightly higher than the touch, Short slightly
    lower (taker semantics). Combines slippage + half-spread.
    """
    cost = _bps(price, PAPER_SLIPPAGE_BPS) + _bps(price, PAPER_SPREAD_BPS / 2.0)
    return price + cost if side == "Long" else price - cost


def _apply_exit_slippage(side: str, price: float, is_loss: bool) -> float:
    """
    Fix G — Always degrade the exit price by slippage. For losing exits the
    slippage is on the unfavourable side; for winning exits the maker-style
    fill nibbles a small amount as well (still configured by SLIPPAGE_BPS).
    """
    cost = _bps(price, PAPER_SLIPPAGE_BPS) + _bps(price, PAPER_SPREAD_BPS / 2.0)
    if side == "Long":
        return price - cost            # Long fill at exit → lower (worse for win, worse for SL)
    return price + cost                # Short fill at exit → higher (worse for win, worse for SL)


# ─── UI Helpers ───────────────────────────────────────────────────────────────

SEP  = "━━━━━━━━━━━━━━━━━━━━━━━"
SEP2 = "─────────────────────────"


def _fp(v):
    """Format price — strips trailing zeros, adapts precision."""
    f = float(v)
    if f == 0:       return "0"
    if f < 0.001:    return f"{f:.8f}".rstrip('0').rstrip('.')
    if f < 1:        return f"{f:.6f}".rstrip('0').rstrip('.')
    if f < 100:      return f"{f:.4f}".rstrip('0').rstrip('.')
    return f"{f:.2f}"


def _pct_price(a, b) -> str:
    a, b = float(a), float(b)
    if a == 0:
        return ""
    return f"{((b - a) / a) * 100:+.2f}%"

_pct = _pct_price


def _roi_on_margin(pnl: float, margin: float) -> str:
    if margin <= 0:
        return ""
    return f"{(pnl / margin) * 100:+.2f}%"


def _roi_on_balance(pnl: float, balance: float) -> str:
    if balance <= 0:
        return ""
    return f"{(pnl / balance) * 100:+.2f}%"


def _calc_margin(entry: float, qty: float, lev: int) -> float:
    if lev <= 0:
        return entry * qty
    return (entry * qty) / lev


def _side_label(side: str) -> str:
    return "🚀 LONG" if side == 'Long' else "🔻 SHORT"


def _build_rr_targets(entry: float, sl: float, side: str):
    """Fallback TP generator: TP1=1R, TP2=2R, TP3=3R."""
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0, 0.0, 0.0
    if side == 'Long':
        return entry + risk, entry + risk * 2.0, entry + risk * 3.0
    return entry - risk, entry - risk * 2.0, entry - risk * 3.0


def _normalize_rr_targets(trade: dict):
    """Return TP levels, rebuilding them when payload is incomplete."""
    entry = float(trade['entry_price'])
    sl    = float(trade['sl_price'])
    side  = trade['side']

    tp1 = float(trade.get('tp1', 0) or 0)
    tp2 = float(trade.get('tp2', 0) or 0)
    tp3 = float(trade.get('tp3', 0) or 0)
    if tp1 > 0 and tp2 > 0 and tp3 > 0:
        return tp1, tp2, tp3
    return _build_rr_targets(entry, sl, side)


# ─── Core PnL Formula ─────────────────────────────────────────────────────────

def _calc_pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    """
    Hitung PnL realized dengan fee.
    qty sudah mengandung leverage, TIDAK perlu dikalikan lagi.
    """
    if side == 'Long':
        raw_pnl = (exit_price - entry) * qty
    else:
        raw_pnl = (entry - exit_price) * qty

    # OKX taker fee 0.05% open + 0.05% close
    fee = (entry * qty * 0.00055) + (exit_price * qty * 0.00055)
    return round(raw_pnl - fee, 4)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _remaining_qty(qty: float, tp1_logged: bool, tp2_logged: bool) -> float:
    """
    Sisa qty setelah partial TP:
      TP1 hit → jual sesuai risk.tp_split[0]
      TP2 hit → jual sesuai risk.tp_split[1]
    """
    remaining = qty
    if tp1_logged:
        remaining -= qty * TP1_CLOSE_PCT
    if tp2_logged:
        remaining -= qty * TP2_CLOSE_PCT
    return round(remaining, 8)


def _chandelier_trail_distance(trade: dict) -> float:
    """Compute the chandelier offset in price terms.

    Uses the trade's original risk distance R = |entry − sl_original| scaled
    by ``CHANDELIER_TRAIL_R_MULT`` (default 1.33, ≈ 2×ATR when the original
    SL is 1.5×ATR). Returns 0 if R cannot be derived (caller should treat as
    "no chandelier trail available" and fall back to TP1 trail).
    """
    entry = float(trade.get('entry_price', 0) or 0)
    sl0   = float(trade.get('sl_price', 0) or 0)
    R     = abs(entry - sl0)
    if R <= 0:
        return 0.0
    return R * CHANDELIER_TRAIL_R_MULT


def _chandelier_sl(trade: dict) -> "float | None":
    """Return chandelier-trail SL price, or ``None`` when not applicable.

    Pre-conditions:
      * Chandelier feature enabled (config flag)
      * Trade has crossed TP2 (``chandelier_active`` flag set, or legacy
        ``_tp2_logged`` flag with extreme already populated)
      * highest_since_entry / lowest_since_entry has been recorded

    The trail floor is the existing TP1 level — never trail tighter than the
    TP1-trail behaviour the prior implementation provided.
    """
    if not CHANDELIER_TRAIL_ENABLED:
        return None
    if not trade.get('chandelier_active'):
        return None

    side  = trade.get('side', 'Long')
    tp1   = float(trade.get('tp1', 0) or 0)
    dist  = _chandelier_trail_distance(trade)
    if dist <= 0:
        return None

    if side == 'Long':
        ext = trade.get('highest_since_entry')
        if ext is None:
            return None
        trail = float(ext) - dist
        # Floor: never tighter than TP1
        return max(trail, tp1) if tp1 > 0 else trail

    # Short
    ext = trade.get('lowest_since_entry')
    if ext is None:
        return None
    trail = float(ext) + dist
    # Ceiling for Short: never tighter than TP1 (which is below entry)
    return min(trail, tp1) if tp1 > 0 else trail


def _effective_sl(trade: dict) -> float:
    """
    SL efektif berdasarkan trailing status:
      - Belum kena TP1   → SL original
      - TP1 hit          → SL = entry (breakeven)
      - TP2 hit          → SL = TP1 price (legacy) atau chandelier trail
                                (chandelier overrides TP1 once it surpasses it)
    """
    if trade.get('_tp2_logged'):
        chand = _chandelier_sl(trade)
        tp1_trail = float(trade.get('tp1', trade.get('entry_price', 0)))
        if chand is None:
            return tp1_trail
        # Ratchet: prefer the tighter (more profit-protecting) of chand vs tp1_trail
        # which was already enforced by the floor inside _chandelier_sl, so we
        # can return chand directly.
        return float(chand)
    if trade.get('is_sl_moved'):
        return float(trade.get('entry_price', 0))  # breakeven
    return float(trade.get('sl_price', 0))         # original


def _apply_partial_balance(pnl: float, label: str, symbol: str) -> float:
    """
    Atomically apply +pnl to paper balance. Uses SQLite UPDATE ... balance = balance + ?
    so two trades closing concurrently never lose updates (fix #1B).
    """
    new_balance = add_paper_balance(pnl)
    logger.info(
        f"💰 [PAPER] {symbol} {label} partial — PnL: ${pnl:+.4f} "
        f"| Balance: ${new_balance:.2f}"
    )
    return new_balance


# ─── PAPER EXECUTE (Entry Fill) ───────────────────────────────────────────────

def paper_execute(trade: dict, current_price: float, *, client=None) -> bool:
    """
    Simulasi fill entry untuk PENDING paper trade.
    Returns True jika terisi, False jika masih menunggu.

    When ``ENTRY_VOLUME_CONFIRM_ENABLED`` and a ``client`` is provided
    (Smart Entry C.2), the fill is gated on the most recent CLOSED
    bar passing both an RVOL ≥ ``min_rvol`` check and a
    rejection-wick check. If the bar fails either gate the trade
    stays PENDING and we re-evaluate on the next tick. The bar
    timestamp is persisted to ``entry_confirmed_bar_ts`` so we don't
    re-confirm the same bar across ticks.
    """
    side  = trade['side']
    entry = float(trade['entry_price'])
    tg_id = trade.get('telegram_msg_id')
    tp1, tp2, tp3 = _normalize_rr_targets(trade)

    filled = (side == 'Long'  and current_price <= entry) or \
             (side == 'Short' and current_price >= entry)

    if not filled:
        return False

    # ─── Smart Entry C.2: Volume confirmation gate ──────────────────────
    # When client is unavailable (legacy callers / tests) we skip
    # confirmation entirely and behave like before.
    confirmed_at_ts: str | None = None
    confirmed_bar_ts: int | None = None
    if ENTRY_VOLUME_CONFIRM_ENABLED and client is not None:
        last_evaluated_bar = trade.get("entry_confirmed_bar_ts")
        timeframe          = trade.get("timeframe") or CONFIG.get("system", {}).get("entry_timeframe", "15m")
        passed, bar_ts, reason = confirm_entry_with_volume(
            client,
            symbol=trade["symbol"],
            side=side,
            timeframe=timeframe,
            entry_price=entry,
            last_confirmed_bar_ts=int(last_evaluated_bar) if last_evaluated_bar else None,
        )
        if not passed:
            # Persist bar_ts so we skip re-checking the same bar; only
            # update if changed (avoids needless writes).
            if bar_ts is not None and bar_ts != last_evaluated_bar:
                update_active_trade(trade["id"], {"entry_confirmed_bar_ts": int(bar_ts)})
            logger.debug(
                f"[PAPER] {trade['symbol']} entry confirmation failed: {reason} "
                f"(bar_ts={bar_ts})"
            )
            return False
        confirmed_bar_ts = bar_ts
        confirmed_at_ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(
            f"📋 [PAPER] {trade['symbol']} entry confirmed: {reason} "
            f"(bar_ts={bar_ts})"
        )

    # Fix G — simulate realistic taker fill (slippage + half-spread).
    fill_price = _apply_entry_slippage(side, current_price)
    spread_bps = abs(fill_price - current_price) / max(current_price, 1e-9) * 10_000.0
    if spread_bps > PAPER_MAX_SPREAD_BPS:
        logger.warning(
            f"[PAPER] {trade['symbol']} reject fill: simulated spread "
            f"{spread_bps:.1f}bps > max {PAPER_MAX_SPREAD_BPS:.0f}bps"
        )
        return False

    fill_updates: dict = {"status": "OPEN", "entry_price": fill_price}
    if confirmed_at_ts is not None:
        fill_updates["entry_confirmed_at"]     = confirmed_at_ts
        fill_updates["entry_confirmed_bar_ts"] = int(confirmed_bar_ts) if confirmed_bar_ts else None
    update_active_trade(trade['id'], fill_updates)
    logger.info(
        f"📋 [PAPER] Entry filled: {trade['symbol']} {side} @ {fill_price} "
        f"(touch {current_price}, slippage {spread_bps:.1f}bps)"
    )
    current_price = fill_price

    qty     = float(trade.get('quantity', 0))
    lev     = int(trade.get('leverage', 1))
    margin  = _calc_margin(current_price, qty, lev)
    pos_val = current_price * qty

    sl_price_pct  = _pct_price(current_price, trade['sl_price'])
    tp1_price_pct = _pct_price(current_price, tp1)
    tp3_price_pct = _pct_price(current_price, tp3)

    sl_pnl  = _calc_pnl(side, current_price, float(trade['sl_price']), qty)
    tp1_pnl = _calc_pnl(side, current_price, float(tp1), qty)
    tp3_pnl = _calc_pnl(side, current_price, float(tp3), qty)

    sl_roi  = _roi_on_margin(sl_pnl,  margin)
    tp1_roi = _roi_on_margin(tp1_pnl, margin)
    tp3_roi = _roi_on_margin(tp3_pnl, margin)

    msg = (
        f"{SEP}\n"
        f"📋 <b>[PAPER] Entry Filled</b>\n"
        f"{SEP}\n\n"
        f"📌 <b>{trade['symbol']}</b>  {_side_label(side)}\n"
        f"💰 Fill    <code>{_fp(current_price)}</code>\n\n"
        f"🛑 Stop    <code>{_fp(trade['sl_price'])}</code>  "
            f"<i>({sl_price_pct} · ROI {sl_roi})</i>\n"
        f"🎯 TP1     <code>{_fp(tp1)}</code>  "
            f"<i>({tp1_price_pct} · ROI {tp1_roi})</i>\n"
        f"🎯 TP2     <code>{_fp(tp2)}</code>\n"
        f"🎯 TP3     <code>{_fp(tp3)}</code>  "
            f"<i>({tp3_price_pct} · ROI {tp3_roi})</i>\n\n"
        f"{SEP2}\n"
        f"💼 Margin  <code>${margin:.2f}</code>  ·  "
            f"Pos  <code>${pos_val:.2f}</code>  ·  "
            f"Lev  <code>{lev}x</code>\n"
        f"<i>🕐 {datetime.now().strftime('%H:%M:%S')}</i>"
    )
    send_reply(msg, reply_to_message_id=tg_id)
    return True


# ─── PARTIAL TP HANDLERS ──────────────────────────────────────────────────────

def _handle_tp1_partial(trade: dict, cascade: bool = False) -> float:
    """
    Partial close di TP1 sesuai risk.tp_split[0].
    ✅ Update balance realtime
    ✅ Set SL ke Breakeven (entry)
    Returns partial PnL.
    """
    t_id   = trade['id']
    sym    = trade['symbol']
    side   = trade['side']
    entry  = float(trade['entry_price'])
    tp1, tp2, tp3 = _normalize_rr_targets(trade)
    qty    = float(trade['quantity'])
    lev    = int(trade.get('leverage', 1))
    tg_id  = trade.get('telegram_msg_id')

    partial_qty    = qty * TP1_CLOSE_PCT
    partial_pnl    = _calc_pnl(side, entry, tp1, partial_qty)
    full_margin    = _calc_margin(entry, qty, lev)
    partial_margin = full_margin * TP1_CLOSE_PCT

    # ✅ Balance update realtime
    new_balance = _apply_partial_balance(partial_pnl, "TP1", sym)

    # ✅ SL → Breakeven, flag TP1 processed
    update_active_trade(t_id, {
        "is_sl_moved": True,
        "_tp1_logged": True,
    })
    logger.info(f"♻️  [PAPER] {sym} TP1 → SL dipindah ke Breakeven @ {entry}")

    cascade_note = "\n⚡ <i>Cascade: harga skip ke TP2/TP3</i>" if cascade else ""

    msg = (
        f"{SEP}\n"
        f"♻️ <b>[PAPER] TP1 Hit — Breakeven Set</b>\n"
        f"{SEP}\n\n"
        f"📌 <b>{sym}</b>  {_side_label(side)}\n"
        f"📈 TP1  <code>{_fp(tp1)}</code>  "
            f"<i>({_pct_price(entry, tp1)} dari entry)</i>\n\n"
        f"💰 Partial PnL   <code>${partial_pnl:+.4f}</code>  <i>({TP1_CLOSE_PCT*100:.0f}% posisi)</i>\n"
        f"📊 ROI on margin <code>{_roi_on_margin(partial_pnl, partial_margin)}</code>  "
            f"<i>(dari ${partial_margin:.2f})</i>\n"
        f"💼 Balance       <code>${new_balance:.2f}</code>\n"
        f"🔒 Stop → Breakeven @ <code>{_fp(entry)}</code>\n"
        f"⏳ Holding {(1-TP1_CLOSE_PCT)*100:.0f}% ke TP2 / TP3{cascade_note}\n\n"
        f"<i>🕐 {datetime.now().strftime('%H:%M:%S')}</i>"
    )
    send_reply(msg, reply_to_message_id=tg_id)
    return partial_pnl


def _handle_tp2_partial(trade: dict, cascade: bool = False) -> float:
    """
    Partial close di TP2 sesuai risk.tp_split[1].
    ✅ Update balance realtime
    ✅ Set SL ke TP1 price (trailing stop)
    Returns partial PnL.
    """
    t_id   = trade['id']
    sym    = trade['symbol']
    side   = trade['side']
    entry  = float(trade['entry_price'])
    tp1, tp2, tp3 = _normalize_rr_targets(trade)
    qty    = float(trade['quantity'])
    lev    = int(trade.get('leverage', 1))
    tg_id  = trade.get('telegram_msg_id')

    partial_qty    = qty * TP2_CLOSE_PCT
    partial_pnl    = _calc_pnl(side, entry, tp2, partial_qty)
    full_margin    = _calc_margin(entry, qty, lev)
    partial_margin = full_margin * TP2_CLOSE_PCT

    # ✅ Balance update realtime
    new_balance = _apply_partial_balance(partial_pnl, "TP2", sym)

    # ✅ SL → TP1 price (trail), flag TP2 processed.
    # Advanced 4/4: also activate chandelier trail and seed the running
    # extreme with the current TP2 price (the price we just filled at).
    updates = {
        "status": "OPEN_TPS_SET",
        "_tp2_logged": True,
        # effective_sl() akan baca tp1 field secara otomatis saat _tp2_logged=True
    }
    if CHANDELIER_TRAIL_ENABLED:
        updates["chandelier_active"] = True
        if side == 'Long':
            updates["highest_since_entry"] = float(tp2)
            trade["highest_since_entry"]   = float(tp2)
        else:
            updates["lowest_since_entry"]  = float(tp2)
            trade["lowest_since_entry"]    = float(tp2)
        trade["chandelier_active"] = True
    update_active_trade(t_id, updates)
    trade["_tp2_logged"] = True

    chand_note = ""
    if CHANDELIER_TRAIL_ENABLED:
        chand_note = (
            f"\n🪜 Chandelier trail ACTIVE — runner SL akan ratchet "
            f"{CHANDELIER_TRAIL_R_MULT:.2f}R di belakang harga tertinggi"
        )
    logger.info(
        f"🎯 [PAPER] {sym} TP2 → SL dipindah ke TP1 @ {tp1}"
        + (" + chandelier trail aktif" if CHANDELIER_TRAIL_ENABLED else "")
    )

    cascade_note = "\n⚡ <i>Cascade: harga skip ke TP3</i>" if cascade else ""

    msg = (
        f"{SEP}\n"
        f"🎯 <b>[PAPER] TP2 Hit — SL Trail ke TP1</b>\n"
        f"{SEP}\n\n"
        f"📌 <b>{sym}</b>  {_side_label(side)}\n"
        f"📈 TP2  <code>{_fp(tp2)}</code>  "
            f"<i>({_pct_price(entry, tp2)} dari entry)</i>\n\n"
        f"💰 Partial PnL   <code>${partial_pnl:+.4f}</code>  <i>({TP2_CLOSE_PCT*100:.0f}% posisi)</i>\n"
        f"📊 ROI on margin <code>{_roi_on_margin(partial_pnl, partial_margin)}</code>  "
            f"<i>(dari ${partial_margin:.2f})</i>\n"
        f"💼 Balance       <code>${new_balance:.2f}</code>\n"
        f"🔒 Stop → TP1 @ <code>{_fp(tp1)}</code>{chand_note}\n"
        f"⏳ Holding {TP3_CLOSE_PCT*100:.0f}% ke TP3 @ <code>{_fp(tp3)}</code>  "
            f"<i>({_pct_price(entry, tp3)})</i>{cascade_note}\n\n"
        f"<i>🕐 {datetime.now().strftime('%H:%M:%S')}</i>"
    )
    send_reply(msg, reply_to_message_id=tg_id)
    return partial_pnl


# ─── PAPER MONITOR (TP / SL Check) ───────────────────────────────────────────

def paper_monitor(trade: dict, current_price: float):
    """
    Cek apakah TP atau SL sudah kena untuk open paper trade.

    SL Trailing:
      Belum TP1  → SL = original
      TP1 hit    → SL = entry (breakeven)
      TP2 hit    → SL = TP1 price

    Cascade TP:
      Harga skip TP1/TP2 langsung ke TP2/TP3:
      → proses TP sebelumnya dulu (cascade=True) baru lanjut ke level berikutnya
    """
    t_id       = trade['id']
    sym        = trade['symbol']
    side       = trade['side']
    entry      = float(trade['entry_price'])
    tp1        = float(trade['tp1'])
    tp2        = float(trade['tp2'])
    tp3        = float(trade['tp3'])
    qty        = float(trade['quantity'])
    lev        = int(trade.get('leverage', 1))
    tg_id      = trade.get('telegram_msg_id')
    tp1_logged = trade.get('_tp1_logged', False)
    tp2_logged = trade.get('_tp2_logged', False)

    # Advanced 4/4: ratchet running extreme for chandelier trail BEFORE
    # computing the effective SL — so a fresh new high tightens the trail
    # immediately. Only meaningful once chandelier_active (post-TP2).
    if CHANDELIER_TRAIL_ENABLED and trade.get('chandelier_active'):
        if side == 'Long':
            prev = trade.get('highest_since_entry')
            new_ext = max(float(prev) if prev is not None else current_price, current_price)
            if prev is None or new_ext > float(prev):
                trade['highest_since_entry'] = new_ext
                update_active_trade(t_id, {"highest_since_entry": new_ext})
        else:
            prev = trade.get('lowest_since_entry')
            new_ext = min(float(prev) if prev is not None else current_price, current_price)
            if prev is None or new_ext < float(prev):
                trade['lowest_since_entry'] = new_ext
                update_active_trade(t_id, {"lowest_since_entry": new_ext})

    eff_sl = _effective_sl(trade)

    if side == 'Long':
        sl_hit  = current_price <= eff_sl
        tp1_hit = current_price >= tp1
        tp2_hit = current_price >= tp2
        tp3_hit = current_price >= tp3
    else:
        sl_hit  = current_price >= eff_sl
        tp1_hit = current_price <= tp1
        tp2_hit = current_price <= tp2
        tp3_hit = current_price <= tp3

    # ── SL Hit ──────────────────────────────────────────────────────────────
    if sl_hit:
        remaining = _remaining_qty(qty, tp1_logged, tp2_logged)

        # Fix H — gap-to-loss should fill at the worse of (eff_sl, current_price)
        # plus exit slippage. Real exchange does not magically print SL exactly
        # when price gapped through it.
        if side == 'Long':
            gap_fill = min(eff_sl, current_price)
        else:
            gap_fill = max(eff_sl, current_price)
        exit_price = _apply_exit_slippage(side, gap_fill, is_loss=True)
        pnl        = _calc_pnl(side, entry, exit_price, remaining)

        if tp2_logged:
            # Distinguish chandelier-trail exits (runner protected by ratcheting
            # SL above TP1) from the legacy "SL pinned at TP1" trail.
            tp1_floor = float(trade.get('tp1', 0) or 0)
            if (
                CHANDELIER_TRAIL_ENABLED
                and trade.get('chandelier_active')
                and tp1_floor > 0
                and (
                    (side == 'Long'  and eff_sl > tp1_floor)
                    or (side == 'Short' and eff_sl < tp1_floor)
                )
            ):
                reason = "Chandelier Trail"
            else:
                reason = "SL Trail (TP1 level)"
        elif trade.get('is_sl_moved'):
            reason = "Breakeven SL"
        else:
            reason = "Stop Loss"
        if abs(exit_price - eff_sl) > _bps(eff_sl, 1.0):
            reason += " (gap)"

        _close_paper_trade(t_id, sym, side, entry, exit_price, pnl, reason, tg_id,
                           qty=remaining, lev=lev)
        return

    # ── TP3 Hit (close remaining 40%) ────────────────────────────────────────
    if tp3_hit:
        # Fix H — detect a single-tick gap straight to TP3 without intermediate
        # fills at TP1/TP2. Without wick info we cannot prove the price wicked
        # through TP1/TP2; closing the whole position at current_price is more
        # conservative than fabricating clean TP1/TP2 fills.
        gap_to_profit = (not tp1_logged) and (not tp2_logged)
        if gap_to_profit:
            remaining  = qty
            exit_price = _apply_exit_slippage(side, current_price, is_loss=False)
            pnl        = _calc_pnl(side, entry, exit_price, remaining)
            _close_paper_trade(t_id, sym, side, entry, exit_price, pnl,
                               "TP3 (gap)", tg_id, qty=remaining, lev=lev)
            return

        # Cascade TP1 jika belum (price wicked through)
        if not tp1_logged:
            _handle_tp1_partial(trade, cascade=True)
            trade['_tp1_logged'] = True
            tp1_logged = True

        # Cascade TP2 jika belum
        if not tp2_logged:
            _handle_tp2_partial(trade, cascade=True)
            trade['_tp2_logged'] = True
            tp2_logged = True

        # Close sisa 40% di TP3
        remaining  = _remaining_qty(qty, tp1_logged, tp2_logged)
        exit_price = _apply_exit_slippage(side, tp3, is_loss=False)
        pnl        = _calc_pnl(side, entry, exit_price, remaining)
        _close_paper_trade(t_id, sym, side, entry, exit_price, pnl, "TP3", tg_id,
                           qty=remaining, lev=lev)
        return

    # ── TP2 Hit (partial 30%) ────────────────────────────────────────────────
    if tp2_hit and not tp2_logged:
        # Cascade TP1 jika belum
        if not tp1_logged:
            _handle_tp1_partial(trade, cascade=True)
            trade['_tp1_logged'] = True

        _handle_tp2_partial(trade, cascade=False)
        return

    # ── TP1 Hit (partial 30%) ────────────────────────────────────────────────
    if tp1_hit and not tp1_logged:
        _handle_tp1_partial(trade, cascade=False)
        return


# ─── CLOSE TRADE ──────────────────────────────────────────────────────────────

def _close_paper_trade(
    trade_id: int,
    symbol: str,
    side: str,
    entry: float,
    exit_price: float,
    pnl: float,
    reason: str,
    telegram_msg_id: int = None,
    qty: float = 0.0,
    lev: int = 1,
):
    """Tutup paper trade, update balance, kirim notifikasi Telegram."""
    new_balance = add_paper_balance(pnl)   # atomic increment (fix #1B)
    update_active_trade(trade_id, {"status": "CLOSED", "pnl": pnl})

    # ── Phase 8: pattern attribution ────────────────────────────────────────
    # Walk the registry hits stored on this active_trade and emit one
    # pattern_stats row per pattern so the rolling-30d actual winrate stays
    # current. Best-effort: errors are logged and swallowed inside.
    # NOTE: pass the *remaining* qty that was actually closed in this leg —
    # after partial TP1+TP2 fills only ~30% may remain. Using the original
    # full quantity would inflate the breakeven band ~3.3× and mislabel
    # decisive TP3 wins as breakeven.
    try:
        n_attr = record_trade_close_outcomes(
            trade_id, symbol=symbol, side=side, pnl=pnl, qty=qty,
        )
        if n_attr:
            logger.debug(
                f"[PAPER] {symbol} attributed close to {n_attr} pattern(s) "
                f"(pnl={pnl:+.4f})"
            )
    except Exception as e:
        logger.warning(f"[PAPER] {symbol} pattern-attribution failed: {e}")

    is_win   = pnl >= 0
    emoji    = "✅" if is_win else "❌"
    result   = "PROFIT 🟢" if is_win else "LOSS 🔴"
    is_sl    = "SL" in reason or "Stop" in reason
    exit_ico = "🛑" if is_sl else "🎯"

    price_pct   = _pct_price(entry, exit_price)
    margin      = _calc_margin(entry, qty, lev) if qty > 0 else 0.0
    roi_margin  = _roi_on_margin(pnl, margin) if margin > 0 else ""
    roi_balance = _roi_on_balance(pnl, new_balance)

    logger.info(
        f"{emoji} [PAPER] {symbol} CLOSED ({reason}) | "
        f"Entry: {entry} → Exit: {exit_price} ({price_pct}) | "
        f"PnL: ${pnl:+.4f} | ROI/Margin: {roi_margin} | "
        f"Balance: ${new_balance:.2f} ({roi_balance})"
    )

    msg = (
        f"{SEP}\n"
        f"{emoji} <b>[PAPER] Trade Closed</b>\n"
        f"{SEP}\n\n"
        f"📌 <b>{symbol}</b>  {_side_label(side)}\n"
        f"📊 Closed by   <b>{reason}</b>\n\n"
        f"📍 Entry    <code>{_fp(entry)}</code>\n"
        f"{exit_ico} Exit     <code>{_fp(exit_price)}</code>  "
            f"<i>({price_pct} harga)</i>\n\n"
        f"{'━'*11}\n"
        f"💰 PnL           <code>${pnl:+.4f}</code>   <b>{result}</b>\n"
        f"📊 ROI/Margin    <code>{roi_margin}</code>  "
            f"<i>(dari ${margin:.2f}  ×{lev})</i>\n"
        f"💼 ROI/Balance   <code>{roi_balance}</code>  "
            f"→  <code>${new_balance:.2f}</code>\n"
        f"{'━'*11}\n\n"
        f"<i>🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
    )
    send_reply(msg, reply_to_message_id=telegram_msg_id)
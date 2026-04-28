"""
modules/leverage.py — Single source of truth for per-symbol leverage.

Both `auto_trades.py` (real mode) and `paper_runner.py` (paper mode) used to
duplicate this logic with **inconsistent defaults**: real mode capped at 100x,
paper mode capped at 20x. The two modes ended up sizing positions differently.

This helper centralises the resolution so paper and real always agree:

    use_max_leverage  → Bybit per-symbol max (BTC=100x, SOL=50x, …) clipped by
                        max_leverage_cap.
    not use_max_leverage → fixed target_leverage clipped by max_leverage_cap.

`risk_percent` already controls margin per trade, so a higher per-symbol
leverage only changes notional size — not the dollar risk on SL.
"""
from __future__ import annotations

import logging
import threading

from modules.config_loader import CONFIG
from modules.exchange import BybitClient

logger = logging.getLogger("Leverage")

_RISK              = CONFIG.get("risk", {})
USE_MAX_LEVERAGE   = bool(_RISK.get("use_max_leverage",   True))
TARGET_LEVERAGE    = int(_RISK.get("target_leverage",     10))
MAX_LEVERAGE_CAP   = int(_RISK.get("max_leverage_cap",   100))

_client: BybitClient | None = None
_lock   = threading.Lock()


def _get_client() -> BybitClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = BybitClient(debug=False, auto_trade=False)
    return _client


def resolve_leverage(symbol: str, client: BybitClient | None = None) -> int:
    """
    Return the leverage to use for `symbol`. Identical for paper and real
    modes. Optional `client` lets `auto_trades.py` reuse its already-
    authenticated client; paper mode just lazily spins one up via
    `_get_client()`.

    Behaviour:
      * `use_max_leverage=true`  → Bybit per-symbol max (clamped by
        `max_leverage_cap`).
      * `use_max_leverage=false` → `target_leverage` (clamped).
      * Any path falls back to `target_leverage` on RPC error.
    """
    cap = max(1, MAX_LEVERAGE_CAP)

    if not USE_MAX_LEVERAGE:
        return min(TARGET_LEVERAGE, cap)

    cli = client or _get_client()
    try:
        max_lev = int(cli.fetch_max_leverage(symbol, fallback=TARGET_LEVERAGE))
    except Exception as e:
        logger.debug(f"[{symbol}] fetch_max_leverage error: {e} — fallback={TARGET_LEVERAGE}x")
        max_lev = TARGET_LEVERAGE

    capped = min(max_lev, cap)
    if capped < max_lev:
        logger.debug(
            f"[{symbol}] leverage Bybit={max_lev}x → di-cap menjadi {capped}x "
            f"(max_leverage_cap={cap}x)"
        )
    return max(1, capped)

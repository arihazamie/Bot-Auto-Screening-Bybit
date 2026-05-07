"""
modules/leverage.py — Single source of truth for per-symbol leverage.

Centralises per-symbol leverage resolution for the paper portfolio tracker.
In the original codebase this also synced real-mode trading; the bot is now
signal-only, so this helper is paper-only — retained for consistent sizing:

    use_max_leverage  → OKX per-symbol max (BTC=100x, SOL=50x, …) clipped by
                        max_leverage_cap.
    not use_max_leverage → fixed target_leverage clipped by max_leverage_cap.

`risk_percent` already controls margin per trade, so a higher per-symbol
leverage only changes notional size — not the dollar risk on SL.
"""
from __future__ import annotations

import logging
import threading

from modules.config_loader import CONFIG
from modules.exchange import OKXClient

logger = logging.getLogger("Leverage")

_RISK              = CONFIG.get("risk", {})
USE_MAX_LEVERAGE   = bool(_RISK.get("use_max_leverage",   True))
TARGET_LEVERAGE    = int(_RISK.get("target_leverage",     10))
MAX_LEVERAGE_CAP   = int(_RISK.get("max_leverage_cap",   100))

_client: OKXClient | None = None
_lock   = threading.Lock()


def _get_client() -> OKXClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = OKXClient(debug=False, auto_trade=False)
    return _client


def resolve_leverage(symbol: str, client: OKXClient | None = None) -> int:
    """
    Return the leverage to use for `symbol` in paper sizing. The optional
    `client` argument is retained for backward-compat — paper mode lazily
    spins one up via `_get_client()`.

    Behaviour:
      * `use_max_leverage=true`  → OKX per-symbol max (clamped by
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
            f"[{symbol}] leverage OKX={max_lev}x → di-cap menjadi {capped}x "
            f"(max_leverage_cap={cap}x)"
        )
    return max(1, capped)
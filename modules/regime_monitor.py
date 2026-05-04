"""
Regime Monitor — track BTC global regime across scans and notify on shifts.

Stores the last classified BTC regime in `state_store` (key:
`btc_regime_last`) so we can compare the current scan's classification
against the previous one and emit a Telegram alert when the macro mood
changes.

Public API:
    check_btc_regime_change(client) -> dict
        Fetch BTC, classify regime, compare with stored value, send
        alert (if enabled and changed), persist new state. Returns
        the freshly-classified regime dict (same shape as
        `regime.classify_regime()`), with extra keys:
          - "changed":   bool, True when regime differs from last
          - "previous":  str  | None, previous label or None on first run
          - "alerted":   bool, True if a Telegram message was sent

Configuration (`config.json` → `regime_alerts`):
    enabled               (bool, default True)   master switch
    btc_symbol            (str,  default "BTC/USDT:USDT")
    timeframe             (str,  default "1h")
    candles               (int,  default 200)    bars to fetch (≥120)
    notify_first_run      (bool, default False)  send alert on first
                                                 classification (no
                                                 previous state)
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from modules.config_loader import CONFIG
from modules.database     import get_state, set_state
from modules.notifier     import send
from modules.regime       import classify_regime
from modules.technicals   import get_technicals

logger = logging.getLogger(__name__)

# ─── Knobs — configurable via `regime_alerts` section in config.json ────────
_RA_CFG               = CONFIG.get("regime_alerts", {})
ALERTS_ENABLED        = bool(_RA_CFG.get("enabled", True))
BTC_SYMBOL            = str(_RA_CFG.get("btc_symbol", "BTC/USDT:USDT"))
BTC_TIMEFRAME         = str(_RA_CFG.get("timeframe", "1h"))
BTC_CANDLES           = max(120, int(_RA_CFG.get("candles", 200)))
NOTIFY_FIRST_RUN      = bool(_RA_CFG.get("notify_first_run", False))

# State key in `state_store`.
_STATE_KEY = "btc_regime_last"

# Map (prev_label, new_label) → (emoji, headline, body). Order: more
# specific transitions first; falls through to a generic "shift" line.
_TRANSITION_MESSAGES: dict[tuple[str, str], tuple[str, str, str]] = {
    # Bullish → Bearish: time to consider trimming Long runners
    ("TREND_BULL", "TREND_BEAR"):
        ("🚨", "BTC shifted: BULL → BEAR",
         "Consider trimming or closing Long runners; new Long entries "
         "will be filtered by btc_bias."),

    # Bearish → Bullish: bias flips, Short runners at risk
    ("TREND_BEAR", "TREND_BULL"):
        ("🚀", "BTC shifted: BEAR → BULL",
         "Short runners now against macro bias; consider trimming."),

    # Range/Squeeze → Trend: directional move starting
    ("RANGE",   "TREND_BULL"): ("📈", "BTC: RANGE → TREND_BULL", "Directional move starting."),
    ("RANGE",   "TREND_BEAR"): ("📉", "BTC: RANGE → TREND_BEAR", "Directional move starting."),
    ("SQUEEZE", "TREND_BULL"): ("📈", "BTC: SQUEEZE → TREND_BULL", "Squeeze resolved upward."),
    ("SQUEEZE", "TREND_BEAR"): ("📉", "BTC: SQUEEZE → TREND_BEAR", "Squeeze resolved downward."),

    # Trend → Squeeze/Range: cooling off, fewer trend entries expected
    ("TREND_BULL", "RANGE"):   ("⏸️", "BTC: TREND_BULL → RANGE",   "Trend cooling, mean-revert mode active."),
    ("TREND_BEAR", "RANGE"):   ("⏸️", "BTC: TREND_BEAR → RANGE",   "Trend cooling, mean-revert mode active."),
    ("TREND_BULL", "SQUEEZE"): ("🤐", "BTC: TREND_BULL → SQUEEZE", "Compression — breakout imminent."),
    ("TREND_BEAR", "SQUEEZE"): ("🤐", "BTC: TREND_BEAR → SQUEEZE", "Compression — breakout imminent."),

    # Range ↔ Squeeze: minor mood shift
    ("RANGE",   "SQUEEZE"):    ("⚠️", "BTC: RANGE → SQUEEZE", "Volatility compressing, breakout imminent — wait for direction."),
    ("SQUEEZE", "RANGE"):      ("ℹ️", "BTC: SQUEEZE → RANGE", "Compression released without clear direction."),
}


def _format_alert(prev_label: str | None, new_regime: dict) -> str:
    """Return Telegram-ready HTML message for the regime transition."""
    new_label = new_regime["label"]

    # ANY → ANOMALY is a critical alert regardless of previous state.
    if new_label == "ANOMALY":
        return (
            f"🔴 <b>BTC ANOMALY</b>\n"
            f"Flash event detected — ATR%={new_regime.get('atr_pct', 0):.2%}.\n"
            f"<i>{new_regime.get('reason', '')}</i>\n"
            f"New entries auto-paused while BTC remains in ANOMALY."
        )

    # ANOMALY → anything: market normalised
    if prev_label == "ANOMALY":
        return (
            f"✅ <b>BTC normalised</b>\n"
            f"Resuming scans. Current regime: <b>{new_label}</b>.\n"
            f"<i>{new_regime.get('reason', '')}</i>"
        )

    # First run / unknown previous → informational
    if prev_label is None or prev_label == "UNKNOWN":
        return (
            f"ℹ️ <b>BTC regime initialised</b>: <b>{new_label}</b>\n"
            f"<i>{new_regime.get('reason', '')}</i>"
        )

    key  = (prev_label, new_label)
    spec = _TRANSITION_MESSAGES.get(key)
    if spec:
        emoji, headline, body = spec
        return (
            f"{emoji} <b>{headline}</b>\n"
            f"{body}\n"
            f"<i>{new_regime.get('reason', '')}</i>"
        )

    # Fallback for unmapped transitions
    return (
        f"🔄 <b>BTC regime shift</b>\n"
        f"<b>{prev_label}</b> → <b>{new_label}</b>\n"
        f"<i>{new_regime.get('reason', '')}</i>"
    )


def _fetch_and_classify(client) -> Optional[dict]:
    """Fetch BTC OHLCV, populate technicals, classify regime.

    Returns None on fetch failure or when not enough bars are available.
    """
    try:
        df = client.fetch_ohlcv(BTC_SYMBOL, BTC_TIMEFRAME, limit=BTC_CANDLES)
    except Exception as e:
        logger.warning(f"regime_monitor: BTC fetch failed: {type(e).__name__}: {e}")
        return None

    if df is None or len(df) < 60:
        logger.warning(f"regime_monitor: insufficient BTC bars (len={0 if df is None else len(df)})")
        return None

    try:
        df = get_technicals(df)
    except Exception as e:
        logger.warning(f"regime_monitor: get_technicals failed: {type(e).__name__}: {e}")
        return None

    return classify_regime(df)


def check_btc_regime_change(client) -> dict:
    """Classify BTC regime and emit a Telegram alert on transitions.

    Always returns a dict so callers can read the current regime for
    logging / scan-header context, even if alerts are disabled or the
    fetch failed.
    """
    out = {"label": "UNKNOWN", "reason": "", "changed": False, "previous": None, "alerted": False}

    if not ALERTS_ENABLED:
        # Caller can still use `out` for header logging — just don't alert.
        regime = _fetch_and_classify(client)
        if regime is not None:
            out.update(regime)
        return out

    regime = _fetch_and_classify(client)
    if regime is None:
        return out

    prev_label = get_state(_STATE_KEY)
    out.update(regime)
    out["previous"] = prev_label

    new_label = regime["label"]

    # First run: store, optionally alert.
    if prev_label is None:
        set_state(_STATE_KEY, new_label)
        if NOTIFY_FIRST_RUN:
            try:
                send(_format_alert(None, regime))
                out["alerted"] = True
            except Exception as e:
                logger.warning(f"regime_monitor: alert send failed: {e}")
        return out

    # No change → nothing to do.
    if prev_label == new_label:
        return out

    # Regime changed — alert and persist.
    out["changed"] = True
    try:
        send(_format_alert(prev_label, regime))
        out["alerted"] = True
        logger.info(f"regime_monitor: BTC {prev_label} → {new_label} (alert sent)")
    except Exception as e:
        logger.warning(f"regime_monitor: alert send failed: {e}")
    set_state(_STATE_KEY, new_label)
    return out

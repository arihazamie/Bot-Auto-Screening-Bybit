"""
manual_assistant.py — READ-ONLY assistant for manual Bybit Futures positions.

Apa yang dilakukan:
  1. Poll posisi terbuka di Bybit Unified (USDT-Perp) via pybit (READ-ONLY).
  2. Untuk setiap posisi: kumpulkan konteks market (regime/RSI/EMA/funding/CVD)
     pakai modul indikator yang sudah ada di repo.
  3. Tanya OpenRouter (LLM) untuk saran TP / SL adjustment + bias market.
  4. Kirim hasil sebagai notifikasi Telegram dengan dedup pintar.
  5. Deteksi posisi closed manual / partial close → kirim summary.

Apa yang TIDAK dilakukan (by design):
  • TIDAK PERNAH place / modify / cancel order. API key bisa permission Read saja.
  • Tidak menyentuh logic screening / auto_trades existing.

Run:
    # Live (kirim ke Telegram):
    python manual_assistant.py

    # Dry-run (log "🟡 SIMULATED" alih-alih kirim Telegram):
    python manual_assistant.py --dry-run

    # Test cycle satu kali (sekali poll lalu exit):
    python manual_assistant.py --once --dry-run

    # Self-test offline (tidak panggil API apapun):
    python manual_assistant.py --simulate

Config (config.json):
  "manual_assistant": {
    "enabled": false,
    "dry_run": true,
    "poll_interval_sec": 10,
    "heartbeat_minutes": 30,
    "tf_entry": "15m",
    "tf_trend": "1h",
    "openrouter": {
      "enabled": true,
      "model": "openrouter/auto",
      "cache_seconds": 60,
      "timeout_sec": 20
    }
  }

Secrets (env-var dulu, fallback config.json):
  BYBIT_API_KEY, BYBIT_API_SECRET, OPENROUTER_API_KEY
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def _setup_logging(debug: bool = False) -> logging.Logger:
    os.makedirs("data", exist_ok=True)
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            "data/manual_assistant.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
    ]
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S", handlers=handlers)
    return logging.getLogger("ManualAssistant")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="READ-ONLY assistant for manual Bybit positions (TP/SL advice via OpenRouter).",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Log 'SIMULATED' messages instead of sending to Telegram.")
    p.add_argument("--once", action="store_true",
                   help="Run one poll cycle and exit (good for cron / testing).")
    p.add_argument("--simulate", action="store_true",
                   help="Offline self-test: fabricate one synthetic position and run the full pipeline "
                        "without calling Bybit/OpenRouter/Telegram. Implies --dry-run.")
    p.add_argument("--debug", action="store_true",
                   help="Verbose log level.")
    return p.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    # Lazy imports — supaya --simulate tidak butuh config.json/pybit beneran kalau
    # dipanggil di lingkungan stripped-down.
    if args.simulate:
        return _run_simulate(logger)

    # Real path: butuh config.json valid.
    from modules.config_loader import CONFIG
    from modules.exchange import BybitClient
    from modules.manual_market_context import build_context
    from modules.manual_notifier import ManualNotifier
    from modules.manual_position_reader import ManualPositionReader
    from modules.openrouter_advisor import OpenRouterAdvisor
    from modules.secrets_loader import get_bybit_keys, get_openrouter_key

    cfg = (CONFIG.get("manual_assistant") or {})
    enabled = bool(cfg.get("enabled", False))
    if not enabled and not args.dry_run and not args.once:
        logger.warning(
            "manual_assistant.enabled = false di config.json. "
            "Set ke true untuk run live, atau pakai --dry-run / --once."
        )
        return 0

    dry_run = bool(args.dry_run or cfg.get("dry_run", True))
    poll_sec = int(cfg.get("poll_interval_sec", 10))
    hb_min   = int(cfg.get("heartbeat_minutes", 30))
    tf_entry = str(cfg.get("tf_entry", "15m"))
    tf_trend = str(cfg.get("tf_trend", "1h"))

    or_cfg = cfg.get("openrouter", {}) or {}
    or_enabled = bool(or_cfg.get("enabled", True))
    or_model   = str(or_cfg.get("model", "openrouter/auto"))
    or_cache_s = int(or_cfg.get("cache_seconds", 60))
    or_timeout = float(or_cfg.get("timeout_sec", 20.0))

    keys = get_bybit_keys()
    or_key = get_openrouter_key() if or_enabled else ""

    if keys.source == "missing":
        logger.error("Tidak bisa start: Bybit API key tidak ditemukan. "
                     "Set env var BYBIT_API_KEY + BYBIT_API_SECRET (permission Read cukup).")
        return 2

    reader = ManualPositionReader(api_key=keys.api_key, api_secret=keys.api_secret)
    bclient = BybitClient(debug=False, auto_trade=False)  # untuk OHLCV publik
    advisor = OpenRouterAdvisor(
        api_key=or_key,
        model=or_model,
        timeout_sec=or_timeout,
        cache_seconds=or_cache_s,
    )
    notifier = ManualNotifier(dry_run=dry_run, heartbeat_minutes=hb_min)

    banner = (
        f"🤖 <b>Manual Assistant started</b>\n"
        f"Mode: <code>{'DRY-RUN' if dry_run else 'LIVE'}</code> · "
        f"Poll: <code>{poll_sec}s</code> · "
        f"Heartbeat: <code>{hb_min}m</code> · "
        f"AI: <code>{'on' if or_key else 'off (heuristic)'}</code>"
    )
    notifier.notify_status(banner)
    logger.info("Manual Assistant ready. Bybit key source=%s", keys.source)

    state: dict = {}  # key -> ManualPosition

    while True:
        try:
            new_state = _poll_once(
                reader, bclient, advisor, notifier,
                state, tf_entry, tf_trend, logger,
            )
            state = new_state
        except KeyboardInterrupt:
            logger.info("🛑 Stopped by user.")
            return 0
        except Exception as e:
            logger.error(f"poll cycle error: {type(e).__name__}: {e}", exc_info=True)

        if args.once:
            return 0
        try:
            time.sleep(poll_sec)
        except KeyboardInterrupt:
            logger.info("🛑 Stopped by user.")
            return 0


def _poll_once(
    reader,
    bclient,
    advisor,
    notifier,
    prev_state: dict,
    tf_entry: str,
    tf_trend: str,
    logger: logging.Logger,
) -> dict:
    """One full polling cycle. Returns updated state dict."""
    from modules.manual_market_context import build_context

    positions = reader.fetch_open_positions()
    diff = reader.diff(prev_state, positions)

    # 1. Closed manual → notify + drop state
    for old in diff["closed"]:
        logger.info(f"🏁 Position closed: {old.key()}")
        notifier.notify_position_closed(old)

    # 2. Size changes (partial close manual)
    for old, new in diff["changed"]:
        logger.info(f"✏️ Size change {old.key()}: {old.size} → {new.size}")
        notifier.notify_size_changed(old, new)

    # 3. Build context + ask advisor + notify (opened + steady + changed)
    new_state: dict = {}
    targets = list(diff["opened"]) + list(diff["steady"]) + [n for _, n in diff["changed"]]
    for pos in targets:
        new_state[pos.key()] = pos
        try:
            ctx = build_context(
                bybit_symbol=pos.symbol,
                bybit_client=bclient,
                tf_entry=tf_entry,
                tf_trend=tf_trend,
            )
            advice = advisor.analyse(_position_to_dict(pos), ctx)
            sent = notifier.notify_position_advice(pos, ctx, advice)
            if not sent:
                logger.debug(f"silent: no change for {pos.key()}")
        except Exception as e:
            logger.error(f"advice pipeline failed for {pos.key()}: {type(e).__name__}: {e}",
                         exc_info=True)

    return new_state


def _position_to_dict(pos) -> dict:
    return {
        "symbol":              pos.symbol,
        "side":                pos.side,
        "size":                pos.size,
        "entry_price":         pos.entry_price,
        "mark_price":          pos.mark_price,
        "leverage":            pos.leverage,
        "liq_price":           pos.liq_price,
        "unrealised_pnl":      pos.unrealised_pnl,
        "unrealised_pnl_pct":  pos.unrealised_pnl_pct,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Offline self-test (--simulate)
# ─────────────────────────────────────────────────────────────────────────────

def _run_simulate(logger: logging.Logger) -> int:
    """
    Verifikasi pipeline tanpa panggilan API:
      • Fabricate 1 posisi BTCUSDT Long
      • Skip OpenRouter (force heuristic fallback)
      • Force notifier ke dry-run

    Mencetak full pesan yang akan dikirim ke Telegram.
    """
    from modules.manual_notifier import ManualNotifier
    from modules.manual_position_reader import ManualPosition
    from modules.openrouter_advisor import _heuristic_fallback

    logger.info("🧪 Starting offline simulation (no Bybit/OpenRouter/Telegram calls).")

    pos = ManualPosition(
        symbol="BTCUSDT",
        side="Long",
        size=0.05,
        entry_price=60_200.0,
        mark_price=60_925.0,
        leverage=10.0,
        liq_price=55_000.0,
        unrealised_pnl=36.25,
        unrealised_pnl_pct=1.20,
        position_idx=0,
    )

    fake_ctx = {
        "symbol":      "BTCUSDT",
        "tf_entry":    "15m",
        "tf_trend":    "1h",
        "regime_15m":  {"label": "TREND_BULL", "adx": 26.4, "atr_pct": 0.005,
                        "ema_align": 1, "reason": "ADX strong, EMA13>EMA21"},
        "regime_1h":   {"label": "TREND_BULL", "adx": 28.1, "atr_pct": 0.008,
                        "ema_align": 1, "reason": "ADX strong, EMA13>EMA21"},
        "rsi_15m":     58.0,
        "rsi_1h":      61.0,
        "ema_align_1h": 1,
        "funding_rate": 0.00012,
        "cvd_slope_15m": 1.5,
        "price_slope_15m": 4.2,
        "last_close":  60_925.0,
        "degraded":    False,
        "errors":      [],
    }

    advice = _heuristic_fallback(_position_to_dict(pos), fake_ctx, reason="simulate")

    notifier = ManualNotifier(dry_run=True, heartbeat_minutes=30)
    notifier.notify_status("🧪 simulate banner — would announce assistant startup")
    sent_first = notifier.notify_position_advice(pos, fake_ctx, advice)
    sent_dup   = notifier.notify_position_advice(pos, fake_ctx, advice)  # should dedup
    # Force a bias change to verify dedup wakes back up
    advice2 = dict(advice)
    advice2["bias"] = "bearish"
    advice2["tp_recommendation"] = {**advice2["tp_recommendation"], "action": "take_partial_now"}
    sent_second = notifier.notify_position_advice(pos, fake_ctx, advice2)

    # And test closed flow
    notifier.notify_position_closed(pos)

    # Self-assert
    ok = (sent_first is True) and (sent_dup is False) and (sent_second is True)
    if ok:
        logger.info("✅ Simulation ran successfully and dedup worked.")
        return 0
    logger.error(
        f"❌ Simulation assertion failed: first={sent_first} dup={sent_dup} second={sent_second}"
    )
    return 1


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    args = _parse_args()
    logger = _setup_logging(debug=args.debug)
    return run(args, logger)


if __name__ == "__main__":
    sys.exit(main())

"""
watchlist.py — Top N USDT Perpetual pairs by 24h volume dari OKX.

Default TOP_N=300: covers virtually every liquid USDT perpetual on OKX.
Override via system.watchlist_top_n in config.json (e.g. 100 for a tighter list).

Direfresh setiap hari jam 7 pagi via schedule di main.py.
Data disimpan di data/watchlist.json sebagai cache.

Alur:
  refresh_watchlist(exchange) → fetch ticker semua pair → sort by volume → simpan top N
  get_watchlist()             → baca dari cache, return list of symbol strings
  get_watchlist_or_default()  → fallback ke semua pair jika cache belum ada
"""

import json
import os
import logging
from datetime import datetime, timezone

from modules.config_loader import CONFIG

logger = logging.getLogger("Watchlist")

# ─── Config ────────────────────────────────────────────────
BASE_DIR      = os.path.join(os.path.dirname(__file__), '..', 'data')
WATCHLIST_F   = os.path.join(BASE_DIR, 'watchlist.json')
TOP_N         = 300

# Fix #3: anggap watchlist stale kalau lebih tua dari `max_age_hours`.
# Default 36 jam (refresh harian + 12 jam toleransi). Override via
# system.watchlist_max_age_hours di config.
_MAX_AGE_HOURS = float(
    CONFIG.get("system", {}).get("watchlist_max_age_hours", 36.0)
)

STABLECOINS = {
    'USDC', 'USDT', 'DAI', 'FDUSD', 'USDD', 'USDE',
    'TUSD', 'BUSD', 'PYUSD', 'USDS', 'EUR', 'USD'
}


# ─── Core ──────────────────────────────────────────────────

def refresh_watchlist(exchange, top_n: int = TOP_N) -> list[str]:
    """
    Fetch semua USDT perpetual ticker dari OKX,
    sort by quoteVolume (24h), ambil top N.
    Simpan hasilnya ke data/watchlist.json.

    Return: list of symbol strings, e.g. ['BTC/USDT:USDT', 'ETH/USDT:USDT', ...]
    """
    logger.info(f"🔄 Refreshing watchlist — fetching top {top_n} pairs by 24h volume...")

    try:
        os.makedirs(BASE_DIR, exist_ok=True)

        # Load markets untuk filter swap + active
        mkts = exchange.load_markets()
        valid_symbols = [
            s for s in mkts
            if mkts[s].get('swap')
            and mkts[s]['quote'] == 'USDT'
            and mkts[s].get('active')
            and mkts[s]['base'] not in STABLECOINS
            and "ST" not in mkts[s].get('id', '')   # skip settlement tokens
        ]

        # Fetch semua ticker sekaligus (1 request, lebih efisien)
        logger.info(f"   Fetching tickers for {len(valid_symbols)} valid pairs...")
        tickers = exchange.fetch_tickers(valid_symbols)

        # Sort by quoteVolume (volume dalam USDT, 24h)
        ranked = sorted(
            [
                {
                    "symbol": sym,
                    "base":   mkts[sym]['base'],
                    "volume": float(tickers[sym].get('quoteVolume') or 0),
                }
                for sym in valid_symbols
                if sym in tickers
            ],
            key=lambda x: x['volume'],
            reverse=True
        )

        top = ranked[:top_n]
        symbols = [r['symbol'] for r in top]

        # Simpan ke cache. updated_at pakai UTC ISO supaya `get_watchlist()`
        # bisa parse umurnya untuk max-age guard (fix #3).
        cache = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "top_n":      top_n,
            "symbols":    symbols,
            "detail":     top,
        }
        with open(WATCHLIST_F, 'w') as f:
            json.dump(cache, f, indent=2)

        # Log top 10 untuk konfirmasi
        logger.info(f"✅ Watchlist updated — top {top_n} pairs saved.")
        logger.info("   Top 10 by volume:")
        for i, r in enumerate(top[:10], 1):
            vol_m = r['volume'] / 1_000_000
            logger.info(f"   {i:>2}. {r['symbol']:<25} ${vol_m:,.1f}M")

        return symbols

    except Exception as e:
        logger.error(f"❌ Watchlist refresh failed: {e}")
        return []


def _cache_age_hours(updated_at_str: str) -> float | None:
    """Return age (hours) of `updated_at_str`, atau None kalau tidak bisa parse."""
    if not updated_at_str:
        return None
    try:
        # Format baru (ISO UTC dari fix #3) ATAU format lama str(datetime.now())
        try:
            ts = datetime.fromisoformat(updated_at_str)
        except ValueError:
            ts = datetime.strptime(updated_at_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    except Exception:
        return None


def get_watchlist() -> list[str] | None:
    """
    Baca watchlist dari cache.

    Fix #3: kalau cache lebih tua dari `_MAX_AGE_HOURS`, return None supaya
    caller paksa re-refresh (atau fallback). Tanpa guard ini, refresh yang
    gagal berhari-hari membuat bot scan pair yang sudah delisted.
    """
    if not os.path.exists(WATCHLIST_F):
        return None

    try:
        with open(WATCHLIST_F, 'r') as f:
            data = json.load(f)
        symbols = data.get('symbols', [])
        if not symbols:
            return None

        updated_at = data.get('updated_at', '')
        age_h = _cache_age_hours(updated_at)
        if age_h is not None and age_h > _MAX_AGE_HOURS:
            logger.warning(
                f"⚠️  Watchlist stale ({age_h:.1f}h > {_MAX_AGE_HOURS:.0f}h max) "
                f"— treating as empty so caller akan re-refresh"
            )
            return None

        age_label = f"{age_h:.1f}h ago" if age_h is not None else updated_at
        logger.info(f"📋 Watchlist loaded: {len(symbols)} pairs (age: {age_label})")
        return symbols

    except Exception as e:
        logger.error(f"Watchlist read error: {e}")
        return None


def get_watchlist_info() -> dict:
    """Return metadata watchlist: updated_at, jumlah pair, top 10 detail."""
    if not os.path.exists(WATCHLIST_F):
        return {}
    try:
        with open(WATCHLIST_F, 'r') as f:
            data = json.load(f)
        return {
            "updated_at": data.get('updated_at'),
            "total":      len(data.get('symbols', [])),
            "top_10":     data.get('detail', [])[:10],
        }
    except Exception:
        return {}
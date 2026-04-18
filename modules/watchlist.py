"""
watchlist.py — Top N USDT Perpetual pairs by 24h volume dari Bybit.

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
from datetime import datetime

logger = logging.getLogger("Watchlist")

# ─── Config ────────────────────────────────────────────────
BASE_DIR      = os.path.join(os.path.dirname(__file__), '..', 'data')
WATCHLIST_F   = os.path.join(BASE_DIR, 'watchlist.json')
TOP_N         = 100

STABLECOINS = {
    'USDC', 'USDT', 'DAI', 'FDUSD', 'USDD', 'USDE',
    'TUSD', 'BUSD', 'PYUSD', 'USDS', 'EUR', 'USD'
}


# ─── Core ──────────────────────────────────────────────────

def refresh_watchlist(exchange, top_n: int = TOP_N) -> list[str]:
    """
    Fetch semua USDT perpetual ticker dari Bybit,
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

        # Simpan ke cache
        cache = {
            "updated_at": str(datetime.now()),
            "top_n":      top_n,
            "symbols":    symbols,
            "detail":     top,   # termasuk volume untuk info
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


def get_watchlist() -> list[str] | None:
    """
    Baca watchlist dari cache.
    Return None jika cache belum ada atau kosong.
    """
    if not os.path.exists(WATCHLIST_F):
        return None

    try:
        with open(WATCHLIST_F, 'r') as f:
            data = json.load(f)
        symbols = data.get('symbols', [])
        if not symbols:
            return None

        updated_at = data.get('updated_at', 'unknown')
        logger.info(f"📋 Watchlist loaded: {len(symbols)} pairs (updated: {updated_at})")
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
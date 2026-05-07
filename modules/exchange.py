"""
exchange.py — Best-practice CCXT/OKX wrapper

Fitur:
  ✅ Auto-detect format symbol (BTC/USDT → BTC/USDT:USDT untuk swap)
  ✅ Retry dengan exponential backoff (RateLimitExceeded, NetworkError)
  ✅ Structured logging — setiap call tercatat dengan durasi & error
  ✅ Connection health-check saat startup
  ✅ Debug mode — print setiap request/response detail
  ✅ Satu titik error handling untuk seluruh bot

Cara pakai di main.py:
    from modules.exchange import OKXClient
    client = OKXClient(debug=False)    # debug=True untuk verbose
    client.health_check()              # cek koneksi saat startup
    df    = client.fetch_ohlcv('BTC/USDT:USDT', '1h', limit=100)
    ticker = client.fetch_ticker('ETH/USDT:USDT')
"""

import time
import logging
import threading
from functools import wraps
from typing import Optional

import ccxt
import pandas as pd

from modules.config_loader import CONFIG

# ─────────────────────────────────────────────────────────────────────────────
# Logger
# ─────────────────────────────────────────────────────────────────────────────
logger = logging.getLogger("Exchange")


# ─────────────────────────────────────────────────────────────────────────────
# Retry decorator — dipakai oleh semua method public
# ─────────────────────────────────────────────────────────────────────────────
def _with_retry(max_retries: int = 3, base_delay: float = 1.0):
    """
    Decorator: retry otomatis jika RateLimitExceeded atau NetworkError.
    Delay: 1s → 2s → 4s (exponential backoff).
    Exception lain (BadSymbol, AuthError, dll) langsung raise — tidak di-retry.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(self, *args, **kwargs):
            last_exc = None
            for attempt in range(1, max_retries + 1):
                try:
                    return fn(self, *args, **kwargs)

                except ccxt.RateLimitExceeded as e:
                    last_exc = e
                    wait = base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        f"⏳ RateLimitExceeded [{fn.__name__}] "
                        f"attempt {attempt}/{max_retries} — tunggu {wait:.1f}s"
                    )
                    time.sleep(wait)

                except ccxt.NetworkError as e:
                    last_exc = e
                    wait = base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        f"🌐 NetworkError [{fn.__name__}] "
                        f"attempt {attempt}/{max_retries} — tunggu {wait:.1f}s | {e}"
                    )
                    time.sleep(wait)

                except ccxt.RequestTimeout as e:
                    last_exc = e
                    wait = base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        f"⏱️  Timeout [{fn.__name__}] "
                        f"attempt {attempt}/{max_retries} — tunggu {wait:.1f}s"
                    )
                    time.sleep(wait)

                except (ccxt.BadSymbol, ccxt.AuthenticationError) as e:
                    # Jenis error ini tidak akan sembuh dengan retry
                    logger.error(f"❌ Fatal [{fn.__name__}]: {type(e).__name__}: {e}")
                    raise

                except ccxt.ExchangeError as e:
                    # Error dari OKX (contoh: invalid parameter)
                    logger.error(f"❌ ExchangeError [{fn.__name__}]: {e}")
                    raise

                except Exception as e:
                    # Exception tak terduga — log lalu raise
                    logger.error(
                        f"❌ Unexpected [{fn.__name__}] {type(e).__name__}: {e}",
                        exc_info=True   # cetak full traceback ke log
                    )
                    raise

            # Semua retry habis
            logger.error(
                f"❌ [{fn.__name__}] Gagal setelah {max_retries} percobaan. "
                f"Error terakhir: {last_exc}"
            )
            raise last_exc
        return wrapper
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# OKXClient
# ─────────────────────────────────────────────────────────────────────────────
class OKXClient:
    """
    Wrapper tunggal untuk semua interaksi dengan OKX via CCXT.

    Parameter
    ---------
    debug : bool
        True  → print setiap API call + durasi ke stdout
        False → hanya log WARNING/ERROR (default untuk produksi)
    """

    def __init__(self, debug: bool = False, auto_trade: bool = False):
        # `auto_trade` argument retained for backward-compat with older callers,
        # but the bot is now signal-only — we never set API keys regardless.
        self.debug = debug
        self.auto_trade = False

        # Signal-only: no API key. CCXT will only hit public market-data endpoints.
        api_cfg: dict = {}

        self._ex = ccxt.okx({
            **api_cfg,
            "options": {
                "defaultType": "swap",   # Perpetual futures
            },
            "enableRateLimit": True,     # CCXT built-in rate limiter
            "timeout": 15_000,           # 15 detik timeout per request
        })

        logger.info("OKXClient initialized (mode=swap, rateLimit=ON)")

        # Symbol normalization cache — instance variable (bukan class variable)
        # agar tidak bocor antar instance (terutama saat testing atau multi-client)
        self._symbol_cache: dict[str, str] = {}

        # FIX: Perbesar connection pool agar tidak "pool is full" saat multi-thread
        # Default pool size = 10, tapi bot pakai 20 threads → naik ke 30
        from requests.adapters import HTTPAdapter
        _adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30)
        self._ex.session.mount("https://", _adapter)

        # Cache max leverage per symbol (thread-safe + TTL)
        # Struktur: { symbol: (max_leverage: int, fetched_at: float) }
        # TTL default 6 jam — OKX sesekali mengubah max leverage per coin
        self._leverage_cache: dict[str, tuple[int, float]] = {}
        self._leverage_lock  = threading.Lock()
        self._leverage_ttl   = 6 * 3600   # 6 jam dalam detik

        # Cache OHLCV per (symbol, timeframe) — mengurangi API call saat scan.
        # Validasi BOUNDARY-AWARE (lihat _is_cache_fresh): cache valid selama
        # fetched_at dan now masih di periode candle yang sama.
        # Struktur: { (symbol, timeframe): (DataFrame, fetched_at: float) }
        self._ohlcv_cache: dict[tuple, tuple] = {}
        self._ohlcv_lock  = threading.Lock()

    # ─────────────────────────────────────────────
    # Symbol normalization
    # ─────────────────────────────────────────────
    def normalize_symbol(self, symbol: str) -> str:
        """
        Konversi symbol ke format OKX Perpetual yang benar.

        Contoh:
          'BTC/USDT'      → 'BTC/USDT:USDT'
          'ETHUSDT'       → 'ETH/USDT:USDT'
          'BTC/USDT:USDT' → 'BTC/USDT:USDT'  (tidak diubah)
        """
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]

        normalized = symbol

        # Format 'BTCUSDT' → 'BTC/USDT:USDT'
        if "/" not in symbol and symbol.endswith("USDT"):
            base = symbol[:-4]
            normalized = f"{base}/USDT:USDT"

        # Format 'BTC/USDT' → 'BTC/USDT:USDT'
        elif "/" in symbol and ":" not in symbol:
            normalized = f"{symbol}:USDT"

        self._symbol_cache[symbol] = normalized

        if normalized != symbol and self.debug:
            logger.debug(f"   symbol normalized: '{symbol}' → '{normalized}'")

        return normalized

    # ─────────────────────────────────────────────
    # Debug helper
    # ─────────────────────────────────────────────
    def _dbg(self, method: str, symbol: str, t0: float, result_size: int = 0):
        if self.debug:
            elapsed = (time.time() - t0) * 1000
            logger.debug(f"   [{method}] {symbol} — {elapsed:.0f}ms | rows={result_size}")

    # ─────────────────────────────────────────────
    # Health check — panggil saat startup
    # ─────────────────────────────────────────────
    def health_check(self, auto_trade: bool = False) -> bool:
        """
        Verifikasi koneksi ke OKX (public market-data only).
        Print ringkasan status ke stdout. Return True jika OK.

        `auto_trade` retained for backward-compat — ignored in signal-only mode.
        """
        print("\n" + "─" * 50)
        print("🔌 OKX Connection Health Check")
        print("─" * 50)

        ok = True

        # 1. Test fetch OHLCV publik (tidak butuh API key)
        try:
            t0 = time.time()
            bars = self._ex.fetch_ohlcv("BTC/USDT:USDT", "1m", limit=3)
            ms   = (time.time() - t0) * 1000
            if bars and len(bars) > 0:
                print(f"  ✅ Public API  — OK ({ms:.0f}ms) | Last BTC: {bars[-1][4]:,.2f}")
            else:
                print(f"  ⚠️  Public API  — Response kosong")
                ok = False
        except Exception as e:
            print(f"  ❌ Public API  — GAGAL: {type(e).__name__}: {e}")
            ok = False

        # Signal-only: no private endpoint check needed.

        # 3. Format symbol
        test_sym = self.normalize_symbol("BTC/USDT")
        print(f"  ℹ️  Symbol format — 'BTC/USDT' → '{test_sym}'")

        # 4. Rate limit status
        print(f"  ℹ️  Rate limit    — CCXT built-in enabled")
        print(f"  ℹ️  Timeout       — 15 detik per request")
        print("─" * 50 + "\n")

        return ok

    # ─────────────────────────────────────────────
    # OHLCV Cache — boundary-aware
    # ─────────────────────────────────────────────

    # Durasi satu candle dalam detik untuk setiap timeframe
    _TF_SECONDS: dict[str, int] = {
        "1m":  60,    "3m":  180,   "5m":  300,
        "15m": 900,   "30m": 1800,  "1h":  3600,
        "2h":  7200,  "4h":  14400, "6h":  21600,
        "12h": 43200, "1d":  86400, "1w":  604800,
    }

    # Offset (detik) untuk menggeser epoch sebelum di-floor ke periode candle.
    # OKX close weekly candle hari SENIN 00:00 UTC — sama seperti mayoritas exchange.
    # Epoch 0 = Kamis → geser +4 hari (345600s) supaya boundary jatuh di Senin.
    _TF_OFFSET: dict[str, int] = {
        "1w": 345600,   # 4 hari = Kamis → Senin
    }

    def _is_cache_fresh(self, timeframe: str, fetched_at: float, now: float) -> bool:
        """
        Cache fresh ⇔ fetched_at dan now masih di periode candle yang sama.
        Unknown timeframe → fallback ke 15m supaya tidak cache forever.
        """
        candle_sec = self._TF_SECONDS.get(timeframe, 900)
        offset     = self._TF_OFFSET.get(timeframe, 0)
        return (
            int((fetched_at - offset) // candle_sec)
            == int((now - offset) // candle_sec)
        )

    # ─────────────────────────────────────────────
    # fetch_ohlcv
    # ─────────────────────────────────────────────
    @_with_retry(max_retries=3, base_delay=1.0)
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
    ) -> Optional[pd.DataFrame]:
        """
        Fetch OHLCV dan return sebagai DataFrame.
        Hasil di-cache per (symbol, timeframe) dengan validasi boundary-aware.

        Return None jika data tidak cukup (bukan exception).
        Kolom: timestamp (datetime), open, high, low, close, volume
        """
        sym       = self.normalize_symbol(symbol)
        cache_key = (sym, timeframe)
        now       = time.time()

        # ── Cache read (fast path) — boundary-aware ─────────────────────────
        cached = self._ohlcv_cache.get(cache_key)
        if cached is not None:
            df_cached, fetched_at = cached
            if self._is_cache_fresh(timeframe, fetched_at, now):
                logger.debug(
                    f"fetch_ohlcv [{sym}/{timeframe}] — cache HIT "
                    f"({now - fetched_at:.0f}s old, same candle period)"
                )
                return df_cached.copy()

        # ── Cache miss: fetch dari OKX ─────────────────────────────────────
        t0   = time.time()
        bars = self._ex.fetch_ohlcv(sym, timeframe, limit=limit)
        self._dbg("fetch_ohlcv", sym, t0, len(bars) if bars else 0)

        if not bars or len(bars) < 10:
            logger.warning(f"fetch_ohlcv [{sym}/{timeframe}] — data kurang ({len(bars) if bars else 0} bars)")
            return None

        df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})

        # ── Cache write (thread-safe) ──────────────────────────────────────
        with self._ohlcv_lock:
            self._ohlcv_cache[cache_key] = (df.copy(), now)

        return df

    # ─────────────────────────────────────────────
    # fetch_ticker
    # ─────────────────────────────────────────────
    @_with_retry(max_retries=3, base_delay=1.0)
    def fetch_ticker(self, symbol: str) -> Optional[dict]:
        """
        Fetch ticker satu symbol.
        Return None jika symbol tidak valid (tidak raise exception ke caller).
        """
        sym = self.normalize_symbol(symbol)
        t0  = time.time()

        ticker = self._ex.fetch_ticker(sym)
        self._dbg("fetch_ticker", sym, t0)

        if not ticker or not ticker.get("last"):
            logger.warning(f"fetch_ticker [{sym}] — response kosong atau last price 0")
            return None

        return ticker

    # ─────────────────────────────────────────────
    # fetch_tickers (bulk)
    # ─────────────────────────────────────────────
    @_with_retry(max_retries=3, base_delay=2.0)
    def fetch_tickers(self, symbols: list[str]) -> dict:
        """
        Fetch banyak ticker sekaligus (1 request). Lebih efisien dari loop fetch_ticker.
        Return dict: {symbol: ticker_dict}
        """
        normalized = [self.normalize_symbol(s) for s in symbols]
        t0 = time.time()

        tickers = self._ex.fetch_tickers(normalized)
        self._dbg("fetch_tickers", f"[{len(normalized)} symbols]", t0, len(tickers))

        return tickers

    # ─────────────────────────────────────────────
    # fetch_max_leverage
    # ─────────────────────────────────────────────
    def fetch_max_leverage(self, symbol: str, fallback: int = 10) -> int:
        """
        Ambil max leverage dari OKX untuk satu symbol perpetual.

        Prioritas pembacaan (dari paling akurat):
          1. info.maxLever      — field OKX asli, paling reliable
          2. limits.leverage.max — field standar ccxt, kadang kosong/0
          3. fallback            — dari config atau parameter

        Thread-safe: hasil di-cache per symbol agar tidak request berulang.
        TTL 6 jam: cache di-invalidate otomatis karena OKX bisa mengubah max
        leverage per coin.

        Contoh hasil nyata dari OKX:
          BTC/USDT:USDT  → 125x
          ETH/USDT:USDT  → 100x
          DOGE/USDT:USDT → 50x
          XRP/USDT:USDT  → 75x

        Parameter
        ---------
        symbol   : str — format apapun ('BTC/USDT', 'BTCUSDT', 'BTC/USDT:USDT')
        fallback : int — leverage default jika data tidak tersedia

        Return
        ------
        int — max leverage yang diizinkan OKX untuk symbol tersebut
        """
        sym = self.normalize_symbol(symbol)

        # ── Fast path: cache hit yang belum expired ───────────────────────────
        cached = self._leverage_cache.get(sym)
        if cached is not None:
            cached_value, fetched_at = cached
            if (time.time() - fetched_at) < self._leverage_ttl:
                return cached_value
            logger.debug(f"[{sym}] leverage cache expired (TTL {self._leverage_ttl/3600:.0f}h) — refresh")

        with self._leverage_lock:
            # Double-check setelah acquire lock (cegah thundering herd)
            cached = self._leverage_cache.get(sym)
            if cached is not None:
                cached_value, fetched_at = cached
                if (time.time() - fetched_at) < self._leverage_ttl:
                    return cached_value

            max_lev = fallback
            symbol_found = False
            try:
                markets = self._ex.load_markets()
                market  = markets.get(sym, {})

                if not market:
                    logger.warning(
                        f"fetch_max_leverage [{sym}] — symbol tidak ditemukan di markets, "
                        f"fallback={fallback}x"
                    )
                    self._leverage_cache[sym] = (fallback, time.time())
                    return fallback

                symbol_found = True

                # ── Prioritas 1: OKX native field ────────────────────────────
                okx_lev = market.get("info", {}).get("maxLever")
                if okx_lev:
                    try:
                        parsed = int(float(okx_lev))
                        if parsed > 0:
                            max_lev = parsed
                            logger.debug(
                                f"[{sym}] max_leverage={max_lev}x "
                                f"(sumber: info.maxLever)"
                            )
                    except (ValueError, TypeError):
                        pass

                # ── Prioritas 2: ccxt standar field ──────────────────────────
                if max_lev == fallback:
                    ccxt_lev = (
                        market.get("limits", {})
                              .get("leverage", {})
                              .get("max")
                    )
                    if ccxt_lev:
                        try:
                            parsed = int(float(ccxt_lev))
                            if parsed > 0:
                                max_lev = parsed
                                logger.debug(
                                    f"[{sym}] max_leverage={max_lev}x "
                                    f"(sumber: limits.leverage.max)"
                                )
                        except (ValueError, TypeError):
                            pass

                if max_lev <= 0:
                    max_lev = fallback

            except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                logger.warning(
                    f"fetch_max_leverage [{sym}] network error — fallback={fallback}x "
                    f"(tidak di-cache, akan retry) | {e}"
                )
                return fallback

            except Exception as e:
                logger.warning(
                    f"fetch_max_leverage [{sym}] error — fallback={fallback}x | {e}"
                )
                self._leverage_cache[sym] = (fallback, time.time() - self._leverage_ttl + 3600)
                return fallback

            # Simpan ke cache dengan timestamp saat ini
            self._leverage_cache[sym] = (max_lev, time.time())
            src = "OKX data" if symbol_found else "fallback"
            logger.info(f"📊 [{sym}] Max leverage: {max_lev}x (cached, TTL {self._leverage_ttl/3600:.0f}h, src={src})")
            return max_lev

    def prefetch_leverage(self, symbols: list[str]) -> None:
        """
        Warm-up cache leverage untuk banyak symbol sekaligus.

        Memanggil load_markets() SEKALI (ccxt sudah cache internal), lalu
        parsing tiap symbol tanpa request tambahan.
        """
        if not symbols:
            return

        logger.info(f"📊 Prefetch leverage cache untuk {len(symbols)} symbols...")
        try:
            markets = self._ex.load_markets()
        except Exception as e:
            logger.warning(f"prefetch_leverage: load_markets() gagal — skip | {e}")
            return

        now = time.time()
        hit = 0
        for symbol in symbols:
            sym = self.normalize_symbol(symbol)

            # Skip jika cache masih valid
            cached = self._leverage_cache.get(sym)
            if cached and (now - cached[1]) < self._leverage_ttl:
                hit += 1
                continue

            market = markets.get(sym, {})
            if not market:
                continue

            max_lev: int = 10  # default
            # OKX native field
            okx_lev = market.get("info", {}).get("maxLever")
            if okx_lev:
                try:
                    parsed = int(float(okx_lev))
                    if parsed > 0:
                        max_lev = parsed
                except (ValueError, TypeError):
                    pass

            if max_lev == 10:
                ccxt_lev = (
                    market.get("limits", {})
                          .get("leverage", {})
                          .get("max")
                )
                if ccxt_lev:
                    try:
                        parsed = int(float(ccxt_lev))
                        if parsed > 0:
                            max_lev = parsed
                    except (ValueError, TypeError):
                        pass

            with self._leverage_lock:
                self._leverage_cache[sym] = (max_lev, now)

        fetched = len(symbols) - hit
        logger.info(
            f"✅ Leverage cache warm-up selesai: "
            f"{fetched} fetched, {hit} already cached"
        )

    # ─────────────────────────────────────────────
    # load_markets
    # ─────────────────────────────────────────────
    @_with_retry(max_retries=3, base_delay=2.0)
    def load_markets(self) -> dict:
        """
        Load semua market info dari OKX.
        Di-cache oleh CCXT secara internal.
        """
        t0 = time.time()
        markets = self._ex.load_markets()
        self._dbg("load_markets", "all", t0, len(markets))
        return markets

    # ─────────────────────────────────────────────
    # fetch_balance
    # ─────────────────────────────────────────────
    @_with_retry(max_retries=2, base_delay=1.0)
    def fetch_balance(self) -> dict:
        """Fetch akun balance. Butuh API key."""
        t0  = time.time()
        bal = self._ex.fetch_balance()
        self._dbg("fetch_balance", "account", t0)
        return bal

    # ─────────────────────────────────────────────
    # Expose raw exchange untuk fitur lain (jika perlu)
    # ─────────────────────────────────────────────
    @property
    def raw(self) -> ccxt.okx:
        """
        Akses langsung ke objek ccxt.okx jika butuh method yang belum di-wrap.
        Gunakan dengan hati-hati — tidak ada retry/logging otomatis.
        """
        return self._ex


# Backward-compat alias — allows older callers that import BybitClient to work
BybitClient = OKXClient
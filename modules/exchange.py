"""
exchange.py — Best-practice CCXT/Bybit wrapper

Fitur:
  ✅ Auto-detect format symbol (BTC/USDT → BTC/USDT:USDT untuk swap)
  ✅ Retry dengan exponential backoff (RateLimitExceeded, NetworkError)
  ✅ Structured logging — setiap call tercatat dengan durasi & error
  ✅ Connection health-check saat startup
  ✅ Debug mode — print setiap request/response detail
  ✅ Satu titik error handling untuk seluruh bot

Cara pakai di main.py:
    from modules.exchange import BybitClient
    client = BybitClient(debug=False)   # debug=True untuk verbose
    client.health_check()              # cek koneksi saat startup
    df    = client.fetch_ohlcv('BTC/USDT:USDT', '1h', limit=100)
    ticker = client.fetch_ticker('ETH/USDT:USDT')
"""

import time
import logging
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
                    # Error dari Bybit (contoh: invalid parameter)
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
# BybitClient
# ─────────────────────────────────────────────────────────────────────────────
class BybitClient:
    """
    Wrapper tunggal untuk semua interaksi dengan Bybit via CCXT.

    Parameter
    ---------
    debug : bool
        True  → print setiap API call + durasi ke stdout
        False → hanya log WARNING/ERROR (default untuk produksi)
    """

    # Simbol yang tidak perlu diubah formatnya
    _SYMBOL_CACHE: dict[str, str] = {}

    def __init__(self, debug: bool = False, auto_trade: bool = False):
        self.debug = debug
        self.auto_trade = auto_trade

        # Hanya set API key jika auto_trade=True.
        # Signal-only mode tidak butuh key — semua data dari public endpoint CCXT.
        # Tanpa key, CCXT tidak akan memanggil endpoint private apapun.
        api_cfg = {}
        if auto_trade:
            api_cfg = {
                "apiKey": CONFIG["api"].get("bybit_key",    ""),
                "secret": CONFIG["api"].get("bybit_secret", ""),
            }

        self._ex = ccxt.bybit({
            **api_cfg,
            "options": {
                "defaultType": "swap",            # Perpetual futures
                "adjustForTimeDifference": True,   # Auto-sync clock dengan Bybit server
                "recvWindow": 20_000,              # Toleransi clock meleset hingga 20 detik
                "unifiedAccount": True,            # Bypass auto-detect unified account
            },
            "enableRateLimit": True,              # CCXT built-in rate limiter
            "timeout": 15_000,                    # 15 detik timeout per request
        })

        logger.info("BybitClient initialized (mode=swap, rateLimit=ON)")

        # FIX: Perbesar connection pool agar tidak "pool is full" saat multi-thread
        # Default pool size = 10, tapi bot pakai 20 threads → naik ke 30
        from requests.adapters import HTTPAdapter
        _adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30)
        self._ex.session.mount("https://", _adapter)

    # ─────────────────────────────────────────────
    # Symbol normalization
    # ─────────────────────────────────────────────
    def normalize_symbol(self, symbol: str) -> str:
        """
        Konversi symbol ke format Bybit Perpetual yang benar.

        Contoh:
          'BTC/USDT'      → 'BTC/USDT:USDT'
          'ETHUSDT'       → 'ETH/USDT:USDT'
          'BTC/USDT:USDT' → 'BTC/USDT:USDT'  (tidak diubah)
        """
        if symbol in self._SYMBOL_CACHE:
            return self._SYMBOL_CACHE[symbol]

        normalized = symbol

        # Format 'BTCUSDT' → 'BTC/USDT:USDT'
        if "/" not in symbol and symbol.endswith("USDT"):
            base = symbol[:-4]
            normalized = f"{base}/USDT:USDT"

        # Format 'BTC/USDT' → 'BTC/USDT:USDT'
        elif "/" in symbol and ":" not in symbol:
            normalized = f"{symbol}:USDT"

        self._SYMBOL_CACHE[symbol] = normalized

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
        Verifikasi koneksi ke Bybit dan validasi API key.
        Print ringkasan status ke stdout.
        Return True jika OK, False jika ada masalah.

        Parameter
        ---------
        auto_trade : bool
            True  → private API wajib OK (bot akan trade)
            False → private API opsional (signal-only, data publik cukup)
        """
        print("\n" + "─" * 50)
        print("🔌 Bybit Connection Health Check")
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

        # 2. Test API key (private endpoint) — hanya wajib jika auto_trade=True
        api_key = CONFIG["api"].get("bybit_key", "")
        if api_key and api_key != "YOUR_BYBIT_API_KEY":
            try:
                t0 = time.time()
                bal = self._ex.fetch_balance(params={"accountType": "UNIFIED"})
                ms  = (time.time() - t0) * 1000
                usdt = bal.get("USDT", {}).get("free", 0)
                print(f"  ✅ Private API — OK ({ms:.0f}ms) | USDT Balance: {usdt:,.2f}")
            except Exception as e:
                if auto_trade:
                    # Auto trade mode: private API wajib → ini error fatal
                    print(f"  ❌ Private API — AUTH GAGAL: {e}")
                    print(f"     → Cek bybit_key & bybit_secret di config.json")
                    ok = False
                else:
                    # Signal-only mode: private API tidak dipakai → cukup warning
                    print(f"  ⚠️  Private API — Tidak bisa diakses (signal-only mode, tidak masalah)")
                    print(f"     → Jika ingin auto trade, tambahkan permission 'Derivatives Read' di API key Bybit")
        else:
            print(f"  ⚠️  Private API — API key tidak dikonfigurasi (signal-only mode OK)")

        # 3. Format symbol
        test_sym = self.normalize_symbol("BTC/USDT")
        print(f"  ℹ️  Symbol format — 'BTC/USDT' → '{test_sym}'")

        # 4. Rate limit status
        print(f"  ℹ️  Rate limit    — CCXT built-in enabled")
        print(f"  ℹ️  Timeout       — 15 detik per request")
        print("─" * 50 + "\n")

        return ok

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

        Return None jika data tidak cukup (bukan exception).
        Kolom: timestamp (datetime), open, high, low, close, volume

        Contoh:
            df = client.fetch_ohlcv('BTC/USDT:USDT', '1h', limit=200)
            if df is None:
                return  # data tidak cukup
        """
        sym = self.normalize_symbol(symbol)
        t0  = time.time()

        bars = self._ex.fetch_ohlcv(sym, timeframe, limit=limit)
        self._dbg("fetch_ohlcv", sym, t0, len(bars) if bars else 0)

        if not bars or len(bars) < 10:
            logger.warning(f"fetch_ohlcv [{sym}/{timeframe}] — data kurang ({len(bars) if bars else 0} bars)")
            return None

        df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})

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
    # load_markets
    # ─────────────────────────────────────────────
    @_with_retry(max_retries=3, base_delay=2.0)
    def load_markets(self) -> dict:
        """
        Load semua market info dari Bybit.
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
    def raw(self) -> ccxt.bybit:
        """
        Akses langsung ke objek ccxt.bybit jika butuh method yang belum di-wrap.
        Gunakan dengan hati-hati — tidak ada retry/logging otomatis.
        """
        return self._ex
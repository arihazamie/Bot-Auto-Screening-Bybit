"""
manual_position_reader.py — READ-ONLY Bybit position poller.

Tugas:
  • Poll posisi terbuka di Bybit Unified Trading (USDT-Perp) via pybit.
  • Diff vs state lokal: deteksi posisi BARU, posisi yang DITUTUP manual,
    dan perubahan SIZE (partial close manual).
  • TIDAK pernah memanggil endpoint write/order. Bot ini READ-ONLY by design.

Dipakai oleh `manual_assistant.py`. Kembali return objek ringkas (dict)
yang siap dipakai oleh market-context + advisor + notifier.

Secrets:
  Diambil via `modules.secrets_loader.get_bybit_keys()` — env var dulu,
  fallback ke config.json. API key cukup permission `Read` saja.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger("ManualReader")


@dataclass
class ManualPosition:
    """Snapshot satu posisi pada satu waktu poll."""
    symbol: str            # contoh: "BTCUSDT" (format Bybit native, tanpa slash)
    side: str              # "Long" | "Short"
    size: float            # qty kontrak
    entry_price: float     # avgEntryPrice
    mark_price: float      # markPrice
    leverage: float
    liq_price: float       # liqPrice (0 = tidak tersedia)
    unrealised_pnl: float
    unrealised_pnl_pct: float   # vs notional posisi
    position_idx: int      # 0 = one-way, 1/2 = hedge mode
    raw_ts: float = field(default_factory=time.time)

    def key(self) -> str:
        """Identitas unik posisi (symbol + sisi + positionIdx)."""
        return f"{self.symbol}|{self.side}|{self.position_idx}"


def _parse_position_row(row: dict) -> ManualPosition | None:
    """
    Konversi satu baris response `get_positions` pybit ke ManualPosition.
    Skip baris dengan size 0 (Bybit kembalikan baris kosong untuk hedge mode).
    """
    try:
        size = float(row.get("size", 0) or 0)
        if size <= 0:
            return None

        side_raw = (row.get("side", "") or "").strip()
        if side_raw not in ("Buy", "Sell"):
            return None
        side = "Long" if side_raw == "Buy" else "Short"

        entry = float(row.get("avgPrice", row.get("entryPrice", 0)) or 0)
        if entry <= 0:
            return None

        mark = float(row.get("markPrice", 0) or 0) or entry
        lev  = float(row.get("leverage", 0) or 0)
        liq  = float(row.get("liqPrice", 0) or 0)
        upnl = float(row.get("unrealisedPnl", 0) or 0)
        idx  = int(row.get("positionIdx", 0) or 0)

        # PnL % vs notional. Pakai mark relatif entry — independen dari leverage,
        # supaya angka yang dikirim ke LLM konsisten lintas posisi.
        if side == "Long":
            pnl_pct = (mark - entry) / entry * 100.0
        else:
            pnl_pct = (entry - mark) / entry * 100.0

        return ManualPosition(
            symbol=str(row.get("symbol", "")).upper(),
            side=side,
            size=size,
            entry_price=entry,
            mark_price=mark,
            leverage=lev,
            liq_price=liq,
            unrealised_pnl=upnl,
            unrealised_pnl_pct=pnl_pct,
            position_idx=idx,
        )
    except (TypeError, ValueError) as e:
        logger.debug(f"_parse_position_row skipped: {e}")
        return None


class ManualPositionReader:
    """
    Polling read-only ke Bybit. Dibuat lazy supaya unit test / dry-run tidak
    butuh kredensial nyata.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        testnet: bool = False,
        category: str = "linear",
    ):
        self._api_key    = api_key
        self._api_secret = api_secret
        self._testnet    = testnet
        self._category   = category
        self._client     = None  # lazy init

    # ────────────────────────────────────────────────────────────
    # Lazy client init — supaya import module tidak crash kalau pybit
    # belum terinstal di mesin tester.
    # ────────────────────────────────────────────────────────────
    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            from pybit.unified_trading import HTTP
        except ImportError as e:
            raise RuntimeError(
                "pybit belum terinstal. Jalankan `pip install -r requirements.txt`."
            ) from e

        self._client = HTTP(
            testnet=self._testnet,
            api_key=self._api_key or None,
            api_secret=self._api_secret or None,
            recv_window=20_000,
        )
        return self._client

    # ────────────────────────────────────────────────────────────
    # Public API
    # ────────────────────────────────────────────────────────────
    def fetch_open_positions(self) -> list[ManualPosition] | None:
        """
        Return semua posisi terbuka (size > 0). Untuk Bybit Unified, harus pakai
        `settleCoin=USDT` untuk linear perpetuals supaya tidak butuh symbol filter.

        Return value:
          • list[ManualPosition] (mungkin kosong) → call sukses, list adalah
            snapshot lengkap posisi terbuka.
          • None                                  → call GAGAL (network/auth/
            retCode!=0). Caller WAJIB skip diff supaya tidak generate notifikasi
            "POSITION CLOSED" palsu untuk posisi yang sebenarnya masih terbuka.

        READ-ONLY: hanya panggil GET /v5/position/list.
        """
        client = self._ensure_client()
        try:
            resp = client.get_positions(category=self._category, settleCoin="USDT")
        except Exception as e:
            logger.error(f"get_positions gagal: {type(e).__name__}: {e}")
            return None

        if not isinstance(resp, dict) or resp.get("retCode", -1) != 0:
            logger.error(f"get_positions retCode != 0: {resp}")
            return None

        rows = resp.get("result", {}).get("list", []) or []
        out: list[ManualPosition] = []
        for r in rows:
            p = _parse_position_row(r)
            if p is not None:
                out.append(p)
        return out

    # ────────────────────────────────────────────────────────────
    # Stateful diff helper
    # ────────────────────────────────────────────────────────────
    def diff(
        self,
        previous: dict[str, ManualPosition],
        current: Iterable[ManualPosition],
    ) -> dict[str, list]:
        """
        Tentukan perubahan antara snapshot sebelum dan sekarang.

        Return dict dengan key:
          opened : list[ManualPosition]            — posisi baru terdeteksi
          closed : list[ManualPosition]            — posisi hilang dari Bybit (closed)
          changed: list[tuple[old, new]]           — size berubah (partial close manual)
          steady : list[ManualPosition]            — posisi tetap (size sama)
        """
        cur_map = {p.key(): p for p in current}

        opened, closed, changed, steady = [], [], [], []

        for k, new in cur_map.items():
            old = previous.get(k)
            if old is None:
                opened.append(new)
            elif abs(old.size - new.size) > 1e-12:
                changed.append((old, new))
            else:
                steady.append(new)

        for k, old in previous.items():
            if k not in cur_map:
                closed.append(old)

        return {
            "opened":  opened,
            "closed":  closed,
            "changed": changed,
            "steady":  steady,
        }

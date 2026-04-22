"""
database.py — SQLite Storage (built-in Python, zero external dependencies)
Single file  : data/bot.db
Journal mode : WAL  → concurrent reads, serialised writes, no corruption on crash

Tables
------
  signals        — signal queue  (main.py → paper_runner / auto_trades)
  active_trades  — live / pending positions
  sent_trades    — sinyal yang sudah dikirim ke Telegram
  paper_state    — paper trading balance  (single row, id = 1)
  daily_reports  — daily performance reports
  state_store    — generic key-value  (dashboard msg id, dll)
"""

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

from modules.config_loader import CONFIG

# ─── Path ─────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
DB_PATH  = os.path.join(BASE_DIR, 'bot.db')

# ─── Thread-local connections ─────────────────────────────────────────────────
# Each thread owns its own sqlite3.Connection — avoids "check_same_thread" issues.
# WAL mode: multiple readers run in parallel; writers serialise at the DB level.
_local    = threading.local()
_BOOL_COLS = {'ingested', 'is_sl_moved', '_tp1_logged', '_tp2_logged'}
_SAFE_COL  = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')   # column name whitelist


def _conn() -> sqlite3.Connection:
    """Return (or create) the per-thread SQLite connection."""
    if not hasattr(_local, 'conn'):
        os.makedirs(BASE_DIR, exist_ok=True)
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")   # safe with WAL, faster than FULL
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return _local.conn


def _to_dict(row) -> dict | None:
    """sqlite3.Row → plain dict; bool columns restored to Python bool."""
    if row is None:
        return None
    d = dict(row)
    for col in _BOOL_COLS:
        if col in d and d[col] is not None:
            d[col] = bool(d[col])
    return d


def _to_int(val) -> int:
    """Python bool / truthy → int for SQLite boolean columns."""
    return 1 if val else 0


# ─── Init ──────────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(BASE_DIR, exist_ok=True)
    c = _conn()
    c.executescript("""
        -- ── signals ──────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS signals (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol           TEXT,
            side             TEXT,
            timeframe        TEXT,
            entry_price      REAL,
            sl_price         REAL,
            tp1              REAL,
            tp2              REAL,
            tp3              REAL,
            rr               REAL,
            pattern          TEXT,
            btc_bias         TEXT,
            telegram_msg_id  INTEGER,
            ingested         INTEGER NOT NULL DEFAULT 0,
            created_at       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_signals_ingested ON signals (ingested);

        -- ── active_trades ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS active_trades (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id        INTEGER,
            symbol           TEXT,
            side             TEXT,
            timeframe        TEXT,
            entry_price      REAL,
            sl_price         REAL,
            tp1              REAL,
            tp2              REAL,
            tp3              REAL,
            quantity         REAL,
            leverage         INTEGER,
            mode             TEXT,
            status           TEXT    NOT NULL DEFAULT 'PENDING',
            order_id         TEXT,
            is_sl_moved      INTEGER NOT NULL DEFAULT 0,
            _tp1_logged      INTEGER NOT NULL DEFAULT 0,
            _tp2_logged      INTEGER NOT NULL DEFAULT 0,
            pnl              REAL    NOT NULL DEFAULT 0,
            telegram_msg_id  INTEGER,
            created_at       TEXT,
            updated_at       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_at_symbol  ON active_trades (symbol);
        CREATE INDEX IF NOT EXISTS idx_at_status  ON active_trades (status);
        CREATE INDEX IF NOT EXISTS idx_at_updated ON active_trades (updated_at);
        CREATE INDEX IF NOT EXISTS idx_at_created ON active_trades (created_at);

        -- ── sent_trades ───────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS sent_trades (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol           TEXT,
            side             TEXT,
            timeframe        TEXT,
            pattern          TEXT,
            entry_price      REAL,
            sl_price         REAL,
            tp1              REAL,
            tp2              REAL,
            tp3              REAL,
            rr               REAL,
            reason           TEXT,
            tech_score       REAL,
            quant_score      REAL,
            deriv_score      REAL,
            smc_score        REAL,
            basis            TEXT,
            btc_bias         TEXT,
            z_score          REAL,
            zeta_score       REAL,
            obi              REAL,
            tech_reasons     TEXT,
            quant_reasons    TEXT,
            deriv_reasons    TEXT,
            smc_reasons      TEXT,
            message_id       TEXT,
            channel_id       TEXT,
            status           TEXT NOT NULL DEFAULT 'OPEN',
            created_at       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_st_status ON sent_trades (status);

        -- ── paper_state ───────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS paper_state (
            id         INTEGER PRIMARY KEY DEFAULT 1,
            balance    REAL    NOT NULL,
            updated_at TEXT
        );

        -- ── daily_reports ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS daily_reports (
            date_str TEXT PRIMARY KEY,
            data     TEXT NOT NULL
        );

        -- ── state_store ───────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS state_store (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)

    # Seed paper_state (single row, never deleted)
    c.execute(
        "INSERT OR IGNORE INTO paper_state (id, balance, updated_at) VALUES (1, ?, ?)",
        (CONFIG['risk']['paper_balance'], str(datetime.now()))
    )
    c.commit()
    print(f"SQLite ready (WAL) — {DB_PATH}")


# ─── Signals ───────────────────────────────────────────────────────────────────

def insert_signal(data: dict) -> int:
    c   = _conn()
    cur = c.execute(
        """INSERT INTO signals
           (symbol, side, timeframe, entry_price, sl_price,
            tp1, tp2, tp3, rr, pattern, btc_bias, telegram_msg_id, ingested, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
        (
            data.get('symbol'),          data.get('side'),
            data.get('timeframe'),       data.get('entry_price'),
            data.get('sl_price'),        data.get('tp1'),
            data.get('tp2'),             data.get('tp3'),
            data.get('rr'),              data.get('pattern'),
            data.get('btc_bias'),        data.get('telegram_msg_id'),
            str(datetime.now()),
        )
    )
    c.commit()
    return cur.lastrowid


def get_waiting_signals() -> list:
    rows = _conn().execute(
        "SELECT * FROM signals WHERE ingested = 0 ORDER BY id"
    ).fetchall()
    return [_to_dict(r) for r in rows]


def mark_signal_ingested(signal_id: int):
    c = _conn()
    c.execute("UPDATE signals SET ingested = 1 WHERE id = ?", (signal_id,))
    c.commit()


# ─── Active Trades ─────────────────────────────────────────────────────────────

def insert_active_trade(data: dict) -> int:
    c   = _conn()
    now = str(datetime.now())
    cur = c.execute(
        """INSERT INTO active_trades
           (signal_id, symbol, side, timeframe, entry_price, sl_price,
            tp1, tp2, tp3, quantity, leverage, mode,
            status, order_id, is_sl_moved, pnl, telegram_msg_id,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            data.get('signal_id'),                    data.get('symbol'),
            data.get('side'),                         data.get('timeframe'),
            data.get('entry_price'),                  data.get('sl_price'),
            data.get('tp1'),                          data.get('tp2'),
            data.get('tp3'),                          data.get('quantity'),
            data.get('leverage'),                     data.get('mode'),
            data.get('status', 'PENDING'),            data.get('order_id'),
            _to_int(data.get('is_sl_moved', False)),  data.get('pnl', 0),
            data.get('telegram_msg_id'),              now, now,
        )
    )
    c.commit()
    return cur.lastrowid


def update_active_trade(trade_id: int, fields: dict):
    """Dynamic UPDATE — only touches the columns present in fields."""
    if not fields:
        return

    # Only allow safe SQL identifiers as column names
    safe = {k: v for k, v in fields.items() if _SAFE_COL.match(k)}
    if not safe:
        return

    safe['updated_at'] = str(datetime.now())

    # Convert bool → int for SQLite boolean columns
    for col in _BOOL_COLS:
        if col in safe:
            safe[col] = _to_int(safe[col])

    set_clause = ', '.join(f'"{k}" = ?' for k in safe)
    values     = list(safe.values()) + [trade_id]

    c = _conn()
    c.execute(f"UPDATE active_trades SET {set_clause} WHERE id = ?", values)
    c.commit()


def get_active_trade_by_symbol(symbol: str, status: str = None):
    if status:
        row = _conn().execute(
            "SELECT * FROM active_trades WHERE symbol = ? AND status = ? LIMIT 1",
            (symbol, status)
        ).fetchone()
    else:
        row = _conn().execute(
            """SELECT * FROM active_trades
               WHERE symbol = ? AND status NOT IN ('CLOSED','CANCELLED','FAILED')
               LIMIT 1""",
            (symbol,)
        ).fetchone()
    return _to_dict(row)


def get_active_trades_by_status(statuses: list) -> list:
    placeholders = ','.join('?' * len(statuses))
    rows = _conn().execute(
        f"SELECT * FROM active_trades WHERE status IN ({placeholders})",
        statuses
    ).fetchall()
    return [_to_dict(r) for r in rows]


def count_open_active_trades() -> int:
    row = _conn().execute(
        "SELECT COUNT(*) FROM active_trades WHERE status NOT IN ('CLOSED','CANCELLED','FAILED')"
    ).fetchone()
    return row[0]


def get_closed_trades_last_24h() -> list:
    since = str(datetime.now() - timedelta(hours=24))
    rows  = _conn().execute(
        "SELECT * FROM active_trades WHERE status = 'CLOSED' AND updated_at >= ?",
        (since,)
    ).fetchall()
    return [_to_dict(r) for r in rows]


def get_closed_trades_today() -> list:
    today = datetime.now().date().isoformat()
    rows  = _conn().execute(
        "SELECT * FROM active_trades WHERE status = 'CLOSED' AND DATE(updated_at) = ?",
        (today,)
    ).fetchall()
    return [_to_dict(r) for r in rows]


def get_trades_last_24h() -> list:
    since = str(datetime.now() - timedelta(hours=24))
    rows  = _conn().execute(
        "SELECT * FROM active_trades WHERE created_at >= ?",
        (since,)
    ).fetchall()
    return [_to_dict(r) for r in rows]


def get_trades_today() -> list:
    today = datetime.now().date().isoformat()
    rows  = _conn().execute(
        "SELECT * FROM active_trades WHERE DATE(created_at) = ?",
        (today,)
    ).fetchall()
    return [_to_dict(r) for r in rows]


# ─── Paper State ───────────────────────────────────────────────────────────────

def get_paper_balance() -> float:
    row = _conn().execute("SELECT balance FROM paper_state WHERE id = 1").fetchone()
    return float(row['balance']) if row else float(CONFIG['risk']['paper_balance'])


def update_paper_balance(new_balance: float):
    c = _conn()
    c.execute(
        "UPDATE paper_state SET balance = ?, updated_at = ? WHERE id = 1",
        (new_balance, str(datetime.now()))
    )
    c.commit()


# ─── Daily Reports ─────────────────────────────────────────────────────────────

def save_daily_report(date_str: str, data: dict):
    c = _conn()
    c.execute(
        "INSERT OR REPLACE INTO daily_reports (date_str, data) VALUES (?, ?)",
        (date_str, json.dumps(data, default=str))
    )
    c.commit()


# ─── Sent Trades ───────────────────────────────────────────────────────────────

def insert_trade(data: dict) -> int:
    c   = _conn()
    cur = c.execute(
        """INSERT INTO sent_trades
           (symbol, side, timeframe, pattern, entry_price, sl_price,
            tp1, tp2, tp3, rr, reason, tech_score, quant_score,
            deriv_score, smc_score, basis, btc_bias, z_score, zeta_score,
            obi, tech_reasons, quant_reasons, deriv_reasons, smc_reasons,
            message_id, channel_id, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            data.get('symbol'),        data.get('side'),          data.get('timeframe'),
            data.get('pattern'),       data.get('entry_price'),   data.get('sl_price'),
            data.get('tp1'),           data.get('tp2'),           data.get('tp3'),
            data.get('rr'),            data.get('reason'),        data.get('tech_score'),
            data.get('quant_score'),   data.get('deriv_score'),   data.get('smc_score'),
            data.get('basis'),         data.get('btc_bias'),      data.get('z_score'),
            data.get('zeta_score'),    data.get('obi'),           data.get('tech_reasons'),
            data.get('quant_reasons'), data.get('deriv_reasons'), data.get('smc_reasons'),
            data.get('message_id'),    data.get('channel_id'),
            data.get('status', 'OPEN'), str(datetime.now()),
        )
    )
    c.commit()
    return cur.lastrowid


def get_trades_open() -> list:
    rows = _conn().execute(
        "SELECT * FROM sent_trades WHERE status = 'OPEN' ORDER BY id"
    ).fetchall()
    return [_to_dict(r) for r in rows]


def get_active_signals() -> set:
    """Return set of (symbol, timeframe) for non-closed sent_trades."""
    rows = _conn().execute(
        "SELECT symbol, timeframe FROM sent_trades "
        "WHERE status NOT IN ('CLOSED','CANCELLED','FAILED')"
    ).fetchall()
    return {(r['symbol'], r['timeframe']) for r in rows}


# ─── State Store ───────────────────────────────────────────────────────────────

def get_state(key: str):
    row = _conn().execute(
        "SELECT value FROM state_store WHERE key = ?", (key,)
    ).fetchone()
    return row['value'] if row else None


def set_state(key: str, value: str):
    c = _conn()
    c.execute(
        "INSERT OR REPLACE INTO state_store (key, value) VALUES (?, ?)",
        (key, value)
    )
    c.commit()


# ─── Candle Confirm State (FIX #07) ────────────────────────────────────────────
# Menyimpan pending candle confirmation ke DB agar tidak hilang saat bot restart.
# Format key  : "candle_confirm:{symbol}:{pattern}:{side}"
# Format value: JSON {"bar_ts": <int ms>, "saved_at": <float unix>}

def get_candle_confirm_state(key: str) -> "int | None":
    """
    Return bar_ts (int ms) yang tersimpan untuk key konfirmasi candle,
    atau None jika belum ada.
    """
    row = _conn().execute(
        "SELECT value FROM state_store WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["value"])
        return int(data["bar_ts"])
    except Exception:
        return None


def set_candle_confirm_state(key: str, bar_ts: int) -> None:
    """Simpan (atau update) pending candle confirmation ke DB."""
    import time as _time
    payload = json.dumps({"bar_ts": bar_ts, "saved_at": _time.time()})
    set_state(key, payload)


def delete_candle_confirm_state(key: str) -> None:
    """Hapus satu entry candle confirmation (dipanggil setelah pattern terkonfirmasi)."""
    c = _conn()
    c.execute("DELETE FROM state_store WHERE key = ?", (key,))
    c.commit()


def purge_candle_confirm_state(max_age_hours: float = 4.0) -> int:
    """
    Hapus semua entri candle confirmation yang tersimpan lebih lama dari
    `max_age_hours` jam. Mencegah akumulasi state lama dari pattern yang
    tidak pernah muncul lagi.

    Dipanggil otomatis oleh purge_old_data() (via scheduler di main.py).

    Return: jumlah baris yang dihapus.
    """
    import time as _time
    cutoff_ts = _time.time() - (max_age_hours * 3600)

    # Ambil semua kunci candle_confirm lalu filter berdasarkan saved_at
    rows = _conn().execute(
        "SELECT key, value FROM state_store WHERE key LIKE 'candle_confirm:%'"
    ).fetchall()

    stale_keys = []
    for row in rows:
        try:
            data = json.loads(row["value"])
            if float(data.get("saved_at", 0)) < cutoff_ts:
                stale_keys.append(row["key"])
        except Exception:
            stale_keys.append(row["key"])   # JSON rusak → hapus juga

    if not stale_keys:
        return 0

    c = _conn()
    c.executemany(
        "DELETE FROM state_store WHERE key = ?",
        [(k,) for k in stale_keys]
    )
    c.commit()
    return len(stale_keys)


# ─── Signal Queue helper (used by main.py / auto_trades.py) ───────────────────

# ─── Data Cleanup / TTL ────────────────────────────────────────────────────────

def purge_old_data(
    signals_days: int = 7,
    closed_trades_days: int = 90,
    sent_trades_days: int = 90,
    daily_reports_days: int = 180,
) -> dict:
    """
    Hapus data lama dari semua tabel untuk mencegah disk/memory leak.

    Aturan retensi default (bisa di-override via argumen):
      • signals          – ingested & lebih lama dari `signals_days`     (default 7 hari)
      • active_trades    – status CLOSED/CANCELLED/FAILED & lebih lama
                           dari `closed_trades_days`                      (default 90 hari)
      • sent_trades      – status CLOSED/CANCELLED/FAILED & lebih lama
                           dari `sent_trades_days`                        (default 90 hari)
      • daily_reports    – lebih lama dari `daily_reports_days`           (default 180 hari)

    Return: dict dengan jumlah baris yang dihapus per tabel.
    """
    now = datetime.now()
    cutoffs = {
        'signals':       str(now - timedelta(days=signals_days)),
        'closed_trades': str(now - timedelta(days=closed_trades_days)),
        'sent_trades':   str(now - timedelta(days=sent_trades_days)),
        'daily_reports': (now - timedelta(days=daily_reports_days)).date().isoformat(),
    }

    c = _conn()
    deleted: dict = {}

    # 1. Sinyal yang sudah di-ingest dan kadaluarsa
    cur = c.execute(
        "DELETE FROM signals WHERE ingested = 1 AND created_at < ?",
        (cutoffs['signals'],)
    )
    deleted['signals'] = cur.rowcount

    # 2. Active trades yang sudah closed/cancelled/failed dan kadaluarsa
    cur = c.execute(
        """DELETE FROM active_trades
           WHERE status IN ('CLOSED','CANCELLED','FAILED')
             AND updated_at < ?""",
        (cutoffs['closed_trades'],)
    )
    deleted['active_trades'] = cur.rowcount

    # 3. Sent trades yang sudah selesai dan kadaluarsa
    cur = c.execute(
        """DELETE FROM sent_trades
           WHERE status IN ('CLOSED','CANCELLED','FAILED')
             AND created_at < ?""",
        (cutoffs['sent_trades'],)
    )
    deleted['sent_trades'] = cur.rowcount

    # 4. Daily reports yang terlalu lama
    cur = c.execute(
        "DELETE FROM daily_reports WHERE date_str < ?",
        (cutoffs['daily_reports'],)
    )
    deleted['daily_reports'] = cur.rowcount

    c.commit()

    # Kembalikan ruang disk ke OS (WAL + auto_vacuum tidak cukup tanpa VACUUM)
    c.execute("VACUUM")

    # FIX #07: Bersihkan candle confirm state yang sudah terlalu lama (> 4 jam)
    stale_cc = purge_candle_confirm_state(max_age_hours=4.0)
    deleted["candle_confirm_state"] = stale_cc

    total = sum(deleted.values())
    print(
        f"[DB purge] Dihapus {total} baris — "
        + ", ".join(f"{t}: {n}" for t, n in deleted.items())
    )
    return deleted


def save_signal_to_db(res: dict, telegram_msg_id: int = None) -> int:
    """Convert analyze_ticker result → signal queue entry."""
    return insert_signal({
        "symbol":          res['Symbol'],
        "side":            res['Side'],
        "timeframe":       res['Timeframe'],
        "entry_price":     res['Entry'],
        "sl_price":        res['SL'],
        "tp1":             res['TP1'],
        "tp2":             res['TP2'],
        "tp3":             res['TP3'],
        "rr":              res['RR'],
        "pattern":         res['Pattern'],
        "btc_bias":        res['BTC_Bias'],
        "telegram_msg_id": telegram_msg_id,
    })
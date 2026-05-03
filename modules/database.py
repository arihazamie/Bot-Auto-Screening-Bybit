"""
database.py — SQLite Storage (built-in Python, zero external dependencies)
Single file  : data/bot.db
Journal mode : WAL  → concurrent reads, serialised writes, no corruption on crash

Tables
------
  signals        — signal queue  (main.py → paper_runner)
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
from datetime import datetime, timedelta, timezone

from modules.config_loader import CONFIG

# All DB timestamps are written as ISO-8601 UTC strings. SQLite still parses
# them with DATE()/datetime() because the leading 'YYYY-MM-DD HH:MM:SS' prefix
# matches its built-in parser. Storing UTC consistently fixes the local-vs-UTC
# mismatch that used to make MAX_DAILY_TRADES reset at local midnight.


def _utcnow_str() -> str:
    """Naive-format ISO string but anchored to UTC. Stored as 'YYYY-MM-DD HH:MM:SS.ffffff'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _utctoday_str() -> str:
    """UTC date as 'YYYY-MM-DD' for SQLite DATE() comparisons."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

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

        -- ── pattern_stats ────────────────────────────────────────────────────
        -- One row per closed paper trade, attributed to the originating
        -- pattern. `get_actual_winrate` aggregates only the trailing 30 days
        -- so old samples auto-fade (rolling stats; never reset).
        CREATE TABLE IF NOT EXISTS pattern_stats (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_name  TEXT    NOT NULL,
            symbol        TEXT,
            side          TEXT,
            outcome       TEXT    NOT NULL,           -- 'win' | 'loss' | 'breakeven'
            pnl_pct       REAL    NOT NULL DEFAULT 0,
            opened_at     TEXT,
            closed_at     TEXT    NOT NULL            -- UTC ISO; the rolling-window key
        );
        CREATE INDEX IF NOT EXISTS idx_ps_name_closed ON pattern_stats (pattern_name, closed_at);
        CREATE INDEX IF NOT EXISTS idx_ps_closed      ON pattern_stats (closed_at);
    """)

    # ── Idempotent schema migrations ─────────────────────────────────────────
    # SQLite has no `ADD COLUMN IF NOT EXISTS`, so we PRAGMA-introspect first.
    # Phase 8: registry_hits_json carries the pattern_registry hits from
    # signal → active_trade so paper-trader close can attribute outcomes.
    def _add_col_if_missing(table: str, col: str, decl: str) -> None:
        existing = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    _add_col_if_missing("signals",       "registry_hits_json", "TEXT")
    _add_col_if_missing("active_trades", "registry_hits_json", "TEXT")

    # Seed paper_state (single row, never deleted)
    c.execute(
        "INSERT OR IGNORE INTO paper_state (id, balance, updated_at) VALUES (1, ?, ?)",
        (CONFIG['risk']['paper_balance'], _utcnow_str())
    )
    c.commit()
    print(f"SQLite ready (WAL) — {DB_PATH}")


# ─── Signals ───────────────────────────────────────────────────────────────────

def insert_signal(data: dict) -> int:
    c   = _conn()
    cur = c.execute(
        """INSERT INTO signals
           (symbol, side, timeframe, entry_price, sl_price,
            tp1, tp2, tp3, rr, pattern, btc_bias, telegram_msg_id, ingested,
            registry_hits_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
        (
            data.get('symbol'),          data.get('side'),
            data.get('timeframe'),       data.get('entry_price'),
            data.get('sl_price'),        data.get('tp1'),
            data.get('tp2'),             data.get('tp3'),
            data.get('rr'),              data.get('pattern'),
            data.get('btc_bias'),        data.get('telegram_msg_id'),
            data.get('registry_hits_json'),
            _utcnow_str(),
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


def try_claim_signal(signal_id: int) -> bool:
    """
    Atomic claim of a waiting signal. Returns True iff this caller flipped
    `ingested` from 0 to 1. Used to prevent the paper-runner double-firing
    bug (TOCTOU between get_waiting_signals → insert_active_trade → mark).
    """
    c = _conn()
    cur = c.execute(
        "UPDATE signals SET ingested = 1 WHERE id = ? AND ingested = 0",
        (signal_id,),
    )
    c.commit()
    return cur.rowcount > 0


# ─── Active Trades ─────────────────────────────────────────────────────────────

def insert_active_trade(data: dict) -> int:
    c   = _conn()
    now = _utcnow_str()
    cur = c.execute(
        """INSERT INTO active_trades
           (signal_id, symbol, side, timeframe, entry_price, sl_price,
            tp1, tp2, tp3, quantity, leverage, mode,
            status, order_id, is_sl_moved, pnl, telegram_msg_id,
            registry_hits_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            data.get('signal_id'),                    data.get('symbol'),
            data.get('side'),                         data.get('timeframe'),
            data.get('entry_price'),                  data.get('sl_price'),
            data.get('tp1'),                          data.get('tp2'),
            data.get('tp3'),                          data.get('quantity'),
            data.get('leverage'),                     data.get('mode'),
            data.get('status', 'PENDING'),            data.get('order_id'),
            _to_int(data.get('is_sl_moved', False)),  data.get('pnl', 0),
            data.get('telegram_msg_id'),              data.get('registry_hits_json'),
            now, now,
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

    safe['updated_at'] = _utcnow_str()

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
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S.%f")
    rows  = _conn().execute(
        "SELECT * FROM active_trades WHERE status = 'CLOSED' AND updated_at >= ?",
        (since,)
    ).fetchall()
    return [_to_dict(r) for r in rows]


def get_closed_trades_today() -> list:
    today = _utctoday_str()
    rows  = _conn().execute(
        "SELECT * FROM active_trades WHERE status = 'CLOSED' AND DATE(updated_at) = ?",
        (today,)
    ).fetchall()
    return [_to_dict(r) for r in rows]


def get_trades_last_24h() -> list:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S.%f")
    rows  = _conn().execute(
        "SELECT * FROM active_trades WHERE created_at >= ?",
        (since,)
    ).fetchall()
    return [_to_dict(r) for r in rows]


def get_trades_today() -> list:
    today = _utctoday_str()
    rows  = _conn().execute(
        "SELECT * FROM active_trades WHERE DATE(created_at) = ?",
        (today,)
    ).fetchall()
    return [_to_dict(r) for r in rows]


def get_active_trades_today() -> list:
    """
    Like get_trades_today() but excludes CANCELLED/FAILED so that expired
    PENDING orders do not consume the daily-trade quota (fix #4).
    """
    today = _utctoday_str()
    rows  = _conn().execute(
        "SELECT * FROM active_trades "
        "WHERE DATE(created_at) = ? "
        "AND status NOT IN ('CANCELLED','FAILED')",
        (today,)
    ).fetchall()
    return [_to_dict(r) for r in rows]


# ─── Paper State ───────────────────────────────────────────────────────────────

def get_paper_balance() -> float:
    row = _conn().execute("SELECT balance FROM paper_state WHERE id = 1").fetchone()
    return float(row['balance']) if row else float(CONFIG['risk']['paper_balance'])


def update_paper_balance(new_balance: float):
    """Replace balance outright. Prefer add_paper_balance() for atomic deltas."""
    c = _conn()
    c.execute(
        "UPDATE paper_state SET balance = ?, updated_at = ? WHERE id = 1",
        (new_balance, _utcnow_str())
    )
    c.commit()


def add_paper_balance(delta: float) -> float:
    """
    Atomically apply +delta (negative for losses) to paper_state.balance and
    return the new balance. SQLite serialises writes in WAL mode, so this
    avoids the read-modify-write race that lost PnL when two trades closed
    concurrently (fix #1B).
    """
    c = _conn()
    cur = c.execute(
        "UPDATE paper_state SET balance = balance + ?, updated_at = ? "
        "WHERE id = 1",
        (float(delta), _utcnow_str()),
    )
    if cur.rowcount == 0:
        # Row missing (init failed?) — fall back to a seed insert.
        seed = float(CONFIG['risk'].get('paper_balance', 0)) + float(delta)
        c.execute(
            "INSERT OR REPLACE INTO paper_state (id, balance, updated_at) "
            "VALUES (1, ?, ?)",
            (seed, _utcnow_str()),
        )
        c.commit()
        return seed
    c.commit()
    row = c.execute("SELECT balance FROM paper_state WHERE id = 1").fetchone()
    return float(row['balance']) if row else 0.0


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
            data.get('status', 'OPEN'), _utcnow_str(),
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


# ─── Pattern stats (rolling 30-day winrate) ───────────────────────────────────

PATTERN_STATS_MIN_SAMPLES = 10
PATTERN_STATS_WINDOW_DAYS = 30


def _isofmt_cutoff(days: int) -> str:
    """Compute a rolling-window cutoff string in the same format used by
    :func:`_utcnow_str` (``'YYYY-MM-DD HH:MM:SS.ffffff'``). Mismatched formats
    (e.g. ``T``-separated ISO with ``+00:00``) would lexicographically miss
    every stored row, so this helper exists to keep the comparison apples-to-
    apples."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )


def record_pattern_outcome(
    pattern_name: str,
    *,
    symbol: str | None = None,
    side: str | None = None,
    outcome: str,
    pnl_pct: float = 0.0,
    opened_at: str | None = None,
    closed_at: str | None = None,
) -> int:
    """Insert one closed-trade outcome attributed to ``pattern_name``.

    ``outcome`` is one of ``"win"`` | ``"loss"`` | ``"breakeven"``.
    ``closed_at`` defaults to the current UTC ISO timestamp; this column
    is the key used by the rolling-30-day window in
    :func:`get_actual_winrate`.
    """
    if not pattern_name:
        raise ValueError("pattern_name is required")
    if outcome not in ("win", "loss", "breakeven"):
        raise ValueError(f"outcome must be win|loss|breakeven, got {outcome!r}")
    closed_at = closed_at or _utcnow_str()
    c = _conn()
    cur = c.execute(
        """INSERT INTO pattern_stats
             (pattern_name, symbol, side, outcome, pnl_pct, opened_at, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (pattern_name, symbol, side, outcome, float(pnl_pct), opened_at, closed_at),
    )
    c.commit()
    return cur.lastrowid


def get_actual_winrate(
    pattern_name: str,
    days: int = PATTERN_STATS_WINDOW_DAYS,
) -> tuple[float | None, int]:
    """Return ``(winrate, sample_count)`` for ``pattern_name`` over the last
    ``days`` days. ``winrate`` is ``None`` when ``sample_count`` is below
    :data:`PATTERN_STATS_MIN_SAMPLES` (insufficient data → caller should
    fall back to the literature baseline).

    Breakeven outcomes count toward sample size but neither win nor loss.
    """
    cutoff = _isofmt_cutoff(days)
    row = _conn().execute(
        """SELECT
             SUM(CASE WHEN outcome = 'win'  THEN 1 ELSE 0 END) AS wins,
             SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses,
             COUNT(*)                                         AS total
           FROM pattern_stats
           WHERE pattern_name = ? AND closed_at >= ?""",
        (pattern_name, cutoff),
    ).fetchone()
    if row is None:
        return None, 0
    # NOTE: _to_int is a *boolean* helper (returns 1 if truthy, 0 otherwise),
    # so we MUST use plain int() for SQL count/sum aggregates.
    total  = int(row["total"]  or 0)
    wins   = int(row["wins"]   or 0)
    losses = int(row["losses"] or 0)
    decisive = wins + losses
    if total < PATTERN_STATS_MIN_SAMPLES or decisive == 0:
        return None, total
    return wins / decisive, total


def get_pattern_stats_summary(days: int = PATTERN_STATS_WINDOW_DAYS) -> list[dict]:
    """Return per-pattern aggregate stats for the trailing window. Rows
    are ordered by sample count desc, then winrate desc. Useful for
    diagnostic Telegram /stats commands."""
    cutoff = _isofmt_cutoff(days)
    rows = _conn().execute(
        """SELECT
             pattern_name,
             SUM(CASE WHEN outcome = 'win'  THEN 1 ELSE 0 END) AS wins,
             SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses,
             COUNT(*)                                         AS total,
             AVG(pnl_pct)                                     AS avg_pnl_pct
           FROM pattern_stats
           WHERE closed_at >= ?
           GROUP BY pattern_name
           ORDER BY total DESC, wins DESC""",
        (cutoff,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        wins   = int(r["wins"]   or 0)
        losses = int(r["losses"] or 0)
        total  = int(r["total"]  or 0)
        decisive = wins + losses
        wr = (wins / decisive) if decisive else None
        out.append({
            "pattern_name": r["pattern_name"],
            "wins": wins,
            "losses": losses,
            "total": total,
            "winrate": wr,
            "avg_pnl_pct": r["avg_pnl_pct"] or 0.0,
        })
    return out


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


# ─── Signal Queue helper (used by main.py / paper_runner.py) ────────────────

# ─── Data Cleanup / TTL ────────────────────────────────────────────────────────

def purge_old_data(
    signals_days: int = 7,
    closed_trades_days: int = 90,
    sent_trades_days: int = 90,
    daily_reports_days: int = 180,
    pattern_stats_days: int = 90,
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
        'signals':        str(now - timedelta(days=signals_days)),
        'closed_trades':  str(now - timedelta(days=closed_trades_days)),
        'sent_trades':    str(now - timedelta(days=sent_trades_days)),
        'daily_reports':  (now - timedelta(days=daily_reports_days)).date().isoformat(),
        'pattern_stats':  (datetime.now(timezone.utc) - timedelta(days=pattern_stats_days)).isoformat(),
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

    # 5. Pattern stats di luar window analitik (default 90 hari, jauh > 30d
    #    rolling window yang dipakai get_actual_winrate, jadi tetap aman).
    cur = c.execute(
        "DELETE FROM pattern_stats WHERE closed_at < ?",
        (cutoffs['pattern_stats'],)
    )
    deleted['pattern_stats'] = cur.rowcount

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
    """Convert analyze_ticker result → signal queue entry. Phase 8: serialise
    pattern_registry hits as JSON so the paper-trader close path can attribute
    pnl back to each detected pattern."""
    hits = res.get("RegistryHits") or []
    # Strip non-serialisable junk defensively (the registry already returns
    # plain dicts of primitives, but a future detector might attach numpy types).
    safe_hits: list[dict] = []
    for h in hits:
        if not isinstance(h, dict):
            continue
        safe_hits.append({
            "name":     str(h.get("name", "")),
            "side":     str(h.get("side", "")),
            "details":  str(h.get("details", "")),
            "baseline": float(h["baseline"]) if h.get("baseline") is not None else None,
            "source":   str(h.get("source", "")),
        })
    registry_hits_json = json.dumps(safe_hits) if safe_hits else None

    return insert_signal({
        "symbol":             res['Symbol'],
        "side":               res['Side'],
        "timeframe":          res['Timeframe'],
        "entry_price":        res['Entry'],
        "sl_price":           res['SL'],
        "tp1":                res['TP1'],
        "tp2":                res['TP2'],
        "tp3":                res['TP3'],
        "rr":                 res['RR'],
        "pattern":            res['Pattern'],
        "btc_bias":           res['BTC_Bias'],
        "telegram_msg_id":    telegram_msg_id,
        "registry_hits_json": registry_hits_json,
    })


# ─── Pattern-attribution close hook ────────────────────────────────────────────

def record_trade_close_outcomes(
    trade_id: int,
    *,
    symbol: str,
    side: str,
    pnl: float,
    opened_at: str | None = None,
    closed_at: str | None = None,
    breakeven_band_pct: float = 0.0005,
) -> int:
    """Walk the closed trade's stored ``registry_hits_json`` and emit one
    :func:`record_pattern_outcome` row per attributed pattern.

    Outcome mapping:
      * ``win``       — ``pnl > +breakeven_band_pct × entry``  (default ±0.05%)
      * ``loss``      — ``pnl < -breakeven_band_pct × entry``
      * ``breakeven`` — within the band (or exactly zero)

    Always reads the originating ``Pattern`` text column too (the existing
    pipeline's primary pattern label) so attribution still works for trades
    that pre-date the phase 8 schema migration.

    Returns the number of pattern_stats rows inserted. Failures are logged
    but never raised — pattern stats are advisory; we don't want a JSON
    decode error to abort the close path.
    """
    import logging
    log = logging.getLogger("PatternStats")
    try:
        row = _conn().execute(
            "SELECT registry_hits_json, entry_price, quantity "
            "FROM active_trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
    except Exception as e:
        log.warning(f"trade {trade_id} close-attribution lookup failed: {e}")
        return 0
    if row is None:
        return 0

    entry = float(row["entry_price"] or 0.0)
    qty   = float(row["quantity"]    or 0.0)
    # Position-value is what we measure pnl against. The breakeven band must
    # be in dollar terms because ``pnl`` is total-dollar PnL (price diff × qty
    # less fees). Comparing dollar pnl to a per-unit price band silently
    # mislabels nearly every BTC trade as breakeven when the actual move is
    # a routine ±1% (caught by Devin Review on PR #21).
    position_value = entry * qty
    if position_value > 0:
        band = position_value * breakeven_band_pct
    else:
        # Defensive fallback for legacy rows without quantity recorded
        band = abs(entry) * breakeven_band_pct
    if pnl > band:
        outcome = "win"
    elif pnl < -band:
        outcome = "loss"
    else:
        outcome = "breakeven"

    try:
        hits_raw = row["registry_hits_json"]
        hits = json.loads(hits_raw) if hits_raw else []
    except (TypeError, ValueError) as e:
        log.warning(f"trade {trade_id} registry_hits_json decode failed: {e}")
        hits = []

    # ``pnl_pct`` is the price-change percentage (= ROI on notional) so it
    # stays comparable across coins with very different prices/quantities.
    if position_value > 0:
        pnl_pct = pnl / position_value * 100.0
    else:
        pnl_pct = 0.0
    n = 0
    seen: set[str] = set()
    for h in hits:
        name = str(h.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            record_pattern_outcome(
                name,
                symbol=symbol,
                side=side,
                outcome=outcome,
                pnl_pct=pnl_pct,
                opened_at=opened_at,
                closed_at=closed_at,
            )
            n += 1
        except Exception as e:
            log.warning(f"record_pattern_outcome({name!r}) failed: {e}")
    return n
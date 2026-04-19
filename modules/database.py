"""
database.py — JSON File Storage (no external DB required)
Data disimpan di folder /data sebagai file .json

Perbaikan v2:
  ✅ Atomic write — tulis ke temp file lalu os.replace() (cegah korupsi jika crash)
  ✅ _read() terima default eksplisit — tidak ada lagi hardcode path check fragile
  ✅ Auto-repair: file korup/kosong → reset ke default, bot tidak crash
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta

from modules.config_loader import CONFIG

# ─── Storage paths ─────────────────────────────────────────
BASE_DIR    = os.path.join(os.path.dirname(__file__), '..', 'data')
SIGNALS_F   = os.path.join(BASE_DIR, 'signals.json')
TRADES_F    = os.path.join(BASE_DIR, 'active_trades.json')
SENT_F      = os.path.join(BASE_DIR, 'sent_trades.json')   # sinyal yg sudah dikirim ke Telegram
STATE_F     = os.path.join(BASE_DIR, 'state.json')         # key-value store (dashboard msg id, dll)
PAPER_F     = os.path.join(BASE_DIR, 'paper_state.json')
REPORTS_F   = os.path.join(BASE_DIR, 'daily_reports.json')

_lock = threading.Lock()


# ─── Helpers ───────────────────────────────────────────────

def _read(path: str, default):
    """
    Baca JSON dari path. Jika file tidak ada atau korup, kembalikan default.
    File korup di-reset ke default otomatis — bot tidak crash saat startup.
    """
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return default
            return json.loads(content)
    except (json.JSONDecodeError, OSError) as e:
        print(f"warning [database] File korup, reset ke default: {os.path.basename(path)} | {e}")
        _write(path, default)
        return default


def _write(path: str, data):
    """
    Tulis JSON secara ATOMIC: temp file -> fsync -> os.replace().

    os.replace() atomic di Linux & Windows NTFS — jika proses mati
    di tengah write, file lama tetap utuh (tidak ada JSON parsial).
    """
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _next_id(records: list) -> int:
    return max((r['id'] for r in records), default=0) + 1


# ─── Init ──────────────────────────────────────────────────

def init_db():
    os.makedirs(BASE_DIR, exist_ok=True)
    for path, default in [
        (SIGNALS_F,  []),
        (TRADES_F,   []),
        (SENT_F,     []),
        (STATE_F,    {}),
        (REPORTS_F,  {}),
        (PAPER_F,    {"balance": CONFIG['risk']['paper_balance']}),
    ]:
        if not os.path.exists(path):
            _write(path, default)
    print("JSON storage ready (atomic write ON) -- folder: data/")


# ─── Signals ───────────────────────────────────────────────

def insert_signal(data: dict) -> int:
    with _lock:
        records = _read(SIGNALS_F, default=[])
        data['id'] = _next_id(records)
        data['ingested'] = False
        data['created_at'] = str(datetime.now())
        records.append(data)
        _write(SIGNALS_F, records)
        return data['id']


def get_waiting_signals() -> list:
    return [s for s in _read(SIGNALS_F, default=[]) if not s.get('ingested')]


def mark_signal_ingested(signal_id: int):
    with _lock:
        records = _read(SIGNALS_F, default=[])
        for r in records:
            if r['id'] == signal_id:
                r['ingested'] = True
        _write(SIGNALS_F, records)


# ─── Active Trades ─────────────────────────────────────────

def insert_active_trade(data: dict) -> int:
    with _lock:
        records = _read(TRADES_F, default=[])
        data['id'] = _next_id(records)
        data.setdefault('status', 'PENDING')
        data.setdefault('order_id', None)
        data.setdefault('is_sl_moved', False)
        data.setdefault('pnl', 0)
        data.setdefault('telegram_msg_id', None)
        data['created_at'] = str(datetime.now())
        data['updated_at'] = str(datetime.now())
        records.append(data)
        _write(TRADES_F, records)
        return data['id']


def update_active_trade(trade_id: int, fields: dict):
    with _lock:
        records = _read(TRADES_F, default=[])
        for r in records:
            if r['id'] == trade_id:
                r.update(fields)
                r['updated_at'] = str(datetime.now())
        _write(TRADES_F, records)


def get_active_trade_by_symbol(symbol: str, status: str = None):
    closed = {'CLOSED', 'CANCELLED', 'FAILED'}
    for r in _read(TRADES_F, default=[]):
        if r['symbol'] != symbol:
            continue
        if status and r.get('status') != status:
            continue
        if not status and r.get('status') in closed:
            continue
        return r
    return None


def get_active_trades_by_status(statuses: list) -> list:
    return [r for r in _read(TRADES_F, default=[]) if r.get('status') in statuses]


def count_open_active_trades() -> int:
    closed = {'CLOSED', 'CANCELLED', 'FAILED'}
    return sum(1 for r in _read(TRADES_F, default=[]) if r.get('status') not in closed)


def get_closed_trades_last_24h() -> list:
    since = datetime.now() - timedelta(hours=24)
    result = []
    for r in _read(TRADES_F, default=[]):
        if r.get('status') != 'CLOSED':
            continue
        try:
            updated = datetime.fromisoformat(r['updated_at'])
            if updated >= since:
                result.append(r)
        except Exception:
            pass
    return result


# ─── Paper State ───────────────────────────────────────────

def get_paper_balance() -> float:
    data = _read(PAPER_F, default={"balance": CONFIG['risk']['paper_balance']})
    return float(data.get('balance', CONFIG['risk']['paper_balance']))


def update_paper_balance(new_balance: float):
    with _lock:
        _write(PAPER_F, {"balance": new_balance, "updated_at": str(datetime.now())})


# ─── Daily Reports ─────────────────────────────────────────

def save_daily_report(date_str: str, data: dict):
    with _lock:
        reports = _read(REPORTS_F, default={})
        reports[date_str] = data
        _write(REPORTS_F, reports)


# ─── Sent Trades (sinyal yang sudah dikirim ke Telegram) ───

def insert_trade(data: dict) -> int:
    """Simpan sinyal yang sudah berhasil dikirim ke Telegram."""
    with _lock:
        records = _read(SENT_F, default=[])
        data['id'] = _next_id(records)
        data.setdefault('status', 'OPEN')
        data['created_at'] = str(datetime.now())
        records.append(data)
        _write(SENT_F, records)
        return data['id']


def get_trades_open() -> list:
    """Ambil semua sinyal dengan status OPEN untuk dashboard."""
    return [r for r in _read(SENT_F, default=[]) if r.get('status') == 'OPEN']


def get_active_signals() -> set:
    """
    Return set of (symbol, timeframe) dari sinyal yang masih aktif.
    Dipakai main.py untuk skip duplikat saat scanning.
    """
    closed = {'CLOSED', 'CANCELLED', 'FAILED'}
    result = set()
    for r in _read(SENT_F, default=[]):
        if r.get('status') not in closed:
            result.add((r.get('symbol', ''), r.get('timeframe', '')))
    return result


# ─── State Store (key-value untuk dashboard msg id, dll) ───

def get_state(key: str):
    """Ambil nilai dari state store. Return None jika tidak ada."""
    return _read(STATE_F, default={}).get(key)


def set_state(key: str, value: str):
    """Simpan nilai ke state store."""
    with _lock:
        data = _read(STATE_F, default={})
        data[key] = value
        _write(STATE_F, data)


# ─── Signal Queue (untuk auto_trades.py) ───────────────────

def save_signal_to_db(res: dict, telegram_msg_id: int = None) -> int:
    """
    Konversi hasil analyze_ticker ke format signal queue
    yang bisa dibaca paper_runner.py via get_waiting_signals().

    telegram_msg_id: Telegram message_id dari signal alert yang dikirim,
    disimpan agar paper_runner bisa reply ke pesan aslinya.
    """
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
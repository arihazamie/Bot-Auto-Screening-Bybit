"""
database.py — JSON File Storage (no PostgreSQL required)
Data disimpan di folder /data sebagai file .json
"""

import json
import os
import threading
from datetime import datetime, timedelta

from modules.config_loader import CONFIG

# ─── Storage paths ─────────────────────────────────────────
BASE_DIR    = os.path.join(os.path.dirname(__file__), '..', 'data')
SIGNALS_F   = os.path.join(BASE_DIR, 'signals.json')
TRADES_F    = os.path.join(BASE_DIR, 'active_trades.json')
PAPER_F     = os.path.join(BASE_DIR, 'paper_state.json')
REPORTS_F   = os.path.join(BASE_DIR, 'daily_reports.json')

_lock = threading.Lock()


# ─── Helpers ───────────────────────────────────────────────

def _read(path: str) -> list | dict:
    if not os.path.exists(path):
        return [] if path != PAPER_F and path != REPORTS_F else {}
    with open(path, 'r') as f:
        return json.load(f)


def _write(path: str, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=str)


def _next_id(records: list) -> int:
    return max((r['id'] for r in records), default=0) + 1


# ─── Init ──────────────────────────────────────────────────

def init_db():
    os.makedirs(BASE_DIR, exist_ok=True)
    for path, default in [
        (SIGNALS_F, []),
        (TRADES_F, []),
        (REPORTS_F, {}),
        (PAPER_F, {"balance": CONFIG['risk']['paper_balance']}),
    ]:
        if not os.path.exists(path):
            _write(path, default)
    print("✅ JSON storage ready — folder: data/")


# ─── Signals ───────────────────────────────────────────────

def insert_signal(data: dict) -> int:
    with _lock:
        records = _read(SIGNALS_F)
        data['id'] = _next_id(records)
        data['ingested'] = False
        data['created_at'] = str(datetime.now())
        records.append(data)
        _write(SIGNALS_F, records)
        return data['id']


def get_waiting_signals() -> list:
    return [s for s in _read(SIGNALS_F) if not s.get('ingested')]


def mark_signal_ingested(signal_id: int):
    with _lock:
        records = _read(SIGNALS_F)
        for r in records:
            if r['id'] == signal_id:
                r['ingested'] = True
        _write(SIGNALS_F, records)


# ─── Active Trades ─────────────────────────────────────────

def insert_active_trade(data: dict) -> int:
    with _lock:
        records = _read(TRADES_F)
        data['id'] = _next_id(records)
        data.setdefault('status', 'PENDING')
        data.setdefault('order_id', None)
        data.setdefault('is_sl_moved', False)
        data.setdefault('pnl', 0)
        data['created_at'] = str(datetime.now())
        data['updated_at'] = str(datetime.now())
        records.append(data)
        _write(TRADES_F, records)
        return data['id']


def update_active_trade(trade_id: int, fields: dict):
    with _lock:
        records = _read(TRADES_F)
        for r in records:
            if r['id'] == trade_id:
                r.update(fields)
                r['updated_at'] = str(datetime.now())
        _write(TRADES_F, records)


def get_active_trade_by_symbol(symbol: str, status: str = None):
    closed = {'CLOSED', 'CANCELLED', 'FAILED'}
    for r in _read(TRADES_F):
        if r['symbol'] != symbol:
            continue
        if status and r.get('status') != status:
            continue
        if not status and r.get('status') in closed:
            continue
        return r
    return None


def get_active_trades_by_status(statuses: list) -> list:
    return [r for r in _read(TRADES_F) if r.get('status') in statuses]


def count_open_active_trades() -> int:
    closed = {'CLOSED', 'CANCELLED', 'FAILED'}
    return sum(1 for r in _read(TRADES_F) if r.get('status') not in closed)


def get_closed_trades_last_24h() -> list:
    since = datetime.now() - timedelta(hours=24)
    result = []
    for r in _read(TRADES_F):
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
    data = _read(PAPER_F)
    return float(data.get('balance', CONFIG['risk']['paper_balance']))


def update_paper_balance(new_balance: float):
    with _lock:
        _write(PAPER_F, {"balance": new_balance, "updated_at": str(datetime.now())})


# ─── Daily Reports ─────────────────────────────────────────

def save_daily_report(date_str: str, data: dict):
    with _lock:
        reports = _read(REPORTS_F)
        reports[date_str] = data
        _write(REPORTS_F, reports)
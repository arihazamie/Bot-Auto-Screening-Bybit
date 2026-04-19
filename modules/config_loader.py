"""
config_loader.py — Load & validasi config.json

Behaviour:
  - config.json tidak ada      → pesan jelas + exit(1)
  - config.json tidak valid    → pesan jelas + exit(1)
  - Key wajib tidak ada/kosong → pesan jelas + exit(1)
  - Semua OK                   → CONFIG siap dipakai

Key wajib:
  - api.telegram_bot_token
  - api.telegram_chat_id
  - auto_trade
"""

import json
import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Key wajib & path-nya di dalam config ────────────────────────────────────
# Format: (label_tampilan, lambda akses ke nilai)
REQUIRED_KEYS = [
    ("api.telegram_bot_token", lambda c: c.get("api", {}).get("telegram_bot_token", "")),
    ("api.telegram_chat_id",   lambda c: c.get("api", {}).get("telegram_chat_id",   "")),
    ("auto_trade",             lambda c: c.get("auto_trade", None)),
]

CONFIG_PATH = "config.json"


def _send_telegram_error(token: str, chat_id: str, message: str):
    """Kirim pesan error ke Telegram jika token sudah tersedia."""
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(
            url,
            json={"chat_id": str(chat_id), "text": message, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception:
        pass  # Telegram gagal? Tidak masalah — terminal sudah cukup


def _abort(message: str, token: str = "", chat_id: str = ""):
    """Tampilkan error ke terminal, coba kirim ke Telegram, lalu exit."""
    border = "=" * 60
    full_msg = f"\n{border}\n❌  CONFIG ERROR\n{border}\n{message}\n{border}\n"
    print(full_msg, file=sys.stderr)

    tg_msg = (
        f"❌ <b>Bot gagal start — Config Error</b>\n\n"
        f"<pre>{message}</pre>\n\n"
        f"Periksa file <code>config.json</code> dan restart bot."
    )
    _send_telegram_error(token, chat_id, tg_msg)
    sys.exit(1)


def load_config() -> dict:
    # ── 1. Cek keberadaan file ────────────────────────────────────────────────
    if not os.path.exists(CONFIG_PATH):
        _abort(
            f"File '{CONFIG_PATH}' tidak ditemukan.\n\n"
            f"Jalankan perintah berikut untuk membuat konfigurasi:\n"
            f"  cp config.example.json config.json\n\n"
            f"Lalu isi nilai yang diperlukan, terutama:\n"
            f"  - api.telegram_bot_token\n"
            f"  - api.telegram_chat_id\n"
            f"  - auto_trade"
        )

    # ── 2. Parse JSON ─────────────────────────────────────────────────────────
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        _abort(
            f"File '{CONFIG_PATH}' tidak valid (JSON rusak).\n\n"
            f"Detail error:\n  {e}\n\n"
            f"Periksa sintaks JSON menggunakan:\n"
            f"  https://jsonlint.com"
        )
    except Exception as e:
        _abort(f"Gagal membaca '{CONFIG_PATH}':\n  {e}")

    # ── 3. Ambil token Telegram lebih awal (untuk kirim error ke TG jika perlu) ─
    token   = config.get("api", {}).get("telegram_bot_token", "")
    chat_id = config.get("api", {}).get("telegram_chat_id",   "")

    # ── 4. Validasi key wajib ─────────────────────────────────────────────────
    missing = []
    for label, getter in REQUIRED_KEYS:
        val = getter(config)
        if val is None or val == "" or val in ("YOUR_TELEGRAM_BOT_TOKEN",
                                                "YOUR_TELEGRAM_CHAT_ID",
                                                "YOUR_BYBIT_API_KEY",
                                                "YOUR_BYBIT_API_SECRET"):
            missing.append(f"  - {label}")

    if missing:
        _abort(
            f"Key wajib berikut belum diisi di '{CONFIG_PATH}':\n\n"
            + "\n".join(missing)
            + "\n\nPastikan nilai tidak kosong dan bukan placeholder.",
            token=token,
            chat_id=chat_id,
        )

    # ── 5. Validasi tipe & range section risk ─────────────────────────────────
    risk = config.get("risk", {})
    risk_errors = []

    risk_percent = risk.get("risk_percent")
    if not isinstance(risk_percent, (int, float)) or not (0 < risk_percent <= 1):
        risk_errors.append("  - risk.risk_percent: harus angka antara 0 (eksklusif) dan 1 (inklusif), misal 0.01 untuk 1%")

    max_positions = risk.get("max_positions")
    if not isinstance(max_positions, int) or max_positions < 1:
        risk_errors.append("  - risk.max_positions: harus integer ≥ 1")

    target_leverage = risk.get("target_leverage")
    if not isinstance(target_leverage, int) or target_leverage < 1:
        risk_errors.append("  - risk.target_leverage: harus integer ≥ 1")

    tp_split = risk.get("tp_split", [])
    if (not isinstance(tp_split, list) or len(tp_split) != 3
            or not all(isinstance(x, (int, float)) for x in tp_split)
            or abs(sum(tp_split) - 1.0) > 1e-6):
        risk_errors.append("  - risk.tp_split: harus list 3 angka yang totalnya 1.0, misal [0.4, 0.3, 0.3]")

    if risk_errors:
        _abort(
            f"Nilai tidak valid di '{CONFIG_PATH}':\n\n"
            + "\n".join(risk_errors),
            token=token,
            chat_id=chat_id,
        )

    return config


CONFIG = load_config()
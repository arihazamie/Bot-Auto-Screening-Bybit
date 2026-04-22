"""
config_loader.py — Load & validasi config.json  (v2 — Secure Secrets via .env)

════════════════════════════════════════════════════════════════════
  CARA PENGELOLAAN SECRETS (WAJIB DIBACA)
════════════════════════════════════════════════════════════════════
  Secrets (API key, token) TIDAK boleh disimpan di config.json karena
  file itu bisa ter-commit ke Git secara tidak sengaja.

  Gunakan file .env (sudah di .gitignore) untuk semua secrets:

    BYBIT_KEY=xxxxxxxxxxxxxxxx
    BYBIT_SECRET=xxxxxxxxxxxxxxxx
    TELEGRAM_BOT_TOKEN=123456:ABCdef...
    TELEGRAM_CHAT_ID=-100123456789

  config.json hanya untuk parameter non-sensitif (strategy, risk, dll).
  Kolom "api" di config.json bisa dikosongkan atau dihapus sepenuhnya.

  Priority: env var  >  config.json  >  string kosong
════════════════════════════════════════════════════════════════════

Behaviour:
  - config.json tidak ada      → pesan jelas + exit(1)
  - config.json tidak valid    → pesan jelas + exit(1)
  - Secrets wajib tidak ada    → pesan jelas + exit(1)
  - Semua OK                   → CONFIG siap dipakai

Secrets wajib (dari .env atau config.json):
  - TELEGRAM_BOT_TOKEN  / api.telegram_bot_token
  - TELEGRAM_CHAT_ID    / api.telegram_chat_id

Secrets opsional (hanya diperlukan saat auto_trade=true):
  - BYBIT_KEY           / api.bybit_key
  - BYBIT_SECRET        / api.bybit_secret
"""

import json
import os
import sys
import requests
from dotenv import load_dotenv

# Muat .env dari direktori project (bukan CWD) agar bekerja dari mana pun
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env"))


# ─── Mapping: env var name → path di config["api"] ───────────────────────────
#
# Saat runtime, nilai dari env var SELALU menimpa nilai di config.json.
# Ini memungkinkan deployment via environment variables (Docker, systemd, VPS)
# tanpa perlu menyentuh file apapun.
#
_SECRET_ENV_MAP: list[tuple[str, str, str]] = [
    # (env_var_name,       config_api_key,       label untuk error message)
    ("BYBIT_KEY",          "bybit_key",           "Bybit API Key"),
    ("BYBIT_SECRET",       "bybit_secret",        "Bybit API Secret"),
    ("TELEGRAM_BOT_TOKEN", "telegram_bot_token",  "Telegram Bot Token"),
    ("TELEGRAM_CHAT_ID",   "telegram_chat_id",    "Telegram Chat ID"),
]

# ─── Key wajib & sumber nilainya (setelah env override) ──────────────────────
REQUIRED_KEYS = [
    ("TELEGRAM_BOT_TOKEN / api.telegram_bot_token",
     lambda c: c.get("api", {}).get("telegram_bot_token", "")),
    ("TELEGRAM_CHAT_ID / api.telegram_chat_id",
     lambda c: c.get("api", {}).get("telegram_chat_id", "")),
    ("auto_trade",
     lambda c: c.get("auto_trade", None)),
]

CONFIG_PATH = "config.json"

# Placeholder values yang dianggap "belum diisi"
_PLACEHOLDERS = {
    "YOUR_TELEGRAM_BOT_TOKEN",
    "YOUR_TELEGRAM_CHAT_ID",
    "YOUR_BYBIT_API_KEY",
    "YOUR_BYBIT_API_SECRET",
    "CHANGE_ME",
    "your_token_here",
}


# ─── Helpers ─────────────────────────────────────────────────────────────────

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
        f"Periksa file <code>.env</code> atau <code>config.json</code> dan restart bot."
    )
    _send_telegram_error(token, chat_id, tg_msg)
    sys.exit(1)


def _apply_env_secrets(config: dict) -> tuple[dict, list[str]]:
    """
    Override nilai api.* di config dengan environment variables.

    Mengembalikan (config_updated, applied_list) di mana applied_list
    adalah daftar nama env var yang berhasil diterapkan (untuk logging).

    Tidak pernah raise — jika env var tidak ada, nilai config.json dipertahankan.
    """
    config.setdefault("api", {})
    applied: list[str] = []

    for env_name, cfg_key, label in _SECRET_ENV_MAP:
        env_val = os.getenv(env_name, "").strip()
        if env_val and env_val not in _PLACEHOLDERS:
            config["api"][cfg_key] = env_val
            applied.append(f"    ✅  {label:<22} ← {env_name} (env)")
        elif config["api"].get(cfg_key, "").strip() not in ("", *_PLACEHOLDERS):
            applied.append(f"    ⚠️  {label:<22} ← config.json (kurang aman, pindahkan ke .env)")
        # else: tidak ada di mana pun — akan tertangkap di validasi berikutnya

    return config, applied


# ─── Main loader ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    # ── 1. Cek keberadaan file ────────────────────────────────────────────────
    if not os.path.exists(CONFIG_PATH):
        _abort(
            f"File '{CONFIG_PATH}' tidak ditemukan.\n\n"
            f"Jalankan perintah berikut untuk membuat konfigurasi:\n"
            f"  cp config.example.json config.json\n\n"
            f"Lalu buat file .env untuk secrets:\n"
            f"  cp .env.example .env\n\n"
            f"Isi nilai wajib di .env:\n"
            f"  TELEGRAM_BOT_TOKEN=...\n"
            f"  TELEGRAM_CHAT_ID=...\n"
            f"  BYBIT_KEY=...          (hanya jika auto_trade=true)\n"
            f"  BYBIT_SECRET=...       (hanya jika auto_trade=true)"
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

    # ── 3. Override secrets dari environment variables ────────────────────────
    #
    # Ini adalah langkah kunci keamanan: env var SELALU menang atas config.json.
    # Tampilkan ringkasan sumber setiap secret agar mudah di-diagnosa.
    #
    config, applied = _apply_env_secrets(config)

    print("\n  🔐 Secrets loading:")
    for line in applied:
        print(line)
    if not applied:
        print("    (tidak ada secret yang ditemukan — validasi di bawah akan gagal)")
    print()

    # ── 4. Ambil token Telegram untuk kirim error ke TG jika perlu ───────────
    token   = config.get("api", {}).get("telegram_bot_token", "")
    chat_id = config.get("api", {}).get("telegram_chat_id",   "")

    # ── 5. Validasi key wajib ─────────────────────────────────────────────────
    missing = []
    for label, getter in REQUIRED_KEYS:
        val = getter(config)
        if val is None or str(val).strip() == "" or str(val) in _PLACEHOLDERS:
            missing.append(f"  - {label}")

    if missing:
        _abort(
            f"Secrets wajib berikut tidak ditemukan di .env maupun config.json:\n\n"
            + "\n".join(missing)
            + "\n\n"
            + "Cara mengisi:\n"
            + "  1. Salin template:  cp .env.example .env\n"
            + "  2. Edit .env dan isi nilai yang diperlukan\n"
            + "  3. Restart bot\n\n"
            + "Lihat .env.example untuk format yang benar.",
            token=token,
            chat_id=chat_id,
        )

    # ── 6. Validasi Bybit key jika auto_trade=true ────────────────────────────
    if config.get("auto_trade", False):
        bybit_errors = []
        if not config["api"].get("bybit_key", "").strip():
            bybit_errors.append("  - BYBIT_KEY (env) atau api.bybit_key (config.json)")
        if not config["api"].get("bybit_secret", "").strip():
            bybit_errors.append("  - BYBIT_SECRET (env) atau api.bybit_secret (config.json)")
        if bybit_errors:
            _abort(
                "auto_trade=true tapi Bybit API credentials tidak ditemukan:\n\n"
                + "\n".join(bybit_errors)
                + "\n\nTambahkan ke .env:\n"
                + "  BYBIT_KEY=your_api_key_here\n"
                + "  BYBIT_SECRET=your_api_secret_here",
                token=token,
                chat_id=chat_id,
            )

    # ── 7. Validasi tipe & range section risk ─────────────────────────────────
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
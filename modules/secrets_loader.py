"""
secrets_loader.py — Resolve credentials from env-var first, config.json fallback.

Tujuan:
  • Bybit API key + secret dan OpenRouter API key tidak boleh ada di repo.
  • Dipakai khusus oleh `manual_assistant.py` (asisten read-only baru) supaya
    bot eksisting tidak terganggu.

Prioritas:
  1. ENV var (BYBIT_API_KEY, BYBIT_API_SECRET, OPENROUTER_API_KEY)
  2. config.json -> api.bybit_key / api.bybit_secret / api.openrouter_api_key
  3. None (string kosong)

Tidak pernah print/log nilai secret. Hanya log "loaded from env" / "loaded from config".
"""

from __future__ import annotations

import logging
import os
from typing import NamedTuple

logger = logging.getLogger("Secrets")


class BybitKeys(NamedTuple):
    api_key: str
    api_secret: str
    source: str  # "env" | "config" | "missing"


def _read_env(name: str) -> str:
    val = os.environ.get(name, "")
    return val.strip() if val else ""


def get_bybit_keys() -> BybitKeys:
    """
    Resolve Bybit credentials. Mengembalikan source supaya bot bisa log
    "loaded from env" tanpa membocorkan nilai.
    """
    env_key    = _read_env("BYBIT_API_KEY")
    env_secret = _read_env("BYBIT_API_SECRET")

    if env_key and env_secret:
        logger.info("Bybit keys loaded from env (BYBIT_API_KEY/_SECRET)")
        return BybitKeys(env_key, env_secret, "env")

    # Fallback: config.json — agar setup lama tetap jalan tanpa migration.
    try:
        from modules.config_loader import CONFIG  # lazy: hindari import-time crash
        api = CONFIG.get("api", {}) or {}
        cfg_key    = (api.get("bybit_key", "") or "").strip()
        cfg_secret = (api.get("bybit_secret", "") or "").strip()
        # Tolak placeholder default biar tidak diam-diam pakai string "YOUR_..."
        if (
            cfg_key and cfg_secret
            and not cfg_key.startswith("YOUR_")
            and not cfg_secret.startswith("YOUR_")
        ):
            logger.info("Bybit keys loaded from config.json (api.bybit_*)")
            return BybitKeys(cfg_key, cfg_secret, "config")
    except Exception as e:
        logger.debug(f"config.json bybit key load skipped: {e}")

    logger.warning(
        "Bybit API key/secret tidak ditemukan. Set BYBIT_API_KEY + "
        "BYBIT_API_SECRET sebagai env var, atau isi api.bybit_key/_secret "
        "di config.json. Bot akan jalan tanpa kemampuan baca posisi."
    )
    return BybitKeys("", "", "missing")


def get_openrouter_key() -> str:
    """Resolve OpenRouter API key. Empty string artinya advisor di-disable."""
    env_val = _read_env("OPENROUTER_API_KEY")
    if env_val:
        logger.info("OpenRouter key loaded from env (OPENROUTER_API_KEY)")
        return env_val

    try:
        from modules.config_loader import CONFIG
        api = CONFIG.get("api", {}) or {}
        cfg_val = (api.get("openrouter_api_key", "") or "").strip()
        if cfg_val and not cfg_val.startswith("YOUR_"):
            logger.info("OpenRouter key loaded from config.json (api.openrouter_api_key)")
            return cfg_val
    except Exception as e:
        logger.debug(f"config.json openrouter key load skipped: {e}")

    logger.warning(
        "OPENROUTER_API_KEY tidak ditemukan. Advisor akan di-bypass — "
        "notifikasi tetap dikirim tapi tanpa analisa AI."
    )
    return ""

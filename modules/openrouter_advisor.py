"""
openrouter_advisor.py — Tanya OpenRouter (LLM) untuk analisa posisi manual.

Kontrak ketat:
  • INPUT  : satu dict berisi posisi + konteks market (build_context output).
  • OUTPUT : satu dict ter-validasi schema (lihat _validate_advice).
  • LLM CUMA SARAN. Bot ini READ-ONLY — hasil tidak pernah di-eksekusi otomatis.

Kalau OpenRouter timeout, key kosong, atau JSON invalid → fallback ke advice
heuristik dari konteks (regime + RSI + slope), supaya pipeline notifier tetap
mengirim sesuatu yang berguna.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger("Advisor")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Skema yang diharapkan dari LLM. Kunci tidak case-sensitive saat parse.
ALLOWED_BIAS = (
    "bullish_strong", "bullish", "neutral", "bearish", "bearish_strong",
)
ALLOWED_TP_ACTIONS = ("take_partial_now", "hold", "scale_out_soon")
ALLOWED_SL_ACTIONS = ("move_to_be", "move_to_price", "tighten", "hold")

SYSTEM_PROMPT = (
    "You are an experienced crypto futures trader-coach. The user gives you "
    "ONE open Bybit perpetual position plus a snapshot of current market "
    "context (regime, RSI, EMA alignment, funding, CVD, slope). Your job is "
    "to analyse and recommend, in JSON only, whether the user should: "
    "(1) take partial profit now or hold, (2) move stop loss to break-even / "
    "tighten / hold. You DO NOT have execution access — your output is "
    "advisory and will be sent to the user as a Telegram message. Be honest. "
    "If signals are mixed, say neutral and recommend hold. Be conservative "
    "with break-even moves: only recommend it when price has moved in the "
    "user's favour AND structure supports it. Keep `overall` under 200 chars. "
    "Output strict JSON with this exact shape:\n"
    "{\n"
    '  "bias": "bullish_strong|bullish|neutral|bearish|bearish_strong",\n'
    '  "tp_recommendation": {\n'
    '     "action": "take_partial_now|hold|scale_out_soon",\n'
    '     "suggested_close_pct": <0-100 int>,\n'
    '     "suggested_price": <number, 0 if N/A>,\n'
    '     "reason": "<=120 chars"\n'
    "  },\n"
    '  "sl_recommendation": {\n'
    '     "action": "move_to_be|move_to_price|tighten|hold",\n'
    '     "suggested_sl": <number, 0 if N/A>,\n'
    '     "reason": "<=120 chars"\n'
    "  },\n"
    '  "overall": "<=200 chars summary in plain English"\n'
    "}\n"
    "No markdown, no commentary outside the JSON object."
)


# ────────────────────────────────────────────────────────────────────────────
# Public class
# ────────────────────────────────────────────────────────────────────────────

class OpenRouterAdvisor:
    """
    Klien tipis ke OpenRouter dengan timeout, retry, dan cache pendek
    (default 60 detik) supaya tidak boros credit kalau loop polling cepat.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "openrouter/auto",
        timeout_sec: float = 20.0,
        max_retries: int = 2,
        cache_seconds: int = 60,
        site_url: str | None = None,
        app_name: str = "bybit-manual-assistant",
    ):
        self._api_key      = api_key or ""
        self._model        = model
        self._timeout      = timeout_sec
        self._max_retries  = max_retries
        self._cache_secs   = cache_seconds
        self._site_url     = site_url
        self._app_name     = app_name
        # cache: { cache_key: (advice_dict, timestamp) }
        self._cache: dict[str, tuple[dict, float]] = {}

    # ────────────────────────────────────────────────────────────
    # Top-level
    # ────────────────────────────────────────────────────────────
    def analyse(self, position: dict, market_ctx: dict) -> dict:
        """
        Return advice dict. NEVER raises — kembalikan fallback kalau gagal.
        """
        cache_key = self._cache_key(position, market_ctx)
        cached = self._cache_get(cache_key)
        if cached is not None:
            cached["_cached"] = True
            return cached

        if not self._api_key:
            logger.debug("OpenRouter key empty — using heuristic fallback")
            advice = _heuristic_fallback(position, market_ctx, reason="no_api_key")
            self._cache_put(cache_key, advice)
            return advice

        try:
            advice = self._call_remote(position, market_ctx)
        except Exception as e:
            logger.warning(f"OpenRouter call failed ({type(e).__name__}: {e}) — heuristic fallback")
            advice = _heuristic_fallback(position, market_ctx, reason=f"api_error:{type(e).__name__}")

        self._cache_put(cache_key, advice)
        return advice

    # ────────────────────────────────────────────────────────────
    # Cache
    # ────────────────────────────────────────────────────────────
    def _cache_key(self, position: dict, market_ctx: dict) -> str:
        # Kombinasi yang stabil per posisi + bucket waktu N detik.
        bucket = int(time.time() // max(1, self._cache_secs))
        return (
            f"{position.get('symbol','')}|{position.get('side','')}|"
            f"{round(float(position.get('mark_price', 0) or 0), 4)}|"
            f"{market_ctx.get('regime_15m', {}).get('label', '')}|"
            f"{bucket}"
        )

    def _cache_get(self, key: str) -> dict | None:
        item = self._cache.get(key)
        if not item:
            return None
        advice, ts = item
        if time.time() - ts > self._cache_secs:
            self._cache.pop(key, None)
            return None
        return dict(advice)

    def _cache_put(self, key: str, advice: dict) -> None:
        self._cache[key] = (advice, time.time())
        # Trim cache supaya tidak grow unbounded di proses long-running
        if len(self._cache) > 256:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1][1])[:32]
            for k, _ in oldest:
                self._cache.pop(k, None)

    # ────────────────────────────────────────────────────────────
    # HTTP call
    # ────────────────────────────────────────────────────────────
    def _call_remote(self, position: dict, market_ctx: dict) -> dict:
        import requests  # already a transitive dep via requirements.txt

        user_payload = {
            "position":    position,
            "market_ctx":  market_ctx,
        }
        body = {
            "model":      self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": json.dumps(user_payload, default=str)},
            ],
            "temperature":      0.3,
            "max_tokens":       400,
            "response_format":  {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type":  "application/json",
            "X-Title":       self._app_name,
        }
        if self._site_url:
            headers["HTTP-Referer"] = self._site_url

        last_err: Exception | None = None
        for attempt in range(1, self._max_retries + 2):
            try:
                resp = requests.post(
                    OPENROUTER_URL,
                    json=body,
                    headers=headers,
                    timeout=self._timeout,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"transient HTTP {resp.status_code}")
                resp.raise_for_status()
                data = resp.json()
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                advice_raw = _extract_json(content)
                advice = _validate_advice(advice_raw)
                advice["_cached"] = False
                advice["_source"] = "openrouter"
                advice["_model"]  = self._model
                return advice

            except Exception as e:
                last_err = e
                if attempt > self._max_retries:
                    break
                wait = 1.5 ** attempt
                logger.debug(f"OpenRouter retry {attempt}: {type(e).__name__}: {e} (sleep {wait:.1f}s)")
                time.sleep(wait)

        # All retries exhausted
        raise last_err if last_err else RuntimeError("openrouter unknown error")


# ────────────────────────────────────────────────────────────────────────────
# Validation + fallback
# ────────────────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """LLM kadang wrap JSON di code-fence. Strip + parse."""
    if not text:
        raise ValueError("empty content")
    s = text.strip()
    # Strip markdown code fence if present
    if s.startswith("```"):
        s = s.strip("`")
        # Drop possible language tag
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    return json.loads(s)


def _validate_advice(raw: Any) -> dict:
    """
    Pastikan struktur sesuai. Kalau ada field hilang → isi default aman.
    Tidak pernah raise — bot tetap kirim notif walau LLM ngelantur.
    """
    if not isinstance(raw, dict):
        raw = {}

    bias = str(raw.get("bias", "neutral")).lower()
    if bias not in ALLOWED_BIAS:
        bias = "neutral"

    tp_in = raw.get("tp_recommendation", {}) or {}
    tp_action = str(tp_in.get("action", "hold")).lower()
    if tp_action not in ALLOWED_TP_ACTIONS:
        tp_action = "hold"
    tp_close_pct = _clamp_int(tp_in.get("suggested_close_pct", 0), 0, 100)
    tp_price     = _safe_float(tp_in.get("suggested_price", 0))
    tp_reason    = _clip_str(tp_in.get("reason", ""), 200)

    sl_in = raw.get("sl_recommendation", {}) or {}
    sl_action = str(sl_in.get("action", "hold")).lower()
    if sl_action not in ALLOWED_SL_ACTIONS:
        sl_action = "hold"
    sl_price  = _safe_float(sl_in.get("suggested_sl", 0))
    sl_reason = _clip_str(sl_in.get("reason", ""), 200)

    overall = _clip_str(raw.get("overall", ""), 280) or "no summary."

    return {
        "bias": bias,
        "tp_recommendation": {
            "action": tp_action,
            "suggested_close_pct": tp_close_pct,
            "suggested_price": tp_price,
            "reason": tp_reason,
        },
        "sl_recommendation": {
            "action": sl_action,
            "suggested_sl": sl_price,
            "reason": sl_reason,
        },
        "overall": overall,
        "_source": "validated",
    }


def _heuristic_fallback(position: dict, market_ctx: dict, reason: str) -> dict:
    """
    Saat LLM tidak tersedia, berikan saran sederhana berdasarkan regime + PnL.
    Selalu konservatif: tidak pernah merekomendasikan move SL ke BE kecuali
    posisi sudah profit.
    """
    side    = position.get("side", "Long")
    pnl_pct = float(position.get("unrealised_pnl_pct", 0.0))
    reg15   = (market_ctx.get("regime_15m") or {}).get("label", "UNKNOWN")
    reg1h   = (market_ctx.get("regime_1h")  or {}).get("label", "UNKNOWN")
    rsi15   = float(market_ctx.get("rsi_15m", 50.0))

    # Bias bersifat MARKET-DIRECTIONAL (bukan relatif posisi), supaya konsisten
    # dengan output schema LLM. TP/SL heuristic di bawah memperhitungkan side.
    bias = "neutral"
    if reg1h == "TREND_BULL" and reg15 in ("TREND_BULL", "SQUEEZE"):
        bias = "bullish"
    elif reg1h == "TREND_BEAR" and reg15 in ("TREND_BEAR", "SQUEEZE"):
        bias = "bearish"

    # Side-aware: regime "melawan" posisi → biasa-bias TP lebih agresif.
    bias_against_position = (
        (side == "Long"  and bias == "bearish") or
        (side == "Short" and bias == "bullish")
    )

    # TP heuristic — urutan dibalik supaya threshold PnL tinggi tidak ter-shadow
    # oleh threshold yang lebih rendah. Bias di sini market-directional, jadi
    # pakai `bias_against_position` supaya logika sama untuk Long & Short.
    tp_action = "hold"
    tp_close_pct = 0
    if pnl_pct >= 2.0:
        tp_action = "scale_out_soon"
        tp_close_pct = 30
    elif pnl_pct >= 1.0 and (bias == "neutral" or bias_against_position):
        tp_action = "take_partial_now"
        tp_close_pct = 25

    # SL heuristic: hanya geser ke BE jika sudah +0.8% (Long) atau -0.8% (Short)
    sl_action = "hold"
    sl_price  = 0.0
    if pnl_pct >= 0.8:
        sl_action = "move_to_be"
        sl_price  = float(position.get("entry_price", 0.0))

    return {
        "bias": bias,
        "tp_recommendation": {
            "action": tp_action,
            "suggested_close_pct": tp_close_pct,
            "suggested_price": 0.0,
            "reason": f"heuristic ({reason}); pnl={pnl_pct:+.2f}% reg={reg1h}/{reg15} rsi15={rsi15:.0f}",
        },
        "sl_recommendation": {
            "action": sl_action,
            "suggested_sl": sl_price,
            "reason": f"heuristic ({reason}); pnl={pnl_pct:+.2f}%",
        },
        "overall": f"Heuristic advice ({reason}). Bias {bias} based on regime {reg1h}/{reg15}.",
        "_source": "heuristic",
        "_cached": False,
    }


def _safe_float(v) -> float:
    try:
        f = float(v)
        return f if f == f else 0.0  # NaN guard
    except (TypeError, ValueError):
        return 0.0


def _clamp_int(v, lo: int, hi: int) -> int:
    try:
        i = int(round(float(v)))
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, i))


def _clip_str(v, max_len: int) -> str:
    s = str(v) if v is not None else ""
    return s[: max_len].strip()

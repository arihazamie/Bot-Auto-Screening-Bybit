"""
pattern_registry.py — central aggregator over every pattern detector.
=====================================================================

Single entry point that runs every detector implemented across the codebase
and returns a unified list of pattern hits, augmented with each pattern's
**baseline winrate** (literature-derived static prior).

Sources aggregated:
  * `modules.patterns.find_pattern` — chart patterns (head & shoulders, flags,
    triangles, double tops/bottoms, rectangles)
  * `modules.candlestick_patterns.detect_all` — 13 single-/multi-candle reversal
    & continuation patterns
  * `modules.harmonic_patterns.detect_all` — 5 XABCD harmonic patterns
  * `modules.smc.analyze_smc` — order blocks, FVG, liquidity sweeps,
    market structure
  * `modules.ict_extras.detect_all` — Breaker Block + Mitigation Block
  * `modules.wyckoff_patterns.detect_all` — Spring + Upthrust
  * `modules.volume_profile.detect_all` — POC / VAH / VAL reactions
  * `modules.divergence.detect_all` — RSI + MACD divergence (incl. multi-TF
    when the caller provides `multi_tf_dfs`)
  * `modules.elliott_wave.detect_all` — Elliott Wave ABC corrective patterns
    (zigzag / flat / irregular completions in the trend direction)

Returned dict shape:
    {
      "name":     str,
      "side":     "Long" | "Short",
      "details":  str,
      "baseline": float,   # baseline winrate (0.50–0.75)
      "source":   str,     # which module produced this hit
    }

Baseline winrates are static priors compiled from:
  * Bulkowski's *Encyclopedia of Chart Patterns* (chart + candlestick)
  * Carney's *Harmonic Trading* (harmonic patterns)
  * Pruden's *Wyckoff Method* (spring/upthrust)
  * ICT teaching material + community backtests (SMC / breaker / mitigation)
  * Volume Profile literature (Steidlmayer & Dalton)
  * Divergence community backtests

Phase 7 (next) overlays *actual* winrates from the rolling 30-day pattern
stats DB on top of these baselines.
"""
from __future__ import annotations

import logging
from typing import Mapping

import pandas as pd

from modules import (
    candlestick_patterns,
    divergence,
    elliott_wave,
    harmonic_patterns,
    ict_extras,
    patterns as chart_patterns,
    smc,
    volume_profile,
    wyckoff_patterns,
)

logger = logging.getLogger("PatternRegistry")


# ─── Baseline winrates (literature priors) ───────────────────────────────────

BASELINE_WINRATES: dict[str, float] = {
    # ── Chart patterns (Bulkowski's Encyclopedia of Chart Patterns) ──
    "head_and_shoulders":         0.66,
    "inverse_head_and_shoulders": 0.65,
    "bull_flag":                  0.67,
    "bear_flag":                  0.67,
    "ascending_triangle":         0.70,
    "descending_triangle":        0.69,
    "double_top":                 0.65,
    "double_bottom":              0.66,
    "bullish_rectangle":          0.55,
    "bearish_rectangle":          0.55,

    # ── Candlestick patterns (Bulkowski's candlestick studies) ──
    "bullish_engulfing":          0.63,
    "bearish_engulfing":          0.62,
    "hammer":                     0.60,
    "inverted_hammer":            0.60,
    "shooting_star":              0.59,
    "hanging_man":                0.59,
    "doji":                       0.50,
    "morning_star":               0.65,
    "evening_star":               0.64,
    "three_white_soldiers":       0.65,
    "three_black_crows":          0.64,
    "tweezer_top":                0.55,
    "tweezer_bottom":             0.56,

    # ── Harmonic patterns (Carney's Harmonic Trading Vol 1-2) ──
    "gartley":                    0.70,
    "bat":                        0.71,
    "butterfly":                  0.70,
    "crab":                       0.70,
    "shark":                      0.70,

    # ── ICT / SMC ──
    "smc_long":                   0.62,   # composite from analyze_smc
    "smc_short":                  0.62,
    "breaker_block_bullish":      0.65,
    "breaker_block_bearish":      0.65,
    "mitigation_block_bullish":   0.62,
    "mitigation_block_bearish":   0.62,

    # ── Wyckoff (Pruden) ──
    "wyckoff_spring":             0.68,
    "wyckoff_upthrust":           0.67,

    # ── Volume Profile (Steidlmayer / Dalton) ──
    "vp_poc_reaction":            0.58,
    "vp_vah_rejection":           0.60,
    "vp_val_reaction":            0.60,

    # ── Divergence (community backtests) ──
    "divergence_rsi":             0.55,
    "divergence_macd":            0.55,
    "divergence_rsi_mtf":         0.65,   # multi-TF confluence boost
    "divergence_macd_mtf":        0.65,

    # ── Elliott Wave (Frost & Prechter, *Elliott Wave Principle*) ──
    # Empirical edge of completed ABC corrections in the trend direction.
    # Conservative starting estimate; the rolling-30d actual winrate will
    # override this once ≥10 closed paper trades carry the pattern.
    "elliott_abc_long":           0.62,
    "elliott_abc_short":          0.62,
}

DEFAULT_BASELINE = 0.50


def baseline_for(name: str) -> float:
    return BASELINE_WINRATES.get(name, DEFAULT_BASELINE)


# ─── Source dispatch ─────────────────────────────────────────────────────────

def _from_chart_patterns(df: pd.DataFrame) -> list[dict]:
    """`patterns.find_pattern` returns a single string name (or None). Wrap it
    into the registry's dict shape."""
    try:
        name = chart_patterns.find_pattern(df)
    except Exception as e:
        logger.debug(f"chart_patterns.find_pattern error: {e}")
        return []
    if not name:
        return []
    side = chart_patterns.pattern_direction(name)
    if not side:
        return []
    return [{
        "name": name,
        "side": side,
        "details": f"chart pattern detected: {name}",
        "source": "chart_patterns",
    }]


def _from_smc(df: pd.DataFrame) -> list[dict]:
    """`smc.analyze_smc(df, side)` is direction-conditional. Try both sides
    and emit a hit when the SMC stack agrees with that direction.

    `analyze_smc` typically returns a (score, reasons, ...) tuple; we treat
    a positive score for the queried side as a hit. Be defensive about return
    shape — older versions returned (int, str, ...), newer ones may return
    a dict. We normalise to "did this side fire?" and record the reason.
    """
    hits: list[dict] = []
    for side in ("Long", "Short"):
        try:
            res = smc.analyze_smc(df, side)
        except Exception as e:
            logger.debug(f"smc.analyze_smc({side}) error: {e}")
            continue
        score, reason = _normalise_smc_result(res)
        if score is None or score <= 0:
            continue
        hits.append({
            "name": f"smc_{side.lower()}",
            "side": side,
            "details": f"SMC stack agrees ({reason})" if reason else "SMC stack agrees",
            "source": "smc",
        })
    return hits


def _normalise_smc_result(res) -> tuple[int | None, str | None]:
    """Best-effort normalisation of `analyze_smc` return shape.

    Current `modules.smc.analyze_smc` returns ``(valid: bool, score: int,
    reasons: list[str])``. Older / alternate shapes are also tolerated
    (dict, ``(score, reason)`` tuple, scalar) so that the registry stays
    robust if the SMC API evolves.
    """
    if res is None:
        return None, None
    if isinstance(res, dict):
        return res.get("score"), str(res.get("reason") or res.get("reasons") or "")
    if isinstance(res, tuple):
        # Canonical 3-tuple: (valid, score, reasons)
        if len(res) >= 3 and isinstance(res[0], bool):
            if not res[0]:                     # hard reject
                return 0, None
            score = res[1] if isinstance(res[1], (int, float)) and not isinstance(res[1], bool) else None
            if isinstance(res[2], list):
                reason = ", ".join(str(r) for r in res[2]) if res[2] else None
            elif isinstance(res[2], str):
                reason = res[2] or None
            else:
                reason = None
            return score, reason
        # Legacy 2-tuple: (score, reason). Reject bool-as-score.
        if len(res) >= 2:
            score = res[0] if isinstance(res[0], (int, float)) and not isinstance(res[0], bool) else None
            reason = res[1] if isinstance(res[1], str) else None
            return score, reason
        if len(res) == 1:
            v = res[0]
            return (int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None), None
    if isinstance(res, (int, float)) and not isinstance(res, bool):
        return int(res), None
    return None, None


def _annotate_baseline(hits: list[dict], source: str) -> list[dict]:
    """Attach baseline winrate + source label to every hit dict."""
    out: list[dict] = []
    for h in hits:
        h2 = dict(h)
        h2.setdefault("source", source)
        h2["baseline"] = baseline_for(h["name"])
        out.append(h2)
    return out


# ─── Public aggregator ───────────────────────────────────────────────────────

def detect_all_patterns(
    df: pd.DataFrame,
    multi_tf_dfs: Mapping[str, pd.DataFrame] | None = None,
) -> list[dict]:
    """Run every detector and return a flat, baseline-annotated list of hits.

    Parameters
    ----------
    df : pd.DataFrame
        Primary OHLC(V) data (typically 15m or 1h closed candles).
    multi_tf_dfs : optional mapping
        e.g. ``{"15m": df15, "1h": df1, "4h": df4}``. When provided, multi-TF
        divergence detection is run alongside the single-TF divergence on `df`.

    Returns
    -------
    list[dict]
        Each item: ``{name, side, details, baseline, source}``.
    """
    if df is None or len(df) == 0:
        return []
    hits: list[dict] = []

    # 1. Chart patterns
    hits.extend(_annotate_baseline(_from_chart_patterns(df), "chart_patterns"))

    # 2. Candlestick patterns
    try:
        cs_hits = candlestick_patterns.detect_all(df)
    except Exception as e:
        logger.debug(f"candlestick_patterns error: {e}")
        cs_hits = []
    hits.extend(_annotate_baseline(cs_hits, "candlestick_patterns"))

    # 3. Harmonic patterns
    try:
        hm_hits = harmonic_patterns.detect_all(df)
    except Exception as e:
        logger.debug(f"harmonic_patterns error: {e}")
        hm_hits = []
    hits.extend(_annotate_baseline(hm_hits, "harmonic_patterns"))

    # 4. SMC stack
    hits.extend(_annotate_baseline(_from_smc(df), "smc"))

    # 5. ICT extras (Breaker / Mitigation)
    try:
        ict_hits = ict_extras.detect_all(df)
    except Exception as e:
        logger.debug(f"ict_extras error: {e}")
        ict_hits = []
    hits.extend(_annotate_baseline(ict_hits, "ict_extras"))

    # 6. Wyckoff
    try:
        wy_hits = wyckoff_patterns.detect_all(df)
    except Exception as e:
        logger.debug(f"wyckoff_patterns error: {e}")
        wy_hits = []
    hits.extend(_annotate_baseline(wy_hits, "wyckoff_patterns"))

    # 7. Volume Profile
    try:
        vp_hits = volume_profile.detect_all(df)
    except Exception as e:
        logger.debug(f"volume_profile error: {e}")
        vp_hits = []
    hits.extend(_annotate_baseline(vp_hits, "volume_profile"))

    # 8. Divergence (single-TF + optional multi-TF confluence)
    try:
        dv_hits = divergence.detect_all(df, multi_tf_dfs=multi_tf_dfs)
    except Exception as e:
        logger.debug(f"divergence error: {e}")
        dv_hits = []
    hits.extend(_annotate_baseline(dv_hits, "divergence"))

    # 9. Elliott Wave ABC corrective patterns
    try:
        ew_hits = elliott_wave.detect_all(df)
    except Exception as e:
        logger.debug(f"elliott_wave error: {e}")
        ew_hits = []
    hits.extend(_annotate_baseline(ew_hits, "elliott_wave"))

    return hits


def list_known_patterns() -> list[str]:
    """All pattern names known to the registry (for stats DB seeding)."""
    return sorted(BASELINE_WINRATES.keys())

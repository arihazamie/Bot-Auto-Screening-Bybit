"""
signal_formatter.py — Telegram payload builder for screener signals.
=====================================================================

Renders the dict produced by :func:`modules.pattern_registry.detect_all_patterns`
into a Markdown message suitable for direct dispatch to Telegram.

Each signal carries:
  * **Baseline winrate** — the static literature prior (Bulkowski, Carney,
    Pruden, Steidlmayer/Dalton, ICT material, divergence backtests).
  * **Actual winrate** — the rolling 30-day paper-trader performance for
    that pattern (from :func:`modules.database.get_actual_winrate`).
    Returns ``None`` until at least 10 samples are accumulated, in which
    case only the baseline is shown.
  * **Confidence label** — derived from the *displayed* winrate (actual
    when available, otherwise baseline):

        HIGH ≥ 65 %, MED 50–65 %, LOW < 50 %

Display contract (per the user's spec): always show baseline, and show
actual side-by-side once 10+ samples exist —

    B: 67 % / A: 71 % / n=24

Public entry point:
    :func:`format_signal(symbol, hits, ...)` -> ``str``
        Renders one Telegram message bundling all hits for a symbol.

The formatter intentionally has no side-effects other than reading the
stats DB.
"""
from __future__ import annotations

import logging
from typing import Iterable, Mapping

from modules.database import (
    PATTERN_STATS_MIN_SAMPLES,
    PATTERN_STATS_WINDOW_DAYS,
    get_actual_winrate,
)
from modules.pattern_registry import baseline_for

logger = logging.getLogger("SignalFormatter")

CONFIDENCE_HIGH_THRESHOLD = 0.65
CONFIDENCE_MED_THRESHOLD  = 0.50


# ─── Confidence + display helpers ─────────────────────────────────────────────

def confidence_label(winrate: float | None) -> str:
    """Map a winrate (0.0–1.0) to ``HIGH`` / ``MED`` / ``LOW``. ``None``
    → ``LOW`` (we treat insufficient data conservatively)."""
    if winrate is None:
        return "LOW"
    if winrate >= CONFIDENCE_HIGH_THRESHOLD:
        return "HIGH"
    if winrate >= CONFIDENCE_MED_THRESHOLD:
        return "MED"
    return "LOW"


def winrate_line(
    pattern_name: str,
    baseline: float | None = None,
    *,
    days: int = PATTERN_STATS_WINDOW_DAYS,
) -> tuple[str, float, str]:
    """Return ``(line, displayed_winrate, confidence_label)`` for one pattern.

    ``line`` is the human-readable substring used in the Telegram payload,
    e.g. ``"B: 67% / A: 71% / n=24"`` or ``"B: 70%"`` while sample count
    is below the minimum threshold.

    ``displayed_winrate`` is *actual* once available, else *baseline*.
    The caller can use it to compute the overall confidence of a multi-
    pattern stack.
    """
    base = float(baseline if baseline is not None else baseline_for(pattern_name))
    actual, n = get_actual_winrate(pattern_name, days=days)
    if actual is not None and n >= PATTERN_STATS_MIN_SAMPLES:
        line = f"B: {base*100:.0f}% / A: {actual*100:.0f}% / n={n}"
        displayed = actual
    elif n > 0:
        line = f"B: {base*100:.0f}% (A pending — n={n}/{PATTERN_STATS_MIN_SAMPLES})"
        displayed = base
    else:
        line = f"B: {base*100:.0f}%"
        displayed = base
    return line, displayed, confidence_label(displayed)


# ─── Side / emoji helpers ─────────────────────────────────────────────────────

def _side_emoji(side: str) -> str:
    return "🟢" if side == "Long" else "🔴" if side == "Short" else "⚪"


def _confidence_emoji(label: str) -> str:
    return {"HIGH": "🟢", "MED": "🟡", "LOW": "⚪"}.get(label, "⚪")


def _aggregate_side(hits: Iterable[Mapping]) -> str | None:
    """Determine the dominant side across ``hits``. Returns ``None`` when
    the stack is split (long & short hits both present) — caller should
    surface that as a *neutral* notification rather than a directional
    signal."""
    sides = {h.get("side") for h in hits if h.get("side") in ("Long", "Short")}
    if len(sides) == 1:
        return next(iter(sides))
    return None


# ─── Public entry point ───────────────────────────────────────────────────────

def format_signal(
    symbol: str,
    hits: list[dict],
    *,
    timeframe: str | None = None,
    extra_lines: list[str] | None = None,
) -> str:
    """Render ``hits`` (output of ``detect_all_patterns``) as a Telegram
    Markdown message. Returns an empty string if ``hits`` is empty.

    ``extra_lines`` are appended verbatim at the bottom — useful for
    appending entry/SL/TP guidance in phase 8 once the registry is wired
    into ``scan()``.
    """
    if not hits:
        return ""

    side = _aggregate_side(hits)
    side_str = side or "MIXED"
    head_emoji = _side_emoji(side or "")
    tf_str = f" · {timeframe}" if timeframe else ""

    lines: list[str] = [
        f"{head_emoji} *{symbol}* — {side_str}{tf_str}",
        f"_Patterns detected: {len(hits)}_",
        "",
    ]

    # Build per-hit detail block; track displayed winrates for an
    # aggregate confidence read at the bottom.
    displayed_rates: list[float] = []
    for h in hits:
        name     = h.get("name", "?")
        h_side   = h.get("side", "?")
        details  = (h.get("details") or "").strip()
        baseline = h.get("baseline")
        line, displayed, label = winrate_line(name, baseline)
        displayed_rates.append(displayed)
        emoji_c = _confidence_emoji(label)
        lines.append(f"{emoji_c} *{name}* ({h_side}) — {line} · {label}")
        if details:
            lines.append(f"   ↳ _{details}_")

    # Aggregate confidence (avg of displayed winrates).
    if displayed_rates:
        avg = sum(displayed_rates) / len(displayed_rates)
        agg_label = confidence_label(avg)
        lines += [
            "",
            f"*Aggregate*: {avg*100:.0f}% ({agg_label}) "
            f"— {len(displayed_rates)} pattern(s)",
        ]

    if extra_lines:
        lines.append("")
        lines.extend(extra_lines)

    return "\n".join(lines)

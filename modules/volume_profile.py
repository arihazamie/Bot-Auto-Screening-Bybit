"""
volume_profile.py — Point-of-Control / Value-Area reactions
===========================================================

Builds a lightweight volume profile over the last `VP_LOOKBACK` candles by
binning typical-price (`(H+L+C)/3`) weighted by volume into `VP_BINS` price
buckets. From the resulting profile we derive:

    * **POC**  — Point of Control: bin with the highest traded volume.
    * **VAH**  — Value Area High: upper edge of the smallest contiguous price
                 region around POC that contains `VA_PCT` of total volume.
    * **VAL**  — Value Area Low: lower edge of the same region.

Detection signals on the most recent closed bar:

* `vp_poc_reaction`: latest close approached POC from above and rejected
  (Short) or from below and rejected (Long), within `TOLERANCE_BIN` bins.
* `vp_vah_rejection` (Short): latest high pierced VAH but closed back below.
* `vp_val_reaction` (Long): latest low pierced VAL but closed back above.

The detectors do NOT fire if the profile is degenerate (volume too low /
all weight in one bin / NaN / missing volume column).

Returned dict shape:
    {"name": str, "side": "Long"|"Short", "details": str}
"""
from __future__ import annotations

import logging
from typing import Callable

import numpy as np
import pandas as pd

logger = logging.getLogger("VolumeProfile")

VP_LOOKBACK   = 100
VP_BINS       = 50
VA_PCT        = 0.70
TOLERANCE_BIN = 1   # how many bin-widths of slack qualifies as "near" a level


def _build_profile(df: pd.DataFrame, lookback: int = VP_LOOKBACK, bins: int = VP_BINS) -> dict | None:
    """Return {'poc': float, 'vah': float, 'val': float, 'bin_w': float} or
    None when the input is too short / degenerate."""
    if "volume" not in df.columns:
        return None
    if len(df) < lookback + 1:
        return None
    window = df.iloc[-(lookback + 1):-1]  # exclude the latest bar
    typical = (window["high"].astype(float) + window["low"].astype(float) + window["close"].astype(float)) / 3.0
    vol = window["volume"].astype(float)
    if not np.isfinite(typical).all() or not np.isfinite(vol).all():
        return None
    if vol.sum() <= 0:
        return None
    lo, hi = float(typical.min()), float(typical.max())
    if hi - lo <= 0:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    centres = (edges[:-1] + edges[1:]) / 2.0
    bin_w = float(edges[1] - edges[0])
    weights, _ = np.histogram(typical, bins=edges, weights=vol)
    if weights.sum() <= 0:
        return None
    poc_bin = int(np.argmax(weights))
    poc_price = float(centres[poc_bin])
    # Expand around POC until we cover VA_PCT of volume.
    target = weights.sum() * VA_PCT
    cum = float(weights[poc_bin])
    lo_bin = hi_bin = poc_bin
    while cum < target and (lo_bin > 0 or hi_bin < bins - 1):
        left  = float(weights[lo_bin - 1]) if lo_bin > 0 else -1.0
        right = float(weights[hi_bin + 1]) if hi_bin < bins - 1 else -1.0
        if right > left:
            hi_bin += 1
            cum += right
        else:
            lo_bin -= 1
            cum += left
    return {
        "poc": poc_price,
        "vah": float(edges[hi_bin + 1]),
        "val": float(edges[lo_bin]),
        "bin_w": bin_w,
    }


def detect_poc_reaction(df: pd.DataFrame) -> dict | None:
    """POC rejection: latest bar reached POC from one side and closed away
    from it. Long if rejected upward (close > POC after low ≤ POC), Short
    if rejected downward (close < POC after high ≥ POC)."""
    prof = _build_profile(df)
    if prof is None:
        return None
    poc = prof["poc"]
    last = df.iloc[-1]
    h, l, c = float(last["high"]), float(last["low"]), float(last["close"])
    bw = prof["bin_w"]
    near = TOLERANCE_BIN * bw
    if l - near <= poc <= h + near:
        if c > poc + 0.5 * bw:
            return {
                "name": "vp_poc_reaction",
                "side": "Long",
                "details": f"POC {poc:.4f} tested (low {l:.4f}) and reclaimed; close {c:.4f}",
            }
        if c < poc - 0.5 * bw:
            return {
                "name": "vp_poc_reaction",
                "side": "Short",
                "details": f"POC {poc:.4f} tested (high {h:.4f}) and rejected; close {c:.4f}",
            }
    return None


def detect_vah_rejection(df: pd.DataFrame) -> dict | None:
    prof = _build_profile(df)
    if prof is None:
        return None
    vah = prof["vah"]
    last = df.iloc[-1]
    h, c = float(last["high"]), float(last["close"])
    if h <= vah:
        return None
    if c >= vah:
        return None
    return {
        "name": "vp_vah_rejection",
        "side": "Short",
        "details": f"VAH {vah:.4f} pierced (high {h:.4f}) but rejected; close {c:.4f}",
    }


def detect_val_reaction(df: pd.DataFrame) -> dict | None:
    prof = _build_profile(df)
    if prof is None:
        return None
    val = prof["val"]
    last = df.iloc[-1]
    l, c = float(last["low"]), float(last["close"])
    if l >= val:
        return None
    if c <= val:
        return None
    return {
        "name": "vp_val_reaction",
        "side": "Long",
        "details": f"VAL {val:.4f} pierced (low {l:.4f}) but reclaimed; close {c:.4f}",
    }


# ─── Registry & aggregator ───────────────────────────────────────────────────

DETECTORS: dict[str, Callable[[pd.DataFrame], dict | None]] = {
    "vp_poc_reaction":  detect_poc_reaction,
    "vp_vah_rejection": detect_vah_rejection,
    "vp_val_reaction":  detect_val_reaction,
}


def detect_all(df: pd.DataFrame) -> list[dict]:
    if df is None or len(df) < VP_LOOKBACK + 1:
        return []
    if not {"open", "high", "low", "close", "volume"}.issubset(df.columns):
        return []
    hits: list[dict] = []
    for name, fn in DETECTORS.items():
        try:
            hit = fn(df)
        except Exception as e:
            logger.debug(f"[vp:{name}] detector error: {e}")
            continue
        if hit:
            hits.append(hit)
    return hits

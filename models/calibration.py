"""
models/calibration.py — In-memory per-(market × prob_bucket) calibration.

No DB table needed — built fresh on each edge_engine run from settled shadow
picks in nba_bets. Returns a dict edge_engine uses to adjust raw model_prob
to empirically-calibrated displayed prob.
"""
from __future__ import annotations
import pandas as pd

from models.auto_log_picks import fetch_shadow_picks


_BUCKETS = [
    ("<55%",   0.00, 0.55),
    ("55-60%", 0.55, 0.60),
    ("60-70%", 0.60, 0.70),
    ("70-80%", 0.70, 0.80),
    ("80%+",   0.80, 1.01),
]


def _bucket_label(prob: float) -> str:
    for label, lo, hi in _BUCKETS:
        if lo <= prob < hi:
            return label
    return "<55%"


def load_calibration_lookup(min_n: int = 8) -> dict:
    """Compute lookup from settled shadow picks. Returns {(market, bucket): hit_rate}."""
    settled = fetch_shadow_picks(only_pending=False, settled_only=True)
    if settled.empty:
        return {}

    settled["model_prob"] = pd.to_numeric(settled["model_prob"], errors="coerce")
    settled = settled.dropna(subset=["model_prob"])
    settled = settled[settled["result"].isin(["Win", "Loss"])]
    if settled.empty:
        return {}

    settled["bucket"] = settled["model_prob"].apply(_bucket_label)
    settled["is_win"] = (settled["result"] == "Win").astype(int)

    out = {}
    for (mkt, bucket), g in settled.groupby(["market_base", "bucket"]):
        if len(g) < min_n:
            continue
        actual = float(g["is_win"].mean())
        out[(mkt, bucket)] = actual
    return out


def calibrate_prob(raw_prob: float, market: str, lookup: dict) -> float:
    """Blend raw model probability with empirical hit rate (40/60 weighting)."""
    if raw_prob is None or raw_prob != raw_prob:
        return raw_prob
    bucket = _bucket_label(raw_prob)
    actual = lookup.get((market, bucket))
    if actual is None:
        return raw_prob
    return round(0.40 * raw_prob + 0.60 * actual, 4)


if __name__ == "__main__":
    lookup = load_calibration_lookup()
    print(f"Calibration lookup ({len(lookup)} entries with n≥8):")
    for k, v in sorted(lookup.items()):
        print(f"  {k}: actual {v*100:.1f}%")

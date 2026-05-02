"""
models/calibration.py — In-memory per-(market × prob_bucket) calibration.

No DB table needed — built fresh on each edge_engine run from settled shadow
picks in nba_bets. Returns a dict edge_engine uses to adjust raw model_prob
to empirically-calibrated displayed prob.

Sample-size-adaptive blend: with little data, lean toward the model's raw
probability (the bucket's empirical rate is noisy). With lots of data, lean
on the empirical rate. The weight on empirical scales linearly from 0 at
n=MIN_N up to 1.0 at n=FULL_TRUST_N.

Audit 2026-05-02 showed that even after calibration, model_probs in the 70-90%
bucket overshot realized hit rates by 12-19pp — the prior fixed-weight
40/60 blend was too gentle. The adaptive blend lets buckets with deep data
carry their own weight rather than being averaged against an overconfident raw.
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

# Adaptive-blend tuning. The audit's biggest losing buckets had n in the
# 20-50 range (e.g. pts 70-80% n=29, pra 70-80% n=21), so we want full
# trust kicking in at n=20 — otherwise the bucket where the cliff lives
# gets the weakest calibration pull.
_MIN_N         = 8     # minimum n to use the bucket at all
_FULL_TRUST_N  = 20    # at n ≥ this, blend is 100% empirical


def _bucket_label(prob: float) -> str:
    for label, lo, hi in _BUCKETS:
        if lo <= prob < hi:
            return label
    return "<55%"


def load_calibration_lookup(min_n: int = _MIN_N) -> dict:
    """Compute lookup from settled shadow picks.

    Returns {(market, bucket): (hit_rate, n)} — n is needed for adaptive blend.
    """
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
        n = len(g)
        if n < min_n:
            continue
        actual = float(g["is_win"].mean())
        out[(mkt, bucket)] = (actual, n)
    return out


def calibrate_prob(raw_prob: float, market: str, lookup: dict) -> float:
    """Blend raw model probability with empirical hit rate.

    Weight on empirical scales with bucket size:
      n=8   → 0%   weight on empirical (still too noisy, return raw)
      n=29  → 50%
      n=50+ → 100% weight on empirical (we trust the data more than the model)
    """
    if raw_prob is None or raw_prob != raw_prob:
        return raw_prob
    bucket = _bucket_label(raw_prob)
    entry = lookup.get((market, bucket))
    if entry is None:
        return raw_prob
    actual, n = entry
    # Linear ramp from 0 at MIN_N to 1.0 at FULL_TRUST_N
    if n <= _MIN_N:
        w = 0.0
    elif n >= _FULL_TRUST_N:
        w = 1.0
    else:
        w = (n - _MIN_N) / (_FULL_TRUST_N - _MIN_N)
    blended = (1.0 - w) * raw_prob + w * actual
    return round(blended, 4)


if __name__ == "__main__":
    lookup = load_calibration_lookup()
    print(f"Calibration lookup ({len(lookup)} entries with n≥{_MIN_N}):")
    for k, (actual, n) in sorted(lookup.items()):
        # weight at this n
        if n <= _MIN_N: w = 0.0
        elif n >= _FULL_TRUST_N: w = 1.0
        else: w = (n - _MIN_N) / (_FULL_TRUST_N - _MIN_N)
        print(f"  {k}: actual {actual*100:.1f}%  n={n}  weight on empirical={w*100:.0f}%")

"""
models/calibration.py — In-memory per-(market × prob_bucket) calibration.

No DB table needed — built fresh on each edge_engine run from settled shadow
picks in nba_bets. Returns a dict edge_engine uses to adjust raw model_prob
to empirically-calibrated displayed prob.

Sample-size-adaptive blend: with little data, lean toward the model's raw
probability (the bucket's empirical rate is noisy). With lots of data, lean
on the empirical rate. The weight on empirical scales linearly from 0 at
n=MIN_N up to 1.0 at n=FULL_TRUST_N.

Audit 2026-05-02 (round 1) showed that even after calibration, model_probs in
the 70-90% bucket overshot realized hit rates by 12-19pp — the prior fixed
40/60 blend was too gentle. Adaptive blend lets buckets with deep data carry
their own weight.

Audit 2026-05-02 (round 2) found the original `<55%` bucket was too coarse:
alt-ladder longshots (raw 5-15%) were lumped with main-line 40-55% close
calls into one bucket, manufacturing fake +37% edges. Fix: finer low-prob
buckets (<10, 10-25, 25-40, 40-55) so each raw-prob population calibrates
against its own history.

Audit 2026-05-02 (round 3) attempted to split alt vs main markets, but the
historical shadow-pick data predates the is_alt field — every settled pick
had is_alt=False, so alt buckets ended up empty. Result: alt-line locks like
Jaylen Brown 78% pra got NO calibration at all, leaving the same overshoot
bug in disguise. Fix: drop the alt/main split. The model's overshoot is
structural (NegBin fit + multipliers) and applies identically to both alt
and main lines — the finer buckets alone provide enough separation.

Also raised FULL_TRUST_N from 20 to 50: with only 20 settled picks, going
100% on empirical lets random noise drive calibration. 50 is closer to where
the bucket's rate actually stabilizes.
"""
from __future__ import annotations
import pandas as pd

from models.auto_log_picks import fetch_shadow_picks


_BUCKETS = [
    ("<10%",   0.00, 0.10),
    ("10-25%", 0.10, 0.25),
    ("25-40%", 0.25, 0.40),
    ("40-55%", 0.40, 0.55),
    ("55-60%", 0.55, 0.60),
    ("60-70%", 0.60, 0.70),
    ("70-80%", 0.70, 0.80),
    ("80%+",   0.80, 1.01),
]

_MIN_N         = 8     # minimum n to use the bucket at all
_FULL_TRUST_N  = 50    # at n ≥ this, blend is 100% empirical


def _bucket_label(prob: float) -> str:
    for label, lo, hi in _BUCKETS:
        if lo <= prob < hi:
            return label
    return "<10%"


def load_calibration_lookup(min_n: int = _MIN_N) -> dict:
    """Compute lookup from settled shadow picks.

    Returns {(market, bucket): (hit_rate, n)}.
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


def calibrate_prob(
    raw_prob: float,
    market: str,
    lookup: dict,
    is_alt: bool = False,
    situational_mult: float = 1.0,
) -> float:
    """Blend raw model probability with empirical hit rate.

    `is_alt` is accepted for API compatibility but ignored — alt and main
    markets share calibration buckets. The model's overshoot is structural,
    not market-specific.

    `situational_mult` is the player's combined multiplier for tonight
    (matchup × rest × playoff × injury). When it deviates substantially
    from neutral (1.0), the model is reading something the historical
    bucket data doesn't reflect (Game 7, bench in playoff, deep matchup,
    injury cascade). In those cases we DAMPEN the bucket calibration so
    situational signal isn't washed out by the bucket average.

    Deviation curve:
      |mult - 1| ≤ 0.05  → full calibration (typical regular-season pick)
      |mult - 1| = 0.10  → 50% calibration weight
      |mult - 1| ≥ 0.15  → no calibration (cap reached → trust raw)
    """
    if raw_prob is None or raw_prob != raw_prob:
        return raw_prob
    bucket = _bucket_label(raw_prob)
    entry = lookup.get((market, bucket))
    if entry is None:
        return raw_prob
    actual, n = entry
    if n <= _MIN_N:
        w = 0.0
    elif n >= _FULL_TRUST_N:
        w = 1.0
    else:
        w = (n - _MIN_N) / (_FULL_TRUST_N - _MIN_N)

    # Situational dampener: pickup deviation past 0.05, ramp to 0 at 0.15.
    deviation = abs(float(situational_mult) - 1.0)
    if deviation > 0.05:
        damp = max(0.0, min(1.0, 1.0 - (deviation - 0.05) / 0.10))
        w *= damp

    blended = (1.0 - w) * raw_prob + w * actual
    return round(blended, 4)


# ── Market-level confidence (historical ROI as a trust multiplier) ────────────
# Rationale: even after per-bucket calibration, some markets reliably outperform
# (pts +25%, pra +13%) while others scrape by (blk +1%, stl -9%). Raw "edge" is
# market_p - novig — but a 5% edge in a market with proven +25% ROI is more
# trustworthy than a 5% edge in a breakeven market. Surface that via a
# confidence multiplier scaled by historical ROI, used for ranking + Kelly
# sizing without distorting the underlying probability math.

_CONFIDENCE_MIN = 0.70   # at -30% ROI or worse, dampen aggressively
_CONFIDENCE_MAX = 1.30   # at +30% ROI or better, boost up to 1.3x
_MIN_N_FOR_CONF = 30     # need ≥30 settled picks to trust a market's ROI


def _profit_per_unit(price, result) -> float:
    """$ profit per $1 staked. Win returns the decimal payout, loss returns -1."""
    try:
        ml = float(price)
    except (TypeError, ValueError):
        return 0.0
    if result == "Win":
        return (ml / 100.0) if ml > 0 else (100.0 / abs(ml))
    if result == "Loss":
        return -1.0
    return 0.0


def load_market_confidence() -> dict:
    """Per-market confidence multiplier derived from historical $1-unit ROI.

    Returns {market_base: multiplier in [_CONFIDENCE_MIN, _CONFIDENCE_MAX]}.
    Markets with insufficient data (n < _MIN_N_FOR_CONF) get neutral 1.0.
    """
    settled = fetch_shadow_picks(only_pending=False, settled_only=True)
    if settled.empty:
        return {}
    settled = settled[settled["result"].isin(["Win", "Loss"])]
    if settled.empty:
        return {}
    out = {}
    for mkt, grp in settled.groupby("market_base"):
        n = len(grp)
        if n < _MIN_N_FOR_CONF:
            out[mkt] = 1.0
            continue
        # Average per-$1 profit over all settled picks
        profits = grp.apply(lambda r: _profit_per_unit(r["price"], r["result"]), axis=1)
        roi = float(profits.mean())
        # Map ROI to multiplier in [MIN, MAX]: ROI of 0 → 1.0, +30% → 1.3, -30% → 0.7
        mult = 1.0 + max(-0.30, min(0.30, roi))
        out[mkt] = round(max(_CONFIDENCE_MIN, min(_CONFIDENCE_MAX, mult)), 4)
    return out


if __name__ == "__main__":
    lookup = load_calibration_lookup()
    print(f"Calibration lookup ({len(lookup)} entries with n≥{_MIN_N}):")
    for k, (actual, n) in sorted(lookup.items()):
        if n <= _MIN_N: w = 0.0
        elif n >= _FULL_TRUST_N: w = 1.0
        else: w = (n - _MIN_N) / (_FULL_TRUST_N - _MIN_N)
        mkt, bucket = k
        print(f"  ({mkt}, {bucket}): actual {actual*100:.1f}%  n={n}  weight={w*100:.0f}%")
    print()
    conf = load_market_confidence()
    print(f"Market confidence multipliers ({len(conf)} markets):")
    for m, c in sorted(conf.items()):
        print(f"  {m:6s}: {c}")

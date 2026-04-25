"""Tests for models/distribution.py — verify the math holds before relying on it."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from models.distribution import fit_distribution, StatDistribution


def test_fit_returns_none_for_too_few_games():
    assert fit_distribution([20, 22]) is None


def test_fit_recovers_known_mean():
    # 30 games of LeBron-ish points: mean ~25, var ~50
    rng = np.random.default_rng(42)
    samples = rng.negative_binomial(n=12.5, p=12.5 / (12.5 + 25.0), size=30).tolist()
    d = fit_distribution(samples)
    assert d is not None
    # Blended mean should land near the sample mean within ±3
    assert abs(d.mu - 25.0) < 3.0
    assert d.alpha > 0


def test_prob_over_under_sum_to_one():
    d = StatDistribution(mu=20.0, alpha=0.1, n_games=30, season_avg=20.0, recent_avg=20.0)
    # For a non-integer line, P(over) + P(under) should = 1
    p_over = d.prob_over(22.5)
    p_under = d.prob_under(22.5)
    assert abs((p_over + p_under) - 1.0) < 1e-6


def test_prob_at_least_monotone_decreasing():
    d = StatDistribution(mu=20.0, alpha=0.1, n_games=30, season_avg=20.0, recent_avg=20.0)
    # Higher threshold → lower probability of clearing it
    probs = [d.prob_at_least(t) for t in (10, 15, 20, 25, 30, 40)]
    for a, b in zip(probs, probs[1:]):
        assert a >= b


def test_alt_ladder_pricing():
    """The point of NegBin: one fit prices the entire ladder cheaply."""
    d = StatDistribution(mu=24.0, alpha=0.15, n_games=30, season_avg=24.0, recent_avg=24.0)
    ladder = [16.5, 18.5, 20.5, 22.5, 24.5, 26.5, 28.5, 30.5, 32.5]
    probs = {line: d.prob_over(line) for line in ladder}
    # Sanity: lowest line should be likely, highest line should be remote.
    # With μ=24 α=0.15, variance ≈ 110 (std ≈ 10.5) — being 7.5 below the mean
    # is still ~74% to clear, not 85%+. That's the correct call.
    assert probs[16.5] > 0.70
    assert probs[32.5] < 0.25
    # Each step down the ladder should have monotonically lower prob
    last = 1.1
    for line in ladder:
        assert probs[line] < last
        last = probs[line]


def test_underdispersed_falls_back_to_poisson():
    # All 20s — variance = 0, would crash a naive NegBin fit
    d = fit_distribution([20] * 20)
    assert d is not None
    assert d.alpha == 0.01  # Poisson-ish floor
    assert abs(d.mu - 20.0) < 0.01

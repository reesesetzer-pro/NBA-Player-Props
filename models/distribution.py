"""
models/distribution.py — Per-player stat-line distribution fitting.

Fits a negative binomial to each (player, stat) using the player's recent game
logs. NegBin handles overdispersion that real NBA box scores show
(variance > mean for points/rebounds/assists), which Poisson can't.

The killer feature: once we have (μ, α) for a player+stat, we can price ANY
alt line — over 24.5 pts AND over 28.5 pts AND over 32.5 pts — from one fit.
That's the foundation of the alt-ladder edge hunt.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import numpy as np
from scipy import stats

from config import (
    DIST_ROLLING_WINDOW, DIST_SEASON_WEIGHT, DIST_RECENT_WEIGHT,
    MIN_GAMES_FOR_FIT,
)


@dataclass
class StatDistribution:
    """Negative-binomial fit for one player's stat (e.g. LeBron's points).

    Parameters use the (μ, α) parameterization where:
      μ = mean
      α = dispersion such that Var = μ + α·μ²  (α=0 ⇒ Poisson)

    scipy.stats.nbinom uses (n, p) instead, with:
      n = 1/α         (number of failures)
      p = n / (n + μ) (success probability)
    """
    mu: float
    alpha: float
    n_games: int
    season_avg: float
    recent_avg: float

    # ── scipy parameterization ────────────────────────────────────────────────
    @property
    def n(self) -> float:
        return 1.0 / max(self.alpha, 1e-6)

    @property
    def p(self) -> float:
        return self.n / (self.n + self.mu)

    # ── Probability queries ───────────────────────────────────────────────────
    def prob_at_least(self, threshold: float) -> float:
        """P(stat ≥ threshold). Use this to price an OVER on a line.

        For a typical alt line of "Over 24.5 points", call prob_at_least(25)
        since you need 25+ to cash. Books always set lines on .5 boundaries
        for this reason — no pushes.
        """
        k = int(np.ceil(threshold))
        return float(1.0 - stats.nbinom.cdf(k - 1, self.n, self.p))

    def prob_at_most(self, threshold: float) -> float:
        """P(stat ≤ threshold). Use this to price an UNDER."""
        k = int(np.floor(threshold))
        return float(stats.nbinom.cdf(k, self.n, self.p))

    def prob_over(self, line: float) -> float:
        """P(stat > line). For a .5 line this is identical to prob_at_least(line+0.5)."""
        if line == int(line):
            # Integer line — push possible at exactly `line`
            return float(1.0 - stats.nbinom.cdf(int(line), self.n, self.p))
        return self.prob_at_least(line + 0.5)

    def prob_under(self, line: float) -> float:
        if line == int(line):
            return float(stats.nbinom.cdf(int(line) - 1, self.n, self.p))
        return self.prob_at_most(line - 0.5)

    def expected_value(self) -> float:
        return self.mu

    def variance(self) -> float:
        return self.mu + self.alpha * (self.mu ** 2)

    def to_dict(self) -> dict:
        return {
            "mu": round(self.mu, 3),
            "alpha": round(self.alpha, 4),
            "variance": round(self.variance(), 3),
            "n_games": self.n_games,
            "season_avg": round(self.season_avg, 2),
            "recent_avg": round(self.recent_avg, 2),
        }


def _moment_fit(values: np.ndarray) -> tuple[float, float]:
    """Method-of-moments fit: returns (μ, α). Falls back to Poisson (α=tiny)
    when the sample variance is at or below the mean (underdispersed)."""
    if len(values) == 0:
        return 0.0, 0.01
    mu = float(np.mean(values))
    if mu <= 0:
        return 0.0, 0.01
    var = float(np.var(values, ddof=1)) if len(values) > 1 else mu
    # NegBin requires var > mu. If sample is underdispersed, bias toward Poisson.
    if var <= mu:
        return mu, 0.01
    alpha = (var - mu) / (mu ** 2)
    return mu, max(alpha, 0.01)


def fit_distribution(stat_values: list[float]) -> Optional[StatDistribution]:
    """Fit a negative binomial to a player's stat history.

    Strategy:
      1. Compute season μ from all available games.
      2. Compute recent μ from the last DIST_ROLLING_WINDOW games.
      3. Blend: μ = 0.4·season + 0.6·recent (recency bias — playoff form
         and rotation changes matter more than 50-game-old performance).
      4. Fit α from the *recent* window's variance (current variance >
         season variance for players in/out of slumps).

    Returns None if insufficient data.
    """
    if not stat_values or len(stat_values) < MIN_GAMES_FOR_FIT:
        return None

    arr = np.asarray(stat_values, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < MIN_GAMES_FOR_FIT:
        return None

    season_mu, _ = _moment_fit(arr)

    # Recent window — most recent `window` games. Caller is expected to pass
    # values in chronological order (oldest first), so we take the tail.
    window = arr[-DIST_ROLLING_WINDOW:]
    recent_mu, recent_alpha = _moment_fit(window)

    # Blend means; use the recent window's dispersion (variance moves faster
    # than the long-run mean during the season).
    blended_mu = DIST_SEASON_WEIGHT * season_mu + DIST_RECENT_WEIGHT * recent_mu

    return StatDistribution(
        mu=blended_mu,
        alpha=recent_alpha,
        n_games=len(arr),
        season_avg=season_mu,
        recent_avg=recent_mu,
    )

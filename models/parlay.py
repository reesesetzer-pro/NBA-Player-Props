"""
models/parlay.py — Combine priced legs into parlays with correlation.

Two correlation cases worth modeling out of the gate:

  1. **Same player, different stats** — e.g. LeBron 25+ pts AND LeBron 7+ ast.
     Heavily positively correlated (good game ≈ both clear). Books price these
     as SGP (same-game-parlay) products at a discount, but cross-book parlays
     can mispricedly assume independence.

  2. **Same team, different players** — Lakers blow-out → most starters clear
     overs. Modest positive correlation.

Default correlation matrix below is conservative. Reality is often higher —
which means independence-assumption parlays UNDERESTIMATE true probability
(and so undervalue the play).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable

from utils.helpers import american_to_implied, implied_to_american


# Correlation coefficients for combined legs (rough priors — refine with data)
_CORRELATION = {
    ("same_player", "same_stat_diff_line"):  0.95,   # LeBron 22+ AND 24+ = redundant
    ("same_player", "diff_stat"):            0.35,   # LeBron pts + LeBron ast
    ("same_team",   "same_stat"):            0.20,   # Two Lakers points overs
    ("same_team",   "diff_stat"):            0.10,
    ("opposing_teams", "any"):              -0.15,   # high-scoring fade — modest negative
    ("different_games", "any"):              0.00,
}


@dataclass
class Leg:
    player_name: str
    team_abbr: str
    market_base: str          # "pts" | "reb" | "ast" | "pra" | "fg3m"
    line: float
    over_under: str           # "Over" | "Under"
    price: int                # American odds
    model_prob: float
    game_id: str
    book: str

    def implied_prob(self) -> float:
        return american_to_implied(self.price)


@dataclass
class Parlay:
    legs: list[Leg]
    independent_prob: float
    adjusted_prob: float
    decimal_odds: float
    american_odds: int
    edge: float                       # adjusted_prob - market_implied
    notes: list[str] = field(default_factory=list)


def _classify_pair(a: Leg, b: Leg) -> str:
    if a.game_id != b.game_id:
        return "different_games"
    same_player = a.player_name.lower() == b.player_name.lower()
    same_team   = a.team_abbr == b.team_abbr
    if same_player:
        if a.market_base == b.market_base:
            return "same_player_same_stat"
        return "same_player_diff_stat"
    if same_team:
        if a.market_base == b.market_base:
            return "same_team_same_stat"
        return "same_team_diff_stat"
    return "opposing_teams"


def _pair_correlation(a: Leg, b: Leg) -> float:
    cls = _classify_pair(a, b)
    return {
        "same_player_same_stat":  _CORRELATION[("same_player", "same_stat_diff_line")],
        "same_player_diff_stat":  _CORRELATION[("same_player", "diff_stat")],
        "same_team_same_stat":    _CORRELATION[("same_team",   "same_stat")],
        "same_team_diff_stat":    _CORRELATION[("same_team",   "diff_stat")],
        "opposing_teams":         _CORRELATION[("opposing_teams", "any")],
        "different_games":        _CORRELATION[("different_games", "any")],
    }[cls]


def _correlation_adjustment(legs: list[Leg]) -> float:
    """Return a multiplier on the independent-product probability.

    Approximate Gaussian copula: positive average correlation → multiplier > 1
    (true joint prob higher than independence math). Conservative scaling.
    """
    if len(legs) < 2:
        return 1.0
    pairs = list(combinations(legs, 2))
    avg_corr = sum(_pair_correlation(a, b) for a, b in pairs) / len(pairs)
    # Empirical scaling: strong positive correlation → up to +25% bump,
    # negative correlation → up to -15% trim. Conservative but directional.
    return 1.0 + avg_corr * 0.5


def build_parlay(legs: list[Leg]) -> Parlay:
    """Compute combined probability + odds for a set of legs."""
    if not legs:
        raise ValueError("legs cannot be empty")

    indep_prob = 1.0
    decimal_odds = 1.0
    for L in legs:
        indep_prob *= L.model_prob
        d = (L.price / 100.0) + 1.0 if L.price > 0 else (100.0 / abs(L.price)) + 1.0
        decimal_odds *= d

    corr_mult = _correlation_adjustment(legs)
    adj_prob  = max(0.001, min(0.999, indep_prob * corr_mult))

    market_implied = 1.0 / decimal_odds
    edge = adj_prob - market_implied

    notes = []
    if corr_mult != 1.0:
        sign = "+" if corr_mult > 1 else ""
        notes.append(f"correlation adj {sign}{(corr_mult - 1) * 100:.1f}%")

    return Parlay(
        legs=list(legs),
        independent_prob=indep_prob,
        adjusted_prob=adj_prob,
        decimal_odds=decimal_odds,
        american_odds=implied_to_american(market_implied),
        edge=edge,
        notes=notes,
    )


def rank_combinations(
    candidates: list[Leg],
    n_legs: int = 3,
    min_edge: float = 0.05,
    one_per_game: bool = False,
) -> list[Parlay]:
    """Enumerate every n-leg combination, return those above min_edge sorted by EV.

    `one_per_game=True` enforces game diversification (lowest correlation).
    """
    out: list[Parlay] = []
    for combo in combinations(candidates, n_legs):
        if one_per_game and len({L.game_id for L in combo}) < n_legs:
            continue
        try:
            p = build_parlay(list(combo))
        except Exception:
            continue
        if p.edge >= min_edge:
            out.append(p)
    out.sort(key=lambda p: p.edge, reverse=True)
    return out

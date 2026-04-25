"""Tests for models/parlay.py."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from models.parlay import Leg, build_parlay, rank_combinations


def make_leg(player="A", team="LAL", market="pts", line=20.5, ou="Over",
             price=-110, prob=0.55, game="g1", book="draftkings") -> Leg:
    return Leg(player, team, market, line, ou, price, prob, game, book)


def test_independence_when_different_games():
    legs = [
        make_leg(player="A", game="g1", prob=0.6, price=-110),
        make_leg(player="B", game="g2", prob=0.6, price=-110),
    ]
    p = build_parlay(legs)
    # No correlation between different games — adjusted ≈ independent
    assert abs(p.adjusted_prob - p.independent_prob) < 0.001
    # Independent prob: 0.6 × 0.6 = 0.36
    assert abs(p.independent_prob - 0.36) < 0.001


def test_same_player_diff_stat_correlation_positive():
    # LeBron pts + LeBron ast — should have positive correlation
    legs = [
        make_leg(player="LeBron", market="pts", prob=0.6),
        make_leg(player="LeBron", market="ast", prob=0.6),
    ]
    p = build_parlay(legs)
    # Adjusted prob should exceed independence
    assert p.adjusted_prob > p.independent_prob
    assert p.notes  # should mention correlation adj


def test_opposing_teams_negative_correlation():
    # If both teams cover an over, that's a high-scoring game; if neither covers,
    # low-scoring. So opposing-team overs are weakly NEGATIVELY correlated in our
    # simple model.
    legs = [
        make_leg(player="A", team="LAL", game="g1", prob=0.6),
        make_leg(player="B", team="BOS", game="g1", prob=0.6),
    ]
    p = build_parlay(legs)
    assert p.adjusted_prob < p.independent_prob


def test_ranking_filters_by_edge():
    legs = [
        make_leg(player=f"P{i}", game=f"g{i}", prob=0.55 + i*0.01, price=-105)
        for i in range(5)
    ]
    ranked = rank_combinations(legs, n_legs=3, min_edge=0.0, one_per_game=True)
    assert len(ranked) > 0
    # All returned parlays should pass min_edge
    for p in ranked:
        assert p.edge >= 0.0


def test_empty_legs_raises():
    with pytest.raises(ValueError):
        build_parlay([])

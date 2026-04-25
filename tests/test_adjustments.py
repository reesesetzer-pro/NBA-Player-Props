"""Tests for models/adjustments.py."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from models.adjustments import (
    matchup_multiplier, rest_multiplier, playoff_multiplier,
    injury_multiplier, compose,
)


def test_rest_multiplier_basic():
    assert rest_multiplier(0)[0] < 1.0       # B2B penalty
    assert rest_multiplier(1)[0] == 1.0      # baseline
    assert rest_multiplier(3)[0] > 1.0       # rested
    assert rest_multiplier(None)[0] == 1.0   # unknown → neutral


def test_matchup_multiplier_caps():
    df = pd.DataFrame([
        {"team_abbr": "BOS", "opp_position": "PG", "stat": "pts", "multiplier": 0.50},
        {"team_abbr": "WAS", "opp_position": "PG", "stat": "pts", "multiplier": 1.50},
    ])
    # 0.50 should cap to 1 - 0.12 = 0.88
    assert abs(matchup_multiplier(df, "BOS", "PG", "pts", cap=0.12)[0] - 0.88) < 1e-6
    # 1.50 should cap to 1 + 0.12 = 1.12
    assert abs(matchup_multiplier(df, "WAS", "PG", "pts", cap=0.12)[0] - 1.12) < 1e-6
    # Missing row → neutral
    assert matchup_multiplier(df, "LAL", "PG", "pts")[0] == 1.0


def test_playoff_star_boost():
    base, _ = playoff_multiplier(is_playoff=True, minutes_per_game=36, series_fatigue=0.0)
    assert base > 1.0


def test_playoff_bench_penalty():
    base, _ = playoff_multiplier(is_playoff=True, minutes_per_game=10, series_fatigue=0.0)
    assert base < 1.0


def test_playoff_fatigue_compounds():
    fresh, _ = playoff_multiplier(True, 36, series_fatigue=0.0)
    tired, _ = playoff_multiplier(True, 36, series_fatigue=1.0)
    assert tired < fresh


def test_regular_season_returns_neutral():
    assert playoff_multiplier(False, 36, 0.0)[0] == 1.0


def test_injury_no_teammates_out():
    inj = pd.DataFrame([{"team_abbr": "LAL", "status": "questionable",
                         "player_id": 999, "minutes_impact": 30}])
    # Player 999 is the one we're checking — no OTHER teammates out
    assert injury_multiplier(inj, "LAL", player_id=1)[0] == 1.0


def test_injury_star_out_bumps():
    inj = pd.DataFrame([{"team_abbr": "LAL", "status": "out",
                         "player_id": 999, "minutes_impact": 35}])
    mult, _ = injury_multiplier(inj, "LAL", player_id=1)
    assert mult > 1.0
    assert mult <= 1.10        # cap


def test_compose_runs_end_to_end():
    pos_def = pd.DataFrame([
        {"team_abbr": "BOS", "opp_position": "SG", "stat": "pts", "multiplier": 0.92},
    ])
    inj = pd.DataFrame([{"team_abbr": "LAL", "status": "out",
                         "player_id": 999, "minutes_impact": 28}])
    a = compose(
        pos_def, inj,
        opponent_abbr="BOS", player_position="SG", stat="pts",
        days_rest=2, is_playoff=True, minutes_per_game=33, series_fatigue=0.3,
        team_abbr="LAL", player_id=1,
    )
    # Combined multiplier should be a real number, not blow up
    assert 0.5 < a.combined < 1.5
    assert len(a.notes) == 4

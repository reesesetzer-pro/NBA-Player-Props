"""
models/adjustments.py — Composable multipliers on the player's stat μ.

Every adjustment returns a small multiplier (typically 0.85 → 1.15). They
multiply together to give the final adjusted μ that gets fed back into the
distribution to price the prop.

    final_mu = base_mu × matchup × rest × playoff × injury

Designed for easy A/B removal — comment out one factor, re-fit, see what changes.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, ClassVar
import pandas as pd

from config import (
    PLAYOFF_SERIES_FATIGUE_PENALTY,
    PLAYOFF_STAR_MIN_BOOST,
    PLAYOFF_BENCH_MIN_PENALTY,
)


# Hard cap on the cumulative multiplier. Prevents the runaway-confident
# picks that surface when 4+ favorable signals all stack (audit 2026-05-02
# showed 7-15% edge buckets with -54% / -41% realized ROI — exactly the
# bets where every adjustment was firing in the same direction and
# independence math overstated joint probability).
# ±15% is roomy enough that a single strong matchup edge survives, but
# prevents the 1.05 × 1.10 × 1.10 × 1.08 ≈ 1.37 stacks we were seeing.
_COMBINED_CAP_LO = 0.85
_COMBINED_CAP_HI = 1.15


@dataclass
class AdjustmentBreakdown:
    """Per-pick traceability — which factors moved μ how much."""
    matchup: float = 1.0
    rest: float = 1.0
    playoff: float = 1.0
    injury: float = 1.0
    notes: list[str] = field(default_factory=list)

    @property
    def combined(self) -> float:
        raw = self.matchup * self.rest * self.playoff * self.injury
        return max(_COMBINED_CAP_LO, min(_COMBINED_CAP_HI, raw))

    @property
    def combined_uncapped(self) -> float:
        """For diagnostics — the would-be multiplier before clamping."""
        return self.matchup * self.rest * self.playoff * self.injury


# ─────────────────────────────────────────────────────────────────────────────
# Matchup
# ─────────────────────────────────────────────────────────────────────────────

def matchup_multiplier(
    pos_def_df: pd.DataFrame,
    opponent_abbr: str,
    player_position: str,
    stat: str,                    # "pts" | "reb" | "ast" | "pra" | "fg3m"
    cap: float = 0.12,            # max ±12% swing — keep adjustments humble
) -> tuple[float, str]:
    """Look up the opponent's defense vs this position for this stat.

    Returns (multiplier, reason). multiplier is the ratio of opponent's allowed
    average to league average, clipped to [1-cap, 1+cap].
    """
    if pos_def_df is None or pos_def_df.empty:
        return 1.0, "no pos_def data"
    row = pos_def_df[
        (pos_def_df["team_abbr"] == opponent_abbr)
        & (pos_def_df["opp_position"] == player_position)
        & (pos_def_df["stat"] == stat)
    ]
    if row.empty:
        return 1.0, f"no row for {opponent_abbr}/{player_position}/{stat}"
    mult = float(row.iloc[0].get("multiplier") or 1.0)
    capped = max(1.0 - cap, min(1.0 + cap, mult))
    return capped, f"{opponent_abbr} allows {mult:.2f}× league avg to {player_position} {stat}"


# ─────────────────────────────────────────────────────────────────────────────
# Rest
# ─────────────────────────────────────────────────────────────────────────────

# Empirical NBA rest impact (regression on 5+ years of data):
#   B2B (0 days rest):  ~3% scoring drop, ~2 min lost
#   1 day rest:         neutral (baseline)
#   2 days rest:        ~1% boost
#   3+ days rest:       ~2% boost (well-rested but a touch of rust risk)
_REST_MULTIPLIER = {
    0: 0.97,
    1: 1.00,
    2: 1.01,
    3: 1.02,
}


def rest_multiplier(days_rest: Optional[int]) -> tuple[float, str]:
    if days_rest is None:
        return 1.00, "rest unknown"
    if days_rest >= 4:
        return 1.02, f"{days_rest}d rest"
    return _REST_MULTIPLIER.get(days_rest, 1.0), f"{days_rest}d rest"


# ─────────────────────────────────────────────────────────────────────────────
# Playoff context
# ─────────────────────────────────────────────────────────────────────────────

def playoff_multiplier(
    is_playoff: bool,
    minutes_per_game: Optional[float],
    series_fatigue: float = 0.0,           # 0.0 (fresh) → 1.0 (just played 7 games)
) -> tuple[float, str]:
    """Playoff context shifts the stat distribution materially:
      - Stars (≥30 mpg) play even more — boost μ
      - Bench (<18 mpg) plays much less or not at all — heavy penalty
      - Mid-rotation roughly neutral
      - Series fatigue applied multiplicatively at end
    """
    if not is_playoff:
        return 1.0, "regular season"

    if minutes_per_game is None:
        base = 1.0
        note = "playoff (mpg unknown)"
    elif minutes_per_game >= 30:
        base = 1.0 + PLAYOFF_STAR_MIN_BOOST
        note = "playoff star (≥30 mpg)"
    elif minutes_per_game < 18:
        base = 1.0 - PLAYOFF_BENCH_MIN_PENALTY
        note = "playoff bench (<18 mpg)"
    else:
        base = 1.0
        note = "playoff rotation"

    fatigue_mult = 1.0 - PLAYOFF_SERIES_FATIGUE_PENALTY * series_fatigue
    if series_fatigue > 0:
        note += f" · fatigue {series_fatigue:.2f}"
    return base * fatigue_mult, note


# ─────────────────────────────────────────────────────────────────────────────
# Injury context
# ─────────────────────────────────────────────────────────────────────────────

def injury_multiplier(
    injuries_df: pd.DataFrame,
    team_abbr: str,
    player_id: int,
) -> tuple[float, str]:
    """When a teammate is OUT, this player's usage typically rises.

    Simple heuristic for v1: each OUT teammate worth ~3% bump per missed
    star (≥30mpg), ~1.5% per missed rotation player.

    Tier 3 will replace this with full minute redistribution from a usage
    network — for now this gets the directional signal right.
    """
    if injuries_df is None or injuries_df.empty:
        return 1.0, "no injury data"

    teammates_out = injuries_df[
        (injuries_df["team_abbr"] == team_abbr)
        & (injuries_df["status"].isin(["out", "doubtful"]))
        & (injuries_df["player_id"] != player_id)
    ]
    if teammates_out.empty:
        return 1.0, "no teammates out"

    bump = 0.0
    for _, r in teammates_out.iterrows():
        impact = float(r.get("minutes_impact") or 0.0)
        if impact >= 30:
            bump += 0.03
        elif impact >= 18:
            bump += 0.015
        else:
            bump += 0.005

    bump = min(bump, 0.10)        # cap at +10% even with multiple stars out
    return 1.0 + bump, f"{len(teammates_out)} teammate(s) out → +{bump*100:.1f}%"


# ─────────────────────────────────────────────────────────────────────────────
# Compose
# ─────────────────────────────────────────────────────────────────────────────

def compose(
    pos_def_df: pd.DataFrame,
    injuries_df: pd.DataFrame,
    *,
    opponent_abbr: str,
    player_position: str,
    stat: str,
    days_rest: Optional[int],
    is_playoff: bool,
    minutes_per_game: Optional[float],
    series_fatigue: float = 0.0,
    team_abbr: str,
    player_id: int,
) -> AdjustmentBreakdown:
    """Run all four adjustments and return the breakdown."""
    out = AdjustmentBreakdown()
    out.matchup, m_note = matchup_multiplier(pos_def_df, opponent_abbr, player_position, stat)
    out.rest, r_note    = rest_multiplier(days_rest)
    out.playoff, p_note = playoff_multiplier(is_playoff, minutes_per_game, series_fatigue)
    out.injury, i_note  = injury_multiplier(injuries_df, team_abbr, player_id)
    out.notes = [m_note, r_note, p_note, i_note]
    return out

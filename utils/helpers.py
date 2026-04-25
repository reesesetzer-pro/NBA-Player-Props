"""
utils/helpers.py — odds conversion, name normalization, formatting.
"""
from __future__ import annotations
from typing import Optional, Tuple
import unicodedata

from config import NBA_TEAMS, TEAM_NAME_TO_ABBR


# ── Odds conversion ───────────────────────────────────────────────────────────

def american_to_implied(american: float) -> float:
    """American odds → implied probability (with vig)."""
    if american > 0:
        return 100.0 / (american + 100.0)
    return abs(american) / (abs(american) + 100.0)


def implied_to_american(p: float) -> int:
    """Implied probability → American odds (rounded)."""
    if p <= 0 or p >= 1:
        raise ValueError(f"Probability must be in (0, 1), got {p}")
    if p < 0.5:
        return int(round((100.0 / p) - 100.0))
    return int(round(-(p / (1.0 - p)) * 100.0))


def remove_vig(prob_a: float, prob_b: float) -> Tuple[float, float]:
    """Strip the bookmaker's overround from a 2-way market."""
    total = prob_a + prob_b
    if total <= 0:
        return 0.0, 0.0
    return prob_a / total, prob_b / total


def kelly_fraction(model_p: float, american_odds: float) -> float:
    """Optimal fraction of bankroll per Kelly. Returns 0 if no edge."""
    if model_p <= 0 or model_p >= 1:
        return 0.0
    b = (american_odds / 100.0) if american_odds > 0 else (100.0 / abs(american_odds))
    q = 1.0 - model_p
    f = (b * model_p - q) / b
    return max(0.0, f)


def fmt_odds(american: Optional[float]) -> str:
    if american is None:
        return "—"
    a = int(american)
    return f"+{a}" if a > 0 else str(a)


# ── Name handling ─────────────────────────────────────────────────────────────

def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def name_to_abbr(name: str) -> str:
    """Convert team name from Odds API to abbreviation."""
    if name in TEAM_NAME_TO_ABBR:
        return TEAM_NAME_TO_ABBR[name]
    stripped = _strip_accents(name)
    if stripped in TEAM_NAME_TO_ABBR:
        return TEAM_NAME_TO_ABBR[stripped]
    lower = stripped.lower().strip()
    for full, abbr in TEAM_NAME_TO_ABBR.items():
        if lower in full.lower() or full.lower() in lower:
            return abbr
    return name[:3].upper()


def normalize_player_name(name: str) -> str:
    """Lowercase + strip accents + collapse whitespace. Used as join key."""
    return " ".join(_strip_accents(name).lower().split())

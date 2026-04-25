"""models/kelly.py — Kelly sizing, with fractional variants.

Kelly = (b·p - q) / b   where b = decimal_odds - 1, p = win prob, q = 1 - p.
Returns the fraction of bankroll to wager. Negative or zero → no bet.
"""
from __future__ import annotations
from typing import Tuple

from utils.helpers import kelly_fraction
from config import KELLY_BANKROLL


def kelly_dollars(model_prob: float, american_odds: float, bankroll: float = KELLY_BANKROLL) -> Tuple[float, float, float]:
    """Return (full, half, quarter) Kelly stake in dollars.

    Most live bettors use fractional Kelly (1/2 or 1/4) to reduce bankroll
    variance — full Kelly is theoretically optimal but emotionally brutal.
    """
    f = kelly_fraction(model_prob, american_odds)
    full = round(bankroll * f, 2)
    return full, round(full * 0.5, 2), round(full * 0.25, 2)

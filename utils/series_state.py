"""utils/series_state.py — derive NBA playoff series state from ESPN scoreboard.

The NBA Stats `commonplayoffseries` endpoint that the official `playoff_sync`
job uses doesn't return game scores, so all rows show 0-0 wins (won't ever
flag a Game 7 or elimination game). This module sidesteps that by walking
ESPN's `/scoreboard?dates=YYYYMMDD` endpoint over the playoff window,
parsing scores + winner flags, and counting head-to-head wins per matchup.

Result is cached to `.series_state_cache.json` with a 1-hour TTL so we
don't hammer ESPN on every edge_engine run.
"""
from __future__ import annotations
import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests

_CACHE_PATH = Path(__file__).resolve().parent.parent / ".series_state_cache.json"
_CACHE_TTL  = 3600   # seconds
_HEADERS    = {"User-Agent": "Mozilla/5.0"}
_ESPN_URL   = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

# Playoff window — regular season typically ends mid-April; finals run into June.
_WINDOW_START_MD = (4, 13)   # April 13 (conservative — covers play-in too)
_WINDOW_END_MD   = (6, 30)


def _load_cache() -> dict:
    try:
        with open(_CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(data: dict) -> None:
    try:
        with open(_CACHE_PATH, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _fetch_date(d: date) -> list[dict]:
    """ESPN scoreboard for one date. Returns list of (away_abbr, home_abbr, away_score, home_score, away_won, home_won, headline)."""
    try:
        r = requests.get(_ESPN_URL, params={"dates": d.strftime("%Y%m%d")},
                         headers=_HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    rows = []
    for e in data.get("events", []):
        for comp in e.get("competitions", []):
            status = comp.get("status", {}).get("type", {}).get("name", "")
            if status != "STATUS_FINAL":
                continue
            home = away = None
            for c in comp.get("competitors", []):
                team = c.get("team", {}).get("abbreviation")
                score = int(c.get("score") or 0)
                won = bool(c.get("winner", False))
                rec = (team, score, won)
                if c.get("homeAway") == "home":
                    home = rec
                else:
                    away = rec
            if not home or not away:
                continue
            headline = ""
            for n in comp.get("notes", []):
                hl = n.get("headline", "") or ""
                if hl:
                    headline = hl
                    break
            rows.append({
                "away": away[0], "home": home[0],
                "away_score": away[1], "home_score": home[1],
                "away_won": away[2], "home_won": home[2],
                "headline": headline,
            })
    return rows


def _is_playoff_game(headline: str) -> bool:
    """ESPN tags playoff games in `notes[].headline` (e.g. 'East 1st Round - Game 6')."""
    h = (headline or "").lower()
    return any(k in h for k in (" round", "conf finals", "conference finals", "nba finals", " finals"))


def _build_series_state(season: int) -> dict:
    """Walk the playoff window for `season` and aggregate wins per matchup.

    Returns {f"{abbrA}-{abbrB}": {"a": abbrA, "b": abbrB, "a_wins": n, "b_wins": m,
                                  "games_played": k, "is_complete": bool,
                                  "is_game7": bool, "is_elimination": bool,
                                  "fatigue_a": f, "fatigue_b": f}}
    Key is sorted alphabetically so lookup works either way.
    """
    today = date.today()
    start = date(season, *_WINDOW_START_MD)
    end   = min(date(season, *_WINDOW_END_MD), today)
    counts: dict[str, dict] = {}
    cur = start
    while cur <= end:
        for r in _fetch_date(cur):
            if not _is_playoff_game(r["headline"]):
                continue
            a, b = sorted([r["away"], r["home"]])
            key = f"{a}-{b}"
            entry = counts.setdefault(key, {"a": a, "b": b, "a_wins": 0, "b_wins": 0})
            winner = r["away"] if r["away_won"] else r["home"]
            if winner == a:
                entry["a_wins"] += 1
            elif winner == b:
                entry["b_wins"] += 1
        cur += timedelta(days=1)
    # Compute derived flags
    for key, e in counts.items():
        gp = e["a_wins"] + e["b_wins"]
        e["games_played"] = gp
        e["is_complete"]  = (e["a_wins"] == 4 or e["b_wins"] == 4)
        e["is_game7"]     = (e["a_wins"] == 3 and e["b_wins"] == 3)
        e["is_elimination"] = (max(e["a_wins"], e["b_wins"]) == 3) and not e["is_game7"] and not e["is_complete"]
        e["fatigue_a"]    = round(gp / 7.0, 3)
        e["fatigue_b"]    = round(gp / 7.0, 3)
    return counts


def get_series_state(home_abbr: str, away_abbr: str, season: Optional[int] = None) -> dict:
    """Return series state for the matchup. Empty dict if no series found."""
    if season is None:
        season = date.today().year
    cache = _load_cache()
    cached = cache.get(str(season))
    if not cached or (time.time() - cached.get("_ts", 0)) > _CACHE_TTL:
        state = _build_series_state(season)
        cache[str(season)] = {"_ts": time.time(), "state": state}
        _save_cache(cache)
    else:
        state = cached["state"]

    a, b = sorted([home_abbr, away_abbr])
    e = state.get(f"{a}-{b}", {})
    if not e:
        return {}
    # Map a/b back to home/away
    home_is_a = (home_abbr == e["a"])
    return {
        "home_wins":       e["a_wins"] if home_is_a else e["b_wins"],
        "away_wins":       e["b_wins"] if home_is_a else e["a_wins"],
        "games_played":    e["games_played"],
        "is_complete":     e["is_complete"],
        "is_game7":        e["is_game7"],
        "is_elimination":  e["is_elimination"],
        "series_fatigue_home": e["fatigue_a"] if home_is_a else e["fatigue_b"],
        "series_fatigue_away": e["fatigue_b"] if home_is_a else e["fatigue_a"],
    }

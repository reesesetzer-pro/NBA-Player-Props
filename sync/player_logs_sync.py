"""
sync/player_logs_sync.py — Backfill + incremental sync of NBA player game logs.

Source: stats.nba.com (via the `nba_api` package, which handles the User-Agent
and Referer headers stats.nba.com requires).

Schema (Supabase table `nba_player_logs`):
    id              TEXT PK   — md5(game_id|player_id)
    game_id         TEXT
    game_date       DATE
    season          TEXT      — e.g. "2025-26"
    season_type     TEXT      — "regular" or "playoffs"
    player_id       INT
    player_name     TEXT
    team_abbr       TEXT
    opponent_abbr   TEXT
    is_home         BOOL
    minutes         FLOAT
    pts             INT
    reb             INT
    ast             INT
    fg3m            INT
    blk             INT
    stl             INT
    tov             INT
    pra             INT      — pts + reb + ast (precomputed for combos)
    plus_minus      INT
    updated_at      TIMESTAMPTZ
"""
from __future__ import annotations
import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Iterable

import pandas as pd
from nba_api.stats.endpoints import playergamelogs

from config import CURRENT_SEASON, PRIOR_SEASON
from utils.db import upsert


def _make_id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


_MATCHUP_RE = re.compile(r"^(\w{3})\s+(@|vs\.?)\s+(\w{3})$")


def _parse_matchup(matchup: str, team_abbr: str) -> tuple[str, bool]:
    """NBA logs encode opponent + venue in MATCHUP like 'LAL @ DEN' or 'BOS vs. NYK'.
    Returns (opponent_abbr, is_home)."""
    m = _MATCHUP_RE.match((matchup or "").strip())
    if not m:
        return "", True
    left, sep, right = m.group(1), m.group(2), m.group(3)
    is_home = (sep.startswith("vs"))
    opponent = right if left == team_abbr else left
    return opponent, is_home


def _coerce_int(v) -> int:
    try:
        if v is None or pd.isna(v):
            return 0
        return int(v)
    except (TypeError, ValueError):
        return 0


def _coerce_float(v) -> float:
    try:
        if v is None or pd.isna(v):
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_season_logs(season: str, season_type: str = "Regular Season") -> pd.DataFrame:
    """Pull every player's game log for one season + season_type.

    season_type is the NBA Stats API value: 'Regular Season' or 'Playoffs'.
    Returns a DataFrame; empty if the API returned nothing.
    """
    print(f"[player_logs] Fetching {season} {season_type}...")
    for attempt in range(3):
        try:
            df = playergamelogs.PlayerGameLogs(
                season_nullable=season,
                league_id_nullable="00",
                season_type_nullable=season_type,
            ).get_data_frames()[0]
            print(f"[player_logs]   {len(df)} rows from API")
            return df
        except Exception as e:
            if attempt < 2:
                print(f"[player_logs]   retry {attempt + 1} after error: {e}")
                time.sleep(3 + attempt * 2)
            else:
                print(f"[player_logs]   gave up: {e}")
                return pd.DataFrame()


def transform(df: pd.DataFrame, season: str, season_type_label: str) -> list[dict]:
    """NBA Stats wide DataFrame → list of dicts ready for Supabase upsert."""
    if df.empty:
        return []

    rows: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    for _, r in df.iterrows():
        team_abbr = str(r.get("TEAM_ABBREVIATION", "")).upper()
        opp, is_home = _parse_matchup(str(r.get("MATCHUP", "")), team_abbr)

        gid     = str(r.get("GAME_ID", ""))
        pid     = _coerce_int(r.get("PLAYER_ID"))
        gdate   = str(r.get("GAME_DATE", ""))[:10]  # ISO date
        pts     = _coerce_int(r.get("PTS"))
        reb     = _coerce_int(r.get("REB"))
        ast     = _coerce_int(r.get("AST"))

        rows.append({
            "id":           _make_id(gid, pid),
            "game_id":      gid,
            "game_date":    gdate,
            "season":       season,
            "season_type":  season_type_label,
            "player_id":    pid,
            "player_name":  str(r.get("PLAYER_NAME", "")),
            "team_abbr":    team_abbr,
            "opponent_abbr": opp,
            "is_home":      bool(is_home),
            "minutes":      _coerce_float(r.get("MIN")),
            "pts":          pts,
            "reb":          reb,
            "ast":          ast,
            "fg3m":         _coerce_int(r.get("FG3M")),
            "blk":          _coerce_int(r.get("BLK")),
            "stl":          _coerce_int(r.get("STL")),
            "tov":          _coerce_int(r.get("TOV")),
            "pra":          pts + reb + ast,
            "plus_minus":   _coerce_int(r.get("PLUS_MINUS")),
            "updated_at":   now,
        })

    return rows


def run_backfill(seasons: Iterable[str] = (CURRENT_SEASON, PRIOR_SEASON)) -> None:
    """Pull regular season + playoffs for each season and upsert to Supabase."""
    print("[player_logs] Running backfill...")
    total = 0
    for season in seasons:
        for st_api, st_label in [("Regular Season", "regular"), ("Playoffs", "playoffs")]:
            df = fetch_season_logs(season, st_api)
            if df.empty:
                continue
            rows = transform(df, season, st_label)
            if rows:
                upsert("nba_player_logs", rows, on_conflict="id")
                total += len(rows)
                print(f"[player_logs]   ✓ upserted {len(rows)} rows for {season} {st_label}")
            time.sleep(1)  # gentle on stats.nba.com
    print(f"[player_logs] Backfill complete — {total} rows total.")


def run_incremental() -> None:
    """Refresh only the current season — call daily after games complete."""
    print("[player_logs] Running incremental sync (current season)...")
    for st_api, st_label in [("Regular Season", "regular"), ("Playoffs", "playoffs")]:
        df = fetch_season_logs(CURRENT_SEASON, st_api)
        if df.empty:
            continue
        rows = transform(df, CURRENT_SEASON, st_label)
        if rows:
            upsert("nba_player_logs", rows, on_conflict="id")
            print(f"[player_logs]   ✓ upserted {len(rows)} rows ({st_label})")
        time.sleep(1)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["backfill", "incremental"], default="backfill")
    args = p.parse_args()
    if args.mode == "backfill":
        run_backfill()
    else:
        run_incremental()

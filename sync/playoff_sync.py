"""
sync/playoff_sync.py — NBA playoff bracket + series fatigue.

Source: nba_api.stats.endpoints.commonplayoffseries.
Fatigue index = (total_games_played_in_series / 7) — a team coming off a
6-game series is at 0.86, a fresh team at the start of a round is 0.0.
"""
from __future__ import annotations
import hashlib
from datetime import datetime, timezone
import time

import pandas as pd
from nba_api.stats.endpoints import commonplayoffseries

from config import CURRENT_SEASON
from utils.db import upsert


def _make_id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


def fetch_bracket(season: str = CURRENT_SEASON) -> pd.DataFrame:
    try:
        return commonplayoffseries.CommonPlayoffSeries(
            season=season, league_id="00",
        ).get_data_frames()[0]
    except Exception as e:
        print(f"[playoff_series] fetch error: {e}")
        return pd.DataFrame()


def run_playoff_sync(season: str = CURRENT_SEASON) -> None:
    print("[playoff_series] Pulling bracket...")
    df = fetch_bracket(season)
    if df.empty:
        print("[playoff_series] No bracket data — likely off-season.")
        return

    # Each row = one game; aggregate by SERIES_ID
    out = []
    now = datetime.now(timezone.utc).isoformat()
    for series_id, sub in df.groupby("SERIES_ID"):
        # series_id format like "0042400101" — round digit is index 7 (0-based)
        round_digit = int(str(series_id)[7]) if len(str(series_id)) >= 8 else 1
        round_name  = {1: "First Round", 2: "Conf Semis",
                       3: "Conf Finals", 4: "Finals"}.get(round_digit, f"Round {round_digit}")
        sub_sorted = sub.sort_values("GAME_NUM")
        # Determine team1 / team2
        first = sub_sorted.iloc[0]
        team1_id = int(first["HOME_TEAM_ID"])
        team2_id = int(first["VISITOR_TEAM_ID"])
        team1_wins = team2_wins = 0
        for _, g in sub_sorted.iterrows():
            video_status = str(g.get("VIDEO_AVAILABLE_FLAG", "0"))
            # Heuristic: GAME_STATUS_TEXT often indicates Final
            home_pts = int(g.get("HOME_TEAM_PTS") or 0)
            away_pts = int(g.get("VISITOR_TEAM_PTS") or 0)
            if home_pts == 0 and away_pts == 0:
                continue
            if home_pts > away_pts:
                # Home team won this game; need to know which is team1
                if int(g["HOME_TEAM_ID"]) == team1_id:
                    team1_wins += 1
                else:
                    team2_wins += 1
            elif away_pts > home_pts:
                if int(g["VISITOR_TEAM_ID"]) == team1_id:
                    team1_wins += 1
                else:
                    team2_wins += 1

        games_played = team1_wins + team2_wins
        is_complete = team1_wins == 4 or team2_wins == 4
        is_elimination = (max(team1_wins, team2_wins) == 3) and not is_complete
        is_game7 = (team1_wins == 3 and team2_wins == 3)

        # Fatigue normalized to 0.0 (fresh) → 1.0 (just played 7-game war)
        fatigue1 = round(games_played / 7.0, 3)
        fatigue2 = fatigue1

        # Map team_ids → abbrs (need a lookup; for v1 store ids)
        out.append({
            "id":              _make_id(season, round_digit, str(series_id)),
            "season":          season,
            "round_number":    round_digit,
            "round_name":      round_name,
            "series_letter":   str(series_id),
            "team1_abbr":      str(team1_id),         # TODO: resolve to abbr
            "team2_abbr":      str(team2_id),
            "team1_wins":      team1_wins,
            "team2_wins":      team2_wins,
            "games_played":    games_played,
            "is_complete":     is_complete,
            "is_elimination":  is_elimination,
            "is_game7":        is_game7,
            "series_fatigue_team1": fatigue1,
            "series_fatigue_team2": fatigue2,
            "updated_at":      now,
        })

    if out:
        upsert("nba_playoff_series", out, on_conflict="id")
    print(f"[playoff_series] ✓ {len(out)} series rows.")


if __name__ == "__main__":
    run_playoff_sync()

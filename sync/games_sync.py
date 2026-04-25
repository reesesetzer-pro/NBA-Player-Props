"""sync/games_sync.py — Today's NBA schedule, with rest-day + B2B flags."""
from __future__ import annotations
from datetime import datetime, timezone, date
from typing import Optional
import time

import pandas as pd
from nba_api.stats.endpoints import scoreboardv2, leaguegamelog

from config import CURRENT_SEASON, NBA_TEAMS
from utils.db import upsert, fetch


def _last_game_date(team_logs: pd.DataFrame, team_abbr: str, before: date) -> Optional[date]:
    sub = team_logs[(team_logs["TEAM_ABBREVIATION"] == team_abbr) & (pd.to_datetime(team_logs["GAME_DATE"]).dt.date < before)]
    if sub.empty:
        return None
    return pd.to_datetime(sub["GAME_DATE"]).dt.date.max()


def fetch_team_schedule(season: str = CURRENT_SEASON) -> pd.DataFrame:
    """Pull every team's regular + playoff games for rest-day calc."""
    frames = []
    for st in ("Regular Season", "Playoffs"):
        try:
            df = leaguegamelog.LeagueGameLog(
                season=season, season_type_all_star=st, league_id="00",
            ).get_data_frames()[0]
            df["SEASON_TYPE"] = st
            frames.append(df)
        except Exception as e:
            print(f"[games] schedule fetch error ({st}): {e}")
        time.sleep(1)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_today_games(today: Optional[date] = None) -> list[dict]:
    today = today or date.today()
    sb = scoreboardv2.ScoreboardV2(game_date=today.isoformat()).get_data_frames()[0]
    if sb.empty:
        return []
    return sb.to_dict(orient="records")


def _team_abbr_from_id(team_id: int, ref: dict) -> str:
    return ref.get(int(team_id), "")


def run_games_sync(target_date: Optional[date] = None) -> None:
    """Build nba_games rows for today (or target_date) with rest_days + B2B."""
    target_date = target_date or date.today()
    print(f"[games] Building schedule for {target_date.isoformat()}...")

    # 1. Today's games
    today_rows = fetch_today_games(target_date)
    if not today_rows:
        print("[games] No games today.")
        return

    # 2. Pull every team's recent logs (for rest calc)
    schedule = fetch_team_schedule()
    if schedule.empty:
        print("[games] WARNING: schedule empty, rest days will be null.")

    # Map team_id → abbr from team-game rows we just got
    id_to_abbr: dict[int, str] = {}
    if not schedule.empty:
        for tid, sub in schedule.groupby("TEAM_ID"):
            id_to_abbr[int(tid)] = str(sub.iloc[0]["TEAM_ABBREVIATION"])

    rows = []
    now = datetime.now(timezone.utc).isoformat()
    for g in today_rows:
        gid       = str(g.get("GAME_ID", ""))
        home_id   = int(g.get("HOME_TEAM_ID", 0) or 0)
        away_id   = int(g.get("VISITOR_TEAM_ID", 0) or 0)
        home_abbr = id_to_abbr.get(home_id, "")
        away_abbr = id_to_abbr.get(away_id, "")

        h_last = _last_game_date(schedule, home_abbr, target_date) if home_abbr else None
        a_last = _last_game_date(schedule, away_abbr, target_date) if away_abbr else None
        rd_h = (target_date - h_last).days - 1 if h_last else None
        rd_a = (target_date - a_last).days - 1 if a_last else None

        # Determine playoff vs regular based on game_id prefix (00 = regular, 004 = playoffs)
        season_type = "playoffs" if gid.startswith("004") else "regular"

        rows.append({
            "id":             gid,
            "game_date":      target_date.isoformat(),
            "season":         CURRENT_SEASON,
            "season_type":    season_type,
            "home_abbr":      home_abbr,
            "away_abbr":      away_abbr,
            "home_team":      NBA_TEAMS.get(home_abbr, ""),
            "away_team":      NBA_TEAMS.get(away_abbr, ""),
            "commence_time":  None,                       # populated by odds_sync
            "odds_event_id":  None,
            "game_state":     "scheduled",
            "is_b2b_home":    (rd_h == 0) if rd_h is not None else None,
            "is_b2b_away":    (rd_a == 0) if rd_a is not None else None,
            "rest_days_home": rd_h,
            "rest_days_away": rd_a,
            "updated_at":     now,
        })

    upsert("nba_games", rows, on_conflict="id")
    print(f"[games] ✓ {len(rows)} games synced for {target_date.isoformat()}")


if __name__ == "__main__":
    run_games_sync()

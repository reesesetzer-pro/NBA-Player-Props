"""
sync/lineups_sync.py — Expected starting lineups + projected minutes.

V1: derive from last-N-games starter status (NBA Stats `boxscoresummaryv2`)
plus minutes averages. V2 should layer in Rotowire / Daily Faceoff scrapes
for confirmed lineups posted ~3 hrs pregame.
"""
from __future__ import annotations
import hashlib
import time
from datetime import datetime, timezone, date

import pandas as pd
from nba_api.stats.endpoints import commonteamroster

from config import CURRENT_SEASON
from utils.db import upsert, fetch


def _make_id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


# Hard-coded NBA team_id → abbr map (constant; avoids extra API call)
_NBA_TEAM_IDS = {
    1610612737: "ATL", 1610612738: "BOS", 1610612751: "BKN", 1610612766: "CHA",
    1610612741: "CHI", 1610612739: "CLE", 1610612742: "DAL", 1610612743: "DEN",
    1610612765: "DET", 1610612744: "GSW", 1610612745: "HOU", 1610612754: "IND",
    1610612746: "LAC", 1610612747: "LAL", 1610612763: "MEM", 1610612748: "MIA",
    1610612749: "MIL", 1610612750: "MIN", 1610612740: "NOP", 1610612752: "NYK",
    1610612760: "OKC", 1610612753: "ORL", 1610612755: "PHI", 1610612756: "PHX",
    1610612757: "POR", 1610612758: "SAC", 1610612759: "SAS", 1610612761: "TOR",
    1610612762: "UTA", 1610612764: "WAS",
}
_ABBR_TO_ID = {v: k for k, v in _NBA_TEAM_IDS.items()}


def _project_role(mpg: float) -> str:
    if mpg >= 25:
        return "starter"
    if mpg >= 12:
        return "rotation"
    return "bench"


def run_lineups_sync(target_date: date | None = None) -> None:
    """For each team playing today, derive expected lineup from rolling minutes."""
    target_date = target_date or date.today()
    print(f"[lineups] Building projected lineups for {target_date.isoformat()}...")

    games = fetch("nba_games", filters={"game_date": target_date.isoformat()})
    if games.empty:
        print("[lineups] No games today.")
        return

    teams_today = set()
    for _, g in games.iterrows():
        teams_today.add(g["home_abbr"])
        teams_today.add(g["away_abbr"])

    # Pull recent player logs for these teams
    logs = fetch("nba_player_logs", filters={"season": CURRENT_SEASON, "season_type": "playoffs"})
    if logs.empty:
        logs = fetch("nba_player_logs", filters={"season": CURRENT_SEASON, "season_type": "regular"})
    if logs.empty:
        print("[lineups] No logs to derive lineups from.")
        return

    logs["game_date"] = pd.to_datetime(logs["game_date"], errors="coerce")
    cutoff = pd.Timestamp(target_date) - pd.Timedelta(days=14)
    recent = logs[logs["game_date"] >= cutoff]

    rows = []
    now = datetime.now(timezone.utc).isoformat()
    for team_abbr in teams_today:
        team_recent = recent[recent["team_abbr"] == team_abbr]
        if team_recent.empty:
            continue
        # Average minutes per player over last 14d
        agg = (team_recent.groupby(["player_id", "player_name"])
               .agg(mpg=("minutes", "mean"))
               .reset_index()
               .sort_values("mpg", ascending=False))
        for _, p in agg.iterrows():
            rows.append({
                "id":            _make_id(team_abbr, target_date.isoformat(), int(p["player_id"])),
                "team_abbr":     team_abbr,
                "game_date":     target_date.isoformat(),
                "player_id":     int(p["player_id"]),
                "player_name":   p["player_name"],
                "role":          _project_role(p["mpg"]),
                "projected_min": round(float(p["mpg"]), 1),
                "confirmed":     False,
                "source":        "rolling_14d",
                "updated_at":    now,
            })

    if rows:
        upsert("nba_lineups", rows, on_conflict="id")
    print(f"[lineups] ✓ {len(rows)} player-slot rows for {len(teams_today)} teams.")


if __name__ == "__main__":
    run_lineups_sync()

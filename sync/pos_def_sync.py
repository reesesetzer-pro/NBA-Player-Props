"""
sync/pos_def_sync.py — Aggregate player_logs to compute "this defense allows
X pts to PGs" splits. Output feeds the matchup multiplier in adjustments.py.

Approach:
  1. Pull all player logs from current season.
  2. Pull each player's listed position from commonplayerinfo (cached).
  3. For each (defending_team, opp_position, stat) combo, average per-game
     stat from all opp games against that team.
  4. Compute league avg per position.
  5. multiplier = team_allowed / league_avg → matchup factor.
"""
from __future__ import annotations
import hashlib
from datetime import datetime, timezone

import pandas as pd

from config import CURRENT_SEASON, POSITION_GROUPS
from utils.db import upsert, fetch_all
from utils.positions import get_position as _get_position


def _make_id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


def run_pos_def_sync(season: str = CURRENT_SEASON, season_type: str = "regular") -> None:
    """Compute & upsert position-vs-defense splits."""
    print(f"[pos_def] Computing splits for {season} ({season_type})...")
    logs = fetch_all("nba_player_logs", filters={"season": season, "season_type": season_type})
    if logs.empty:
        print("[pos_def] No logs — run player_logs_sync first.")
        return

    # 1. Get a position for each player (only need active players to limit API calls)
    active_player_ids = logs["player_id"].dropna().astype(int).unique().tolist()
    print(f"[pos_def] Looking up positions for {len(active_player_ids)} players (one-time)...")
    pos_map: dict[int, str] = {}
    for i, pid in enumerate(active_player_ids):
        pos_map[pid] = _get_position(pid)
        if (i + 1) % 50 == 0:
            print(f"[pos_def]   {i+1}/{len(active_player_ids)} positions resolved")
    logs["position"] = logs["player_id"].map(pos_map).fillna("SF")

    # 2. For each (defending team, opp_position, stat), compute the average
    rows = []
    now = datetime.now(timezone.utc).isoformat()
    stats = ["pts", "reb", "ast", "fg3m", "pra"]

    # League averages per position per stat (denominator)
    league_avgs: dict[tuple[str, str], float] = {}
    for stat in stats:
        for pos in POSITION_GROUPS:
            sub = logs[logs["position"] == pos]
            if not sub.empty:
                league_avgs[(stat, pos)] = float(sub[stat].mean())

    # Per defending team
    for team_abbr, team_logs in logs.groupby("opponent_abbr"):
        for stat in stats:
            for pos in POSITION_GROUPS:
                pos_logs = team_logs[team_logs["position"] == pos]
                if pos_logs.empty:
                    continue
                avg_allowed = float(pos_logs[stat].mean())
                lg_avg = league_avgs.get((stat, pos), 0.0)
                multiplier = (avg_allowed / lg_avg) if lg_avg else 1.0
                rows.append({
                    "id":           _make_id(team_abbr, pos, stat, season),
                    "team_abbr":    team_abbr,
                    "opp_position": pos,
                    "stat":         stat,
                    "season":       season,
                    "season_type":  season_type,
                    "avg_allowed":  round(avg_allowed, 3),
                    "games_n":      int(len(pos_logs)),
                    "league_avg":   round(lg_avg, 3),
                    "rank":         None,                  # filled below
                    "multiplier":   round(multiplier, 4),
                    "updated_at":   now,
                })

    # Compute ranks per (stat, pos) — 1 = best defense (lowest allowed)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["rank"] = (df.groupby(["stat", "opp_position"])["avg_allowed"]
                        .rank(method="min", ascending=True).astype(int))
        rows = df.to_dict(orient="records")
        upsert("nba_pos_def", rows, on_conflict="id")
    print(f"[pos_def] ✓ {len(rows)} position-vs-defense rows written.")


if __name__ == "__main__":
    run_pos_def_sync()

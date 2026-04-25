"""
sync/splits_sync.py — Player home/road, B2B, days-rest splits.

Uses player_logs as the source of truth (already in DB) — no extra API calls.
Conditional distributions matter: LeBron home + 2-day rest is a different
beast than LeBron road on B2B.
"""
from __future__ import annotations
import hashlib
from datetime import datetime, timezone

import pandas as pd

from config import CURRENT_SEASON
from utils.db import upsert, fetch_all


def _make_id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


def _split_row(player_id, player_name, season, season_type,
               split_type, split_value, sub: pd.DataFrame, now: str) -> dict:
    return {
        "id":           _make_id(player_id, split_type, split_value, season),
        "player_id":    int(player_id),
        "player_name":  player_name,
        "season":       season,
        "season_type":  season_type,
        "split_type":   split_type,
        "split_value":  str(split_value),
        "games_n":      int(len(sub)),
        "pts_avg":      round(float(sub["pts"].mean() or 0), 2),
        "reb_avg":      round(float(sub["reb"].mean() or 0), 2),
        "ast_avg":      round(float(sub["ast"].mean() or 0), 2),
        "minutes_avg":  round(float(sub["minutes"].mean() or 0), 2),
        "updated_at":   now,
    }


def run_splits_sync(season: str = CURRENT_SEASON, season_type: str = "regular") -> None:
    print(f"[splits] Computing player splits for {season} ({season_type})...")
    logs = fetch_all("nba_player_logs", filters={"season": season, "season_type": season_type})
    if logs.empty:
        print("[splits] No logs.")
        return

    logs["game_date"] = pd.to_datetime(logs["game_date"], errors="coerce")
    logs = logs.sort_values(["player_id", "game_date"])
    # Compute days_rest per row (within player)
    logs["prev_date"] = logs.groupby("player_id")["game_date"].shift(1)
    logs["days_rest"] = (logs["game_date"] - logs["prev_date"]).dt.days - 1
    logs["days_rest"] = logs["days_rest"].fillna(-1).astype(int)

    rows = []
    now = datetime.now(timezone.utc).isoformat()

    for (pid, name), sub in logs.groupby(["player_id", "player_name"]):
        if pd.isna(pid) or len(sub) < 5:
            continue

        # home / road
        rows.append(_split_row(pid, name, season, season_type,
                               "home_road", "home", sub[sub["is_home"] == True], now))
        rows.append(_split_row(pid, name, season, season_type,
                               "home_road", "road", sub[sub["is_home"] == False], now))

        # rest buckets
        for r in [0, 1, 2, 3]:
            rs = sub[sub["days_rest"] == r]
            if not rs.empty:
                label = "b2b" if r == 0 else f"{r}d_rest"
                rows.append(_split_row(pid, name, season, season_type, "rest_days", label, rs, now))
        rs4 = sub[sub["days_rest"] >= 4]
        if not rs4.empty:
            rows.append(_split_row(pid, name, season, season_type, "rest_days", "4d_plus", rs4, now))

        # vs each opponent (only emit if 3+ games)
        for opp, opp_sub in sub.groupby("opponent_abbr"):
            if len(opp_sub) >= 3:
                rows.append(_split_row(pid, name, season, season_type,
                                       "vs_team", f"vs_{opp}", opp_sub, now))

    if rows:
        upsert("nba_player_splits", rows, on_conflict="id")
    print(f"[splits] ✓ {len(rows)} split rows written.")


if __name__ == "__main__":
    run_splits_sync()

"""
sync/defender_sync.py — Per-player defensive matchup data via leaguedashptdefend.

This is the secret weapon: when SGA is being guarded by Lu Dort, that's a
fundamentally different prop universe than when Cody Martin guards him.
"""
from __future__ import annotations
import hashlib
import time
from datetime import datetime, timezone

from nba_api.stats.endpoints import leaguedashptdefend

from config import CURRENT_SEASON
from utils.db import upsert


def _make_id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


def fetch_defender_data(season_type: str) -> list[dict]:
    """Overall defensive impact per player (vs all positions)."""
    try:
        df = leaguedashptdefend.LeagueDashPtDefend(
            season=CURRENT_SEASON,
            season_type_all_star=season_type,
            defense_category="Overall",
            per_mode_simple="PerGame",
        ).get_data_frames()[0]
        return df.to_dict(orient="records")
    except Exception as e:
        print(f"[defender] fetch error ({season_type}): {e}")
        return []


def transform(rows: list[dict], season_type_label: str) -> list[dict]:
    out = []
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        pid = int(r.get("CLOSE_DEF_PERSON_ID") or 0)
        out.append({
            "id":             _make_id(pid, CURRENT_SEASON, season_type_label),
            "player_id":      pid,
            "player_name":    str(r.get("PLAYER_NAME", "")),
            "team_abbr":      str(r.get("PLAYER_LAST_TEAM_ABBREVIATION", "")),
            "position":       str(r.get("PLAYER_POSITION", "")),
            "season":         CURRENT_SEASON,
            "season_type":    season_type_label,
            "matchup_min":    float(r.get("MATCHUP_MIN") or 0),
            "pts_allowed_per_chance": float(r.get("PTS") or 0),       # pts allowed when guarding
            "fg_pct_allowed": float(r.get("D_FG_PCT") or 0),
            "fg3_pct_allowed": float(r.get("D_FG3_PCT") or 0),
            "plus_minus":     float(r.get("PLUSMINUS") or 0),
            "updated_at":     now,
        })
    return out


def run_defender_sync() -> None:
    print("[defender] Pulling per-player defense (regular + playoffs)...")
    total = 0
    for st_api, st_label in [("Regular Season", "regular"), ("Playoffs", "playoffs")]:
        raw = fetch_defender_data(st_api)
        rows = transform(raw, st_label)
        if rows:
            upsert("nba_defender_stats", rows, on_conflict="id")
            total += len(rows)
            print(f"[defender]   ✓ {st_label}: {len(rows)} rows")
        time.sleep(1)
    print(f"[defender] Done — {total} rows.")


if __name__ == "__main__":
    run_defender_sync()

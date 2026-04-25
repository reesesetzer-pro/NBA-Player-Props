"""
sync/tracking_sync.py — Tier 3 player tracking metrics.

NBA tracking data covers shot zones, drives, catch-and-shoot vs pull-up,
contested vs uncontested rebounds, etc. All free from stats.nba.com.
For v1 we sync the high-signal subset: shot_zone splits + drives.
"""
from __future__ import annotations
import hashlib
import time
from datetime import datetime, timezone

from nba_api.stats.endpoints import (
    leaguedashplayerptshot,
    leaguedashptstats,
)

from config import CURRENT_SEASON
from utils.db import upsert


def _make_id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


def _store_metric(rows: list[dict], metric_name: str, value_col: str, season_type_label: str) -> list[dict]:
    """Convert a wide leaguedash response into our long (player, metric, value) format."""
    out = []
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        try:
            v = float(r.get(value_col) or 0)
        except (TypeError, ValueError):
            continue
        pid = int(r.get("PLAYER_ID") or 0)
        out.append({
            "id":          _make_id(pid, CURRENT_SEASON, metric_name),
            "player_id":   pid,
            "player_name": str(r.get("PLAYER_NAME", "")),
            "team_abbr":   str(r.get("TEAM_ABBREVIATION", "")),
            "season":      CURRENT_SEASON,
            "season_type": season_type_label,
            "metric":      metric_name,
            "value":       v,
            "rank":        None,
            "updated_at":  now,
        })
    return out


def run_tracking_sync() -> None:
    print("[tracking] Pulling shot zone + drive data...")
    total = 0
    for st_api, st_label in [("Regular Season", "regular"), ("Playoffs", "playoffs")]:
        # Shot dashboard — distance + zone
        try:
            df = leaguedashplayerptshot.LeagueDashPlayerPtShot(
                season=CURRENT_SEASON,
                season_type_all_star=st_api,
            ).get_data_frames()[0]
            raw = df.to_dict(orient="records")
            for col, label in [("FGA_FREQUENCY", "shot_frequency"),
                               ("EFG_PCT", "efg_pct_overall")]:
                rows = _store_metric(raw, label, col, st_label)
                if rows:
                    upsert("nba_player_tracking", rows, on_conflict="id")
                    total += len(rows)
        except Exception as e:
            print(f"[tracking]   shot dashboard error ({st_api}): {e}")
        time.sleep(1)

        # Drives
        try:
            df = leaguedashptstats.LeagueDashPtStats(
                season=CURRENT_SEASON,
                season_type_all_star=st_api,
                pt_measure_type="Drives",
                player_or_team="Player",
            ).get_data_frames()[0]
            raw = df.to_dict(orient="records")
            for col, label in [("DRIVES", "drives_per_game"),
                               ("DRIVE_PTS", "drive_pts"),
                               ("DRIVE_PASSES_PCT", "drive_pass_pct")]:
                rows = _store_metric(raw, label, col, st_label)
                if rows:
                    upsert("nba_player_tracking", rows, on_conflict="id")
                    total += len(rows)
        except Exception as e:
            print(f"[tracking]   drives error ({st_api}): {e}")
        time.sleep(1)

    print(f"[tracking] Done — {total} rows total.")


if __name__ == "__main__":
    run_tracking_sync()

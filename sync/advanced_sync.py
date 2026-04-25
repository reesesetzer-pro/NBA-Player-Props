"""sync/advanced_sync.py — Advanced per-game stats (USG%, TS%, etc)."""
from __future__ import annotations
import hashlib
import time
from datetime import datetime, timezone

from nba_api.stats.endpoints import playergamelogs

from config import CURRENT_SEASON, PRIOR_SEASON
from utils.db import upsert


def _make_id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


def fetch_advanced(season: str, season_type: str):
    return playergamelogs.PlayerGameLogs(
        season_nullable=season,
        season_type_nullable=season_type,
        league_id_nullable="00",
        measure_type_player_game_logs_nullable="Advanced",
    ).get_data_frames()[0].to_dict(orient="records")


def transform(rows, season: str, season_type_label: str) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    out = []
    for r in rows:
        gid = str(r.get("GAME_ID", ""))
        pid = int(r.get("PLAYER_ID") or 0)
        out.append({
            "id":               _make_id(gid, pid),
            "game_id":          gid,
            "game_date":        str(r.get("GAME_DATE", ""))[:10],
            "season":           season,
            "season_type":      season_type_label,
            "player_id":        pid,
            "player_name":      str(r.get("PLAYER_NAME", "")),
            "team_abbr":        str(r.get("TEAM_ABBREVIATION", "")),
            "minutes":          float(r.get("MIN") or 0),
            "usage_pct":        float(r.get("USG_PCT") or 0),
            "true_shooting_pct": float(r.get("TS_PCT") or 0),
            "eff_fg_pct":       float(r.get("EFG_PCT") or 0),
            "ast_pct":          float(r.get("AST_PCT") or 0),
            "reb_pct":          float(r.get("REB_PCT") or 0),
            "off_rating":       float(r.get("OFF_RATING") or 0),
            "def_rating":       float(r.get("DEF_RATING") or 0),
            "pace":             float(r.get("PACE") or 0),
            "updated_at":       now,
        })
    return out


def run_advanced_sync(seasons=(CURRENT_SEASON, PRIOR_SEASON)) -> None:
    print("[advanced] Pulling advanced per-game logs...")
    total = 0
    for season in seasons:
        for st_api, st_label in [("Regular Season", "regular"), ("Playoffs", "playoffs")]:
            try:
                raw = fetch_advanced(season, st_api)
                rows = transform(raw, season, st_label)
                if rows:
                    upsert("nba_player_advanced", rows, on_conflict="id")
                    total += len(rows)
                    print(f"[advanced]   ✓ {season}/{st_label}: {len(rows)} rows")
            except Exception as e:
                print(f"[advanced]   {season}/{st_label} error: {e}")
            time.sleep(1)
    print(f"[advanced] Done — {total} rows.")


if __name__ == "__main__":
    run_advanced_sync()

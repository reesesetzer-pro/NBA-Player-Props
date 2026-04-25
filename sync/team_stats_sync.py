"""sync/team_stats_sync.py — Team-level pace, ratings, opponent stats."""
from __future__ import annotations
import hashlib
import time
from datetime import datetime, timezone

from nba_api.stats.endpoints import leaguedashteamstats

from config import CURRENT_SEASON
from utils.db import upsert


def _make_id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


def fetch_measure(measure_type: str, season_type: str) -> list[dict]:
    """Pull leaguedashteamstats for a measure type + season type."""
    df = leaguedashteamstats.LeagueDashTeamStats(
        season=CURRENT_SEASON,
        season_type_all_star=season_type,
        measure_type_detailed_defense=measure_type,
        per_mode_detailed="Per100Possessions",
    ).get_data_frames()[0]
    return df.to_dict(orient="records")


def transform(rows: list[dict], measure_type: str, season_type_label: str) -> list[dict]:
    out = []
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        team_abbr = str(r.get("TEAM_ABBREVIATION", "")).upper()
        out.append({
            "id":             _make_id(team_abbr, CURRENT_SEASON, season_type_label, measure_type),
            "team_abbr":      team_abbr,
            "season":         CURRENT_SEASON,
            "season_type":    season_type_label,
            "measure_type":   measure_type,
            "games_played":   int(r.get("GP") or 0),
            "pace":           float(r.get("PACE") or 0),
            "off_rating":     float(r.get("OFF_RATING") or 0),
            "def_rating":     float(r.get("DEF_RATING") or 0),
            "net_rating":     float(r.get("NET_RATING") or 0),
            "opp_pts_per100": float(r.get("OPP_PTS_PER_100POSS") or r.get("DEF_RATING") or 0),
            "opp_efg_pct":    float(r.get("OPP_EFG_PCT") or 0),
            "opp_tov_pct":    float(r.get("OPP_TOV_PCT") or 0),
            "opp_oreb_pct":   float(r.get("OPP_OREB_PCT") or 0),
            "opp_ftr":        float(r.get("OPP_FTA_RATE") or 0),
            "updated_at":     now,
        })
    return out


def run_team_stats_sync() -> None:
    print("[team_stats] Pulling Base + Advanced + Defense (regular + playoffs)...")
    total = 0
    for st_api, st_label in [("Regular Season", "regular"), ("Playoffs", "playoffs")]:
        for measure in ["Base", "Advanced", "Defense"]:
            try:
                raw = fetch_measure(measure, st_api)
                rows = transform(raw, measure, st_label)
                if rows:
                    upsert("nba_team_stats", rows, on_conflict="id")
                    total += len(rows)
                    print(f"[team_stats]   ✓ {st_label}/{measure}: {len(rows)} rows")
            except Exception as e:
                print(f"[team_stats]   {st_label}/{measure} error: {e}")
            time.sleep(1)
    print(f"[team_stats] Done — {total} rows total.")


if __name__ == "__main__":
    run_team_stats_sync()

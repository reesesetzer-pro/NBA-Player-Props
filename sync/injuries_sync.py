"""
sync/injuries_sync.py — NBA injury report.

Source: ESPN's public JSON API. The HTML scraper this module used to rely on
broke when ESPN switched to client-rendered React (the static page no longer
contains injury rows), so we now hit
  https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries
which returns the same data structured by team.

Each injured row includes a `minutes_impact` estimate derived from the
player's recent minutes — fed into adjustments.injury_multiplier.
"""
from __future__ import annotations
import hashlib
from datetime import datetime, timezone

import requests
import pandas as pd

from config import CURRENT_SEASON
from utils.db import upsert, fetch
from utils.helpers import normalize_player_name


_HEADERS = {"User-Agent": "Mozilla/5.0"}
ESPN_API_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"


def _make_id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


_STATUS_MAP = {
    "out":          "out",
    "doubtful":     "doubtful",
    "questionable": "questionable",
    "probable":     "probable",
    "day-to-day":   "day-to-day",
    "dtd":          "day-to-day",
    "out for season": "out",
}


def _normalize_status(s: str) -> str:
    s = (s or "").strip().lower()
    for k, v in _STATUS_MAP.items():
        if k in s:
            return v
    return "questionable"


def scrape_espn() -> list[dict]:
    rows = []
    try:
        r = requests.get(ESPN_API_URL, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        payload = r.json()
        for team_block in payload.get("injuries", []):
            for inj in team_block.get("injuries", []):
                ath = inj.get("athlete") or {}
                player = ath.get("displayName") or ""
                if not player:
                    continue
                # Team abbr lives on the athlete, not on the team_block
                team_abbr = (ath.get("team") or {}).get("abbreviation") or ""
                status = inj.get("status") or ""
                details = inj.get("details") or {}
                comment = (
                    inj.get("shortComment")
                    or inj.get("longComment")
                    or details.get("type")
                    or ""
                )
                rows.append({
                    "player_name":  player,
                    "team_abbr":    team_abbr,
                    "status_raw":   status,
                    "comment":      comment,
                })
    except Exception as e:
        print(f"[injuries] ESPN API error: {e}")
    return rows


def _avg_minutes(logs: pd.DataFrame, player_name_norm: str) -> float:
    if logs.empty:
        return 0.0
    sub = logs[logs["player_name_norm"] == player_name_norm]
    if sub.empty:
        return 0.0
    sub = sub.sort_values("game_date").tail(15)
    return float(sub["minutes"].mean() or 0.0)


def run_injuries_sync() -> None:
    print("[injuries] Scraping ESPN NBA injuries...")
    raw = scrape_espn()
    if not raw:
        print("[injuries] No rows.")
        return

    # Pull recent logs to estimate each injured player's minutes_impact
    logs = fetch("nba_player_logs", filters={"season": CURRENT_SEASON, "season_type": "playoffs"})
    if logs.empty:
        logs = fetch("nba_player_logs", filters={"season": CURRENT_SEASON, "season_type": "regular"})
    if not logs.empty:
        logs["player_name_norm"] = logs["player_name"].apply(normalize_player_name)

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    out_n = dtd_n = q_n = 0
    for r in raw:
        pname_norm = normalize_player_name(r["player_name"])
        status = _normalize_status(r["status_raw"])
        impact = _avg_minutes(logs, pname_norm) if not logs.empty else 0.0
        if status == "out":     out_n += 1
        elif status == "day-to-day": dtd_n += 1
        else: q_n += 1
        rows.append({
            # Hash by player only — a player can change teams or have an
            # earlier sync miss the team_abbr; we want the latest status
            # to overwrite the prior row, not create a duplicate.
            "id":             _make_id(r["player_name"]),
            "player_id":      None,
            "player_name":    r["player_name"],
            "team_abbr":      r["team_abbr"],
            "status":         status,
            "notes":          r["comment"][:500],
            "minutes_impact": round(impact, 1),
            "updated_at":     now,
        })

    upsert("nba_injuries", rows, on_conflict="id")
    print(f"[injuries] ✓ {len(rows)} | 🚑 {out_n} OUT | ⚠️ {dtd_n} DTD | ❓ {q_n} Q/P")


if __name__ == "__main__":
    run_injuries_sync()

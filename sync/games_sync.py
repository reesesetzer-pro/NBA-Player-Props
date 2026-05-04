"""sync/games_sync.py — Today's NBA schedule, with rest-day + B2B flags."""
from __future__ import annotations
from datetime import datetime, timezone, date
from typing import Optional
import re
import time

import pandas as pd
import pytz
from nba_api.stats.endpoints import scoreboardv2, leaguegamelog

from config import CURRENT_SEASON, NBA_TEAMS
from utils.db import upsert, fetch

ET = pytz.timezone("America/New_York")
_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*(am|pm)\s*ET\s*$", re.I)


def _parse_status(status_text: str, status_id: int, game_date: date) -> tuple[Optional[str], str]:
    """Return (commence_time_iso_utc, game_state).

    NBA scoreboardv2's GAME_STATUS_TEXT is one of:
      * "1:00 pm ET" / "8:00 PM ET"  → upcoming, parse tipoff time
      * "Final" / "Final/OT"          → game finished
      * "Q3 4:30" / "Half"            → in progress
      * "PPD" / "Postponed"           → postponed
    GAME_STATUS_ID:  1=scheduled  2=in-progress  3=final
    """
    text = (status_text or "").strip()
    state = "scheduled"
    if status_id == 3 or text.lower().startswith("final"):
        state = "final"
    elif status_id == 2:
        state = "live"
    elif "ppd" in text.lower() or "postpon" in text.lower():
        state = "postponed"

    ct_iso = None
    m = _TIME_RE.match(text)
    if m and state == "scheduled":
        h, mi, ampm = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        if ampm == "PM" and h != 12: h += 12
        if ampm == "AM" and h == 12: h = 0
        try:
            et_dt = ET.localize(datetime(game_date.year, game_date.month, game_date.day, h, mi))
            ct_iso = et_dt.astimezone(timezone.utc).isoformat()
        except Exception:
            ct_iso = None
    return ct_iso, state


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


def fetch_today_games_espn(today: Optional[date] = None) -> list[dict]:
    """Fallback when NBA Stats API hasn't published the day's schedule yet.

    Round-2 schedule typically lags 12-24h after round 1 ends — round 1
    finished on 2026-05-03 (DET-ORL Game 7) and the NBA scoreboardv2
    endpoint had no games for 2026-05-04 even though ESPN already listed
    PHI@NY and MIN@SA. Fetch from ESPN and return scoreboardv2-shaped rows
    so the rest of the sync code is unchanged.
    """
    import requests
    today = today or date.today()
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            params={"dates": today.strftime("%Y%m%d")},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[games] ESPN fallback failed: {e}")
        return []

    # Map ESPN abbr → NBA Stats team_id (same table our pos_def cache uses).
    # ESPN uses 2-3 letter abbreviations; some differ from NBA Stats:
    #   ESPN "NY" → NBA "NYK", "SA" → "SAS", "GS" → "GSW", "NO" → "NOP",
    #   "UTAH" → "UTA". Normalize before lookup.
    from config import TEAM_ABBR_TO_ID
    _ESPN_TO_NBA = {"NY": "NYK", "SA": "SAS", "GS": "GSW", "NO": "NOP", "UTAH": "UTA"}
    rows = []
    for e in data.get("events", []):
        comps = e.get("competitions", []) or []
        if not comps:
            continue
        comp = comps[0]
        # ESPN's event id isn't the NBA stats game_id — synthesize one with
        # the playoff prefix so downstream `gid.startswith("004")` works.
        gid_raw = str(e.get("id", ""))
        gid = f"00420{gid_raw[-5:]}" if gid_raw else f"esp{today.strftime('%y%m%d')}{len(rows):02d}"

        home = away = None
        for c in comp.get("competitors", []) or []:
            t = c.get("team", {}) or {}
            abbr_espn = t.get("abbreviation", "")
            abbr      = _ESPN_TO_NBA.get(abbr_espn, abbr_espn)
            tid       = TEAM_ABBR_TO_ID.get(abbr, 0)
            if c.get("homeAway") == "home":
                home = (tid, abbr)
            else:
                away = (tid, abbr)
        if not home or not away:
            continue

        # Status text from ESPN is e.g. "Mon, May 4th at 8:00 PM EDT"
        status = comp.get("status", {}).get("type", {}) or {}
        rows.append({
            "GAME_ID":          gid,
            "HOME_TEAM_ID":     home[0],
            "VISITOR_TEAM_ID":  away[0],
            "GAME_STATUS_TEXT": status.get("shortDetail", "") or status.get("detail", ""),
            "GAME_STATUS_ID":   1,                # 1 = scheduled
            # Stash abbr directly so the abbr-lookup loop has a fallback path
            "_HOME_ABBR_ESPN":  home[1],
            "_AWAY_ABBR_ESPN":  away[1],
        })
    return rows


def _team_abbr_from_id(team_id: int, ref: dict) -> str:
    return ref.get(int(team_id), "")


def run_games_sync(target_date: Optional[date] = None) -> None:
    """Build nba_games rows for today (or target_date) with rest_days + B2B."""
    target_date = target_date or date.today()
    print(f"[games] Building schedule for {target_date.isoformat()}...")

    # 1. Today's games — try NBA Stats first, fall back to ESPN.
    # NBA Stats lags ~12-24h after a round ends before publishing the next
    # round's schedule. ESPN has it earlier.
    today_rows = fetch_today_games(target_date)
    if not today_rows:
        print("[games] NBA Stats has no games for today — trying ESPN fallback...")
        today_rows = fetch_today_games_espn(target_date)
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
        # Prefer the schedule-derived abbr; fall back to ESPN-stashed abbr
        # when this row came from the ESPN fallback path.
        home_abbr = id_to_abbr.get(home_id, "") or g.get("_HOME_ABBR_ESPN", "")
        away_abbr = id_to_abbr.get(away_id, "") or g.get("_AWAY_ABBR_ESPN", "")

        h_last = _last_game_date(schedule, home_abbr, target_date) if home_abbr else None
        a_last = _last_game_date(schedule, away_abbr, target_date) if away_abbr else None
        rd_h = (target_date - h_last).days - 1 if h_last else None
        rd_a = (target_date - a_last).days - 1 if a_last else None

        # Determine playoff vs regular based on game_id prefix (00 = regular, 004 = playoffs)
        season_type = "playoffs" if gid.startswith("004") else "regular"

        # Parse tip time + status from scoreboard's GAME_STATUS fields
        ct_iso, game_state = _parse_status(
            str(g.get("GAME_STATUS_TEXT", "")),
            int(g.get("GAME_STATUS_ID", 0) or 0),
            target_date,
        )

        rows.append({
            "id":             gid,
            "game_date":      target_date.isoformat(),
            "season":         CURRENT_SEASON,
            "season_type":    season_type,
            "home_abbr":      home_abbr,
            "away_abbr":      away_abbr,
            "home_team":      NBA_TEAMS.get(home_abbr, ""),
            "away_team":      NBA_TEAMS.get(away_abbr, ""),
            "commence_time":  ct_iso,                     # NBA-derived; odds_sync may override
            "odds_event_id":  None,
            "game_state":     game_state,
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

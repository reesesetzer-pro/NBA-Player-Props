"""
sync/odds_sync.py — DraftKings + FanDuel game lines + ALL alt prop ladders.

Two endpoints used:
  /sports/{sport}/odds              — bulk h2h/spreads/totals (1 credit)
  /sports/{sport}/events/{eid}/odds — per-event, supports prop markets (1 credit / market)
"""
from __future__ import annotations
import hashlib
from datetime import datetime, timezone
from typing import Optional
import time
import requests

from config import (
    ODDS_API_BASE, ODDS_API_KEY, NBA_SPORT_KEY, BOOKS,
    MARKETS_GAME, ALL_PROP_MARKETS, ODDS_FORMAT,
)
from utils.db import upsert, get_client
from utils.helpers import name_to_abbr, normalize_player_name


def _make_id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


def _get(url: str, params: dict, retries: int = 2) -> Optional[dict | list]:
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                print(f"[odds] rate limited, sleeping 60s")
                time.sleep(60)
                continue
            r.raise_for_status()
            remaining = r.headers.get("x-requests-remaining")
            if remaining:
                print(f"[odds]   credits remaining: {remaining}")
            return r.json()
        except requests.RequestException as e:
            if attempt == retries:
                print(f"[odds] failed: {e}")
                return None
            time.sleep(2 + attempt * 3)
    return None


def fetch_events() -> list[dict]:
    """List today's NBA events (event_ids needed for prop endpoints)."""
    url = f"{ODDS_API_BASE}/sports/{NBA_SPORT_KEY}/events"
    return _get(url, {"apiKey": ODDS_API_KEY}) or []


def fetch_game_odds() -> list[dict]:
    """Bulk h2h/spreads/totals across all events — cheaper than per-event."""
    url = f"{ODDS_API_BASE}/sports/{NBA_SPORT_KEY}/odds"
    return _get(url, {
        "apiKey":     ODDS_API_KEY,
        "bookmakers": ",".join(BOOKS),
        "markets":    ",".join(MARKETS_GAME),
        "oddsFormat": ODDS_FORMAT,
    }) or []


def fetch_event_props(event_id: str) -> dict:
    """Pull every prop market for one event. One API call per market.

    Returns the API's event-with-bookmakers dict (markets nested per book).
    """
    url = f"{ODDS_API_BASE}/sports/{NBA_SPORT_KEY}/events/{event_id}/odds"
    out = {"id": event_id, "bookmakers": []}
    seen_books: dict[str, dict] = {}            # book_key → bookmaker dict
    for market in ALL_PROP_MARKETS:
        data = _get(url, {
            "apiKey":     ODDS_API_KEY,
            "bookmakers": ",".join(BOOKS),
            "markets":    market,
            "oddsFormat": ODDS_FORMAT,
        })
        if not data or not isinstance(data, dict):
            continue
        out.setdefault("home_team", data.get("home_team", ""))
        out.setdefault("away_team", data.get("away_team", ""))
        for bm in data.get("bookmakers", []) or []:
            key = bm.get("key", "")
            if key not in BOOKS:
                continue
            if key not in seen_books:
                seen_books[key] = {"key": key, "title": bm.get("title", ""), "markets": []}
            seen_books[key]["markets"].extend(bm.get("markets", []) or [])
        time.sleep(0.4)                          # gentle on Odds API rate limits
    out["bookmakers"] = list(seen_books.values())
    return out


def _resolve_game_id(
    odds_event_id: str,
    home_team: str,
    away_team: str,
    commence_time: Optional[str] = None,
) -> Optional[str]:
    """Find the matching nba_games.id for an Odds API event.

    Strategy: match on (game_date == today, home_abbr, away_abbr). Also
    backfills odds_event_id and commence_time onto the game row so the
    dashboard can filter out games that have already tipped off.
    """
    from datetime import date as _d
    today = _d.today().isoformat()
    home_abbr = name_to_abbr(home_team)
    away_abbr = name_to_abbr(away_team)
    sb = get_client()
    resp = (sb.table("nba_games").select("id").eq("game_date", today)
            .eq("home_abbr", home_abbr).eq("away_abbr", away_abbr).execute())
    if resp.data:
        gid = resp.data[0]["id"]
        update = {"odds_event_id": odds_event_id}
        # Only stamp commence_time if it's within 24 hours of "now" — i.e.,
        # this Odds API event is genuinely tonight's game. Without this check
        # the Odds API can serve the NEXT game in a playoff series under the
        # same team-pair (different date entirely), and stamping that future
        # time would mark a finished game as still upcoming.
        if commence_time:
            try:
                ct = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
                hours_away = (ct - datetime.now(timezone.utc)).total_seconds() / 3600
                if -6 <= hours_away <= 24:
                    update["commence_time"] = commence_time
            except Exception:
                pass
        sb.table("nba_games").update(update).eq("id", gid).execute()
        return gid
    return None


def parse_game_odds(events: list[dict]) -> list[dict]:
    """Bulk h2h/spreads/totals → nba_odds rows."""
    rows = []
    now = datetime.now(timezone.utc).isoformat()
    for e in events:
        eid = e.get("id", "")
        gid = _resolve_game_id(eid, e.get("home_team", ""), e.get("away_team", ""),
                               commence_time=e.get("commence_time"))
        if not gid:
            continue
        for bm in e.get("bookmakers", []) or []:
            if bm.get("key") not in BOOKS:
                continue
            for mk in bm.get("markets", []) or []:
                for o in mk.get("outcomes", []) or []:
                    rows.append({
                        "id":            _make_id(gid, bm["key"], mk["key"], o.get("name"), o.get("point") or ""),
                        "game_id":       gid,
                        "odds_event_id": eid,
                        "book":          bm["key"],
                        "market":        mk["key"],
                        "outcome":       o.get("name"),
                        "price":         int(o.get("price", 0)),
                        "point":         o.get("point"),
                        "updated_at":    now,
                    })
    return rows


def parse_props(event: dict) -> tuple[list[dict], list[dict]]:
    """One event's prop bookmakers → (props_rows, history_rows)."""
    props_rows = []
    history_rows = []
    now = datetime.now(timezone.utc).isoformat()

    eid = event.get("id", "")
    gid = _resolve_game_id(eid, event.get("home_team", ""), event.get("away_team", ""))
    if not gid:
        return [], []

    for bm in event.get("bookmakers", []) or []:
        book = bm.get("key", "")
        if book not in BOOKS:
            continue
        for mk in bm.get("markets", []) or []:
            mkt_key = mk.get("key", "")
            for o in mk.get("outcomes", []) or []:
                # Props: name="Over"/"Under", description="Player Name"
                player = o.get("description") or ""
                ou     = o.get("name") or ""
                line   = o.get("point")
                price  = o.get("price")
                if not (player and ou in ("Over", "Under") and line is not None and price is not None):
                    continue
                player_norm = normalize_player_name(player)
                row_id = _make_id(gid, book, mkt_key, player_norm, line, ou)
                props_rows.append({
                    "id":               row_id,
                    "game_id":          gid,
                    "odds_event_id":    eid,
                    "book":             book,
                    "market":           mkt_key,
                    "player_name":      player,
                    "player_name_norm": player_norm,
                    "team_abbr":        "",                          # joined later via lineups
                    "line":             float(line),
                    "over_under":       ou,
                    "price":            int(price),
                    "updated_at":       now,
                })
                history_rows.append({
                    "game_id":          gid,
                    "book":             book,
                    "market":           mkt_key,
                    "player_name_norm": player_norm,
                    "line":             float(line),
                    "over_under":       ou,
                    "price":            int(price),
                    "snapshot_at":      now,
                })
    return props_rows, history_rows


def run_game_odds_sync() -> int:
    print("[odds] Game lines (h2h/spreads/totals)...")
    events = fetch_game_odds()
    rows = parse_game_odds(events)
    if rows:
        upsert("nba_odds", rows, on_conflict="id")
    print(f"[odds] ✓ {len(rows)} game-odds rows")
    return len(rows)


def run_props_sync() -> tuple[int, int]:
    """Pull props for every event today. Returns (props_count, history_count)."""
    print("[odds] Player props (DK + FD, all markets + alt ladders)...")
    events = fetch_events()
    if not events:
        print("[odds] No events today.")
        return 0, 0

    all_props = []
    all_history = []
    for e in events:
        eid = e.get("id", "")
        if not eid:
            continue
        full = fetch_event_props(eid)
        full["home_team"] = e.get("home_team", "")
        full["away_team"] = e.get("away_team", "")
        p, h = parse_props(full)
        all_props.extend(p)
        all_history.extend(h)

    if all_props:
        upsert("nba_props", all_props, on_conflict="id")
    if all_history:
        # History is append-only — use insert via upsert with bigserial PK
        get_client().table("nba_props_history").insert(all_history).execute()

    print(f"[odds] ✓ {len(all_props)} prop rows | {len(all_history)} history snapshots")
    return len(all_props), len(all_history)


if __name__ == "__main__":
    run_game_odds_sync()
    run_props_sync()

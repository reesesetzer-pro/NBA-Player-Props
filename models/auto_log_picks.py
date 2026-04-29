"""
models/auto_log_picks.py — Shadow-log every priced edge into nba_bets.

Each edge_engine run inserts each priced opportunity as a "shadow bet" in
nba_bets, marked with notes="[SHADOW] ..." so we can distinguish from the
user's actual placed bets. Grading + calibration scripts only look at SHADOW
rows, never at user-placed bets.

Using the existing nba_bets table avoids needing a new migration — the schema
fits our needs perfectly (player_name, market_base, line, over_under, book,
price, model_prob, edge_at_bet, result, profit_loss are all already there).
"""
from __future__ import annotations
import hashlib
import json
from datetime import date, datetime, timezone
import pandas as pd

from utils.db import get_client
from utils.helpers import normalize_player_name


SHADOW_MARKER = "[SHADOW]"


def _shadow_id(game_id: str, player_norm: str, market: str, line: float, ou: str, sync_date: str) -> str:
    """Stable hash so re-running on same day idempotently dedupes."""
    return hashlib.md5(
        f"{game_id}|{player_norm}|{market}|{line}|{ou}|{sync_date}".encode()
    ).hexdigest()


def shadow_log_edges(edges: list[dict], sync_date: date | None = None) -> int:
    """Insert today's edges as shadow bets in nba_bets. Returns rows written."""
    if not edges:
        return 0
    sync_date = sync_date or date.today()
    sd_str = sync_date.isoformat()

    client = get_client()
    # Pull existing shadow-bet IDs for today so we don't double-insert
    existing_resp = (client.table("nba_bets")
                     .select("notes")
                     .eq("game_date", sd_str)
                     .ilike("notes", f"%{SHADOW_MARKER}%")
                     .execute())
    existing_ids: set[str] = set()
    for r in existing_resp.data or []:
        notes = r.get("notes") or ""
        if "shadow_id=" in notes:
            sid = notes.split("shadow_id=", 1)[1].split()[0]
            existing_ids.add(sid)

    rows = []
    for e in edges:
        norm = normalize_player_name(e.get("player_name", ""))
        sid = _shadow_id(
            e.get("game_id", ""), norm, e.get("market_base", ""),
            e.get("line", ""), e.get("over_under", ""), sd_str,
        )
        if sid in existing_ids:
            continue
        # Encode metadata in notes so grading + calibration can recover it
        meta = {
            "shadow_id":   sid,
            "player_norm": norm,
            "is_alt":      bool(e.get("is_alt", False)),
            "novig":       e.get("market_prob_novig"),
        }
        notes = f"{SHADOW_MARKER} shadow_id={sid} meta={json.dumps(meta)}"
        rows.append({
            "placed_at":   datetime.now(timezone.utc).isoformat(),
            "game_id":     e.get("game_id"),
            "game_date":   e.get("game_date") or sd_str,
            "player_name": e.get("player_name"),
            "market_base": e.get("market_base"),
            "line":        e.get("line"),
            "over_under":  e.get("over_under"),
            "book":        e.get("best_book"),
            "price":       e.get("best_price"),
            "stake":       0,                            # shadow = no real stake
            "to_win":      0,
            "model_prob":  e.get("model_prob"),
            "edge_at_bet": e.get("edge"),
            "result":      "Pending",
            "profit_loss": 0,
            "notes":       notes,
        })

    if not rows:
        return 0
    # Insert in chunks
    written = 0
    for i in range(0, len(rows), 500):
        batch = rows[i:i+500]
        client.table("nba_bets").insert(batch).execute()
        written += len(batch)
    return written


def fetch_shadow_picks(only_pending: bool = True, settled_only: bool = False) -> pd.DataFrame:
    """Pull shadow picks from nba_bets. Helper for grading + calibration."""
    client = get_client()
    q = client.table("nba_bets").select("*").ilike("notes", f"%{SHADOW_MARKER}%")
    if only_pending:
        q = q.eq("result", "Pending")
    elif settled_only:
        q = q.in_("result", ["Win", "Loss", "Push"])
    return pd.DataFrame(q.execute().data or [])

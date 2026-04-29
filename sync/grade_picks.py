"""
sync/grade_picks.py — Grade pending shadow picks against actual results.

Reads SHADOW-tagged rows from nba_bets where result='Pending' and game_date
is past, looks up the player's actual stat in nba_player_logs, marks
Win/Loss/Push + profit_loss.
"""
from __future__ import annotations
from datetime import datetime, timezone, date
import pandas as pd

from utils.db import get_client, fetch_all
from utils.helpers import normalize_player_name
from models.auto_log_picks import fetch_shadow_picks


_MARKET_TO_LOG_COL = {
    "pts": "pts", "reb": "reb", "ast": "ast",
    "fg3m": "fg3m", "blk": "blk", "stl": "stl", "pra": "pra",
}


def _settle(over_under: str, line: float, actual: float) -> str:
    if pd.isna(actual):
        return "Pending"
    if line == int(line):
        if actual == line:
            return "Push"
    if over_under == "Over":
        return "Win" if actual > line else "Loss"
    return "Win" if actual < line else "Loss"


def _pnl(price, result: str) -> float:
    if result in ("Pending", "Push") or not price:
        return 0.0
    if result == "Loss":
        return -1.0
    p = float(price)
    return (p / 100.0) if p > 0 else (100.0 / abs(p))


def run_grading(verbose: bool = True) -> dict:
    today = date.today().isoformat()
    pending = fetch_shadow_picks(only_pending=True)
    if pending.empty:
        print("[grade] no pending shadow picks.")
        return {"graded": 0, "missed": 0}
    # Filter to picks for completed games
    pending = pending[pending["game_date"] < today]
    if pending.empty:
        print("[grade] no pending shadow picks for completed games.")
        return {"graded": 0, "missed": 0}

    print(f"[grade] {len(pending)} pending shadow picks for completed games — looking up stats")

    logs = fetch_all("nba_player_logs")
    if logs.empty:
        print("[grade] no player logs in DB.")
        return {"graded": 0, "missed": 0}
    logs["player_name_norm"] = logs["player_name"].apply(normalize_player_name)

    client = get_client()
    graded = 0
    misses = 0
    for _, p in pending.iterrows():
        # Try to recover normalized name from notes metadata, fall back
        notes = str(p.get("notes") or "")
        try:
            player_norm = (notes.split("player_norm\":", 1)[1]
                                .split(",", 1)[0].strip().strip('"'))
        except Exception:
            player_norm = normalize_player_name(p.get("player_name", ""))

        gid = str(p.get("game_id") or "")
        gdate = str(p.get("game_date") or "")
        sub = logs[
            (logs["player_name_norm"] == player_norm)
            & ((logs["game_id"].astype(str) == gid)
               | (logs["game_date"].astype(str) == gdate))
        ]
        if sub.empty:
            misses += 1
            continue

        col = _MARKET_TO_LOG_COL.get(p.get("market_base"))
        if not col:
            misses += 1
            continue

        actual = float(sub.iloc[0].get(col) or 0)
        result = _settle(p.get("over_under"), float(p.get("line") or 0), actual)
        if result == "Pending":
            continue
        pnl = _pnl(p.get("price"), result)

        client.table("nba_bets").update({
            "result":       result,
            "profit_loss":  round(pnl, 4),
            "notes":        f"{notes} actual={actual}",
        }).eq("id", int(p["id"])).execute()
        graded += 1

    print(f"[grade] graded {graded} | unmatched: {misses}")

    if verbose and graded:
        _print_summary()
    return {"graded": graded, "missed": misses}


def _print_summary():
    settled = fetch_shadow_picks(only_pending=False, settled_only=True)
    if settled.empty:
        return
    print("\n=== NBA Shadow-Pick Calibration ===")
    print(f"{'Market':10} {'N':>5} {'W-L-P':>10} {'Win%':>7} {'Avg pred':>9} {'$/$1':>7}")
    for mkt, g in settled.groupby("market_base"):
        n = len(g)
        w = (g["result"] == "Win").sum()
        l = (g["result"] == "Loss").sum()
        p = (g["result"] == "Push").sum()
        wr = w / (w + l) * 100 if (w + l) else 0
        ap = pd.to_numeric(g["model_prob"], errors="coerce").mean() * 100
        pnl = pd.to_numeric(g["profit_loss"], errors="coerce").sum()
        wagered = w + l
        roi = pnl / wagered * 100 if wagered else 0
        print(f"{mkt:10} {n:>5}  {int(w)}-{int(l)}-{int(p):<4} {wr:>6.1f}% {ap:>8.1f}% {roi:>+6.1f}%")


if __name__ == "__main__":
    run_grading()

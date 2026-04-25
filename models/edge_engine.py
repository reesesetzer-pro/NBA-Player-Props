"""
models/edge_engine.py — Price every available prop on every alt line, write
edges to Supabase.

Pipeline per game:
  1. Pull props (DK + FD) for the game from `nba_props`.
  2. For each unique (player, market_base), fetch the player's recent game
     logs and fit a NegBin distribution.
  3. Apply matchup, rest, playoff, injury multipliers to μ → adjusted_mu.
  4. For every (line, over_under) in the alt ladder, compute model_prob from
     the adjusted distribution.
  5. Compute no-vig market consensus across DK + FD (for that line).
  6. Edge = model_prob - market_prob_novig.
  7. Write to `nba_prop_edges` with full traceability (μ, α, multipliers).
"""
from __future__ import annotations
import hashlib
from datetime import datetime, timezone
from typing import Optional
import numpy as np
import pandas as pd

from config import CURRENT_SEASON, EDGE_SOFT_THRESHOLD, BOOKS
from models.distribution import fit_distribution, StatDistribution
from models.adjustments import compose
from models.kelly import kelly_dollars
from utils.db import get_client, fetch, fetch_in, fetch_all, upsert
from utils.helpers import american_to_implied, remove_vig, normalize_player_name


# Map Odds API market keys → our internal stat base + alt flag
MARKET_TO_STAT = {
    "player_points":                          ("pts",  False),
    "player_rebounds":                        ("reb",  False),
    "player_assists":                         ("ast",  False),
    "player_threes":                          ("fg3m", False),
    "player_blocks":                          ("blk",  False),
    "player_steals":                          ("stl",  False),
    "player_points_rebounds_assists":         ("pra",  False),
    "player_points_alternate":                ("pts",  True),
    "player_rebounds_alternate":              ("reb",  True),
    "player_assists_alternate":               ("ast",  True),
    "player_threes_alternate":                ("fg3m", True),
    "player_points_rebounds_assists_alternate": ("pra", True),
}


def _make_id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


def _player_recent_logs(logs_df: pd.DataFrame, player_name_norm: str, stat: str, n: int = 60) -> list[float]:
    """Return last `n` game values for the stat, oldest-first (so .tail() in
    fit_distribution catches the most-recent window)."""
    if logs_df.empty:
        return []
    sub = logs_df[logs_df["player_name_norm"] == player_name_norm].copy()
    if sub.empty:
        return []
    sub["game_date"] = pd.to_datetime(sub["game_date"], errors="coerce")
    sub = sub.sort_values("game_date").tail(n)
    return [float(v) for v in sub[stat].tolist() if pd.notna(v)]


def _no_vig_consensus(
    prices: list[int],
    opp_prices: list[int],
    single_side_vig: float = 0.05,
) -> Optional[float]:
    """Average implied probabilities across books and strip vig.

    If both sides exist (typical main markets), proper 2-way devig.
    If only one side exists (typical alt-ladder lines — books post Over-only
    ladders all the way up), strip an assumed `single_side_vig` (~5% is
    standard DK/FD alt-prop hold). This unlocks the alt-ladder edge hunt.
    """
    if not prices:
        return None
    p_imp = float(np.mean([american_to_implied(p) for p in prices]))
    if p_imp <= 0:
        return None
    if not opp_prices:
        return p_imp / (1.0 + single_side_vig)
    o_imp = float(np.mean([american_to_implied(p) for p in opp_prices]))
    if o_imp <= 0:
        return None
    novig, _ = remove_vig(p_imp, o_imp)
    return novig


def _player_meta(meta_df: pd.DataFrame, player_name_norm: str) -> dict:
    """Return position + minutes_per_game from the most recent player record."""
    if meta_df.empty:
        return {}
    sub = meta_df[meta_df["player_name_norm"] == player_name_norm]
    if sub.empty:
        return {}
    return {
        "player_id":        int(sub.iloc[0].get("player_id") or 0),
        "team_abbr":        str(sub.iloc[0].get("team_abbr") or ""),
        "position":         str(sub.iloc[0].get("position") or "SG"),
        "minutes_per_game": float(sub.iloc[0].get("minutes_per_game") or 0.0),
    }


def calculate_edges_for_game(
    game_row: dict,
    props_df: pd.DataFrame,
    logs_df: pd.DataFrame,
    meta_df: pd.DataFrame,
    pos_def_df: pd.DataFrame,
    injuries_df: pd.DataFrame,
    series_df: pd.DataFrame,
) -> list[dict]:
    """Compute edges for every prop in one game. Returns rows ready to upsert."""
    edges: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    game_id   = str(game_row["id"])
    game_date = str(game_row.get("game_date", ""))
    is_playoff = (str(game_row.get("season_type", "")).lower() == "playoffs")

    # Cache fitted distributions per (player, stat) — same player, multiple lines
    fit_cache: dict[tuple[str, str], Optional[StatDistribution]] = {}
    # Group by (player_name_norm, market_base) to find both Over/Under sides per line
    if props_df.empty:
        return edges

    # Reduce props to this game only
    g_props = props_df[props_df["game_id"] == game_id]
    if g_props.empty:
        return edges

    # Iterate per (player, market_base)
    for (player_norm, market), grp in g_props.groupby(["player_name_norm", "market"]):
        stat_info = MARKET_TO_STAT.get(market)
        if not stat_info:
            continue
        stat, is_alt = stat_info

        # Skip if we have no values for this stat in logs (e.g., we don't track BLK)
        if stat not in logs_df.columns:
            continue

        meta = _player_meta(meta_df, player_norm)
        team_abbr = meta.get("team_abbr") or grp.iloc[0].get("team_abbr") or ""
        opp_abbr  = (game_row["away_abbr"] if team_abbr == game_row["home_abbr"]
                     else game_row["home_abbr"])
        days_rest = (game_row.get("rest_days_home") if team_abbr == game_row["home_abbr"]
                     else game_row.get("rest_days_away"))

        cache_key = (player_norm, stat)
        if cache_key not in fit_cache:
            values = _player_recent_logs(logs_df, player_norm, stat)
            fit_cache[cache_key] = fit_distribution(values)
        dist = fit_cache[cache_key]
        if dist is None:
            continue

        # Apply adjustments — multipliers on μ
        adj = compose(
            pos_def_df, injuries_df,
            opponent_abbr=opp_abbr,
            player_position=meta.get("position", "SG"),
            stat=stat,
            days_rest=days_rest,
            is_playoff=is_playoff,
            minutes_per_game=meta.get("minutes_per_game"),
            series_fatigue=0.0,                      # TODO: pull from series_df
            team_abbr=team_abbr,
            player_id=meta.get("player_id", 0),
        )
        adjusted_dist = StatDistribution(
            mu=dist.mu * adj.combined,
            alpha=dist.alpha,
            n_games=dist.n_games,
            season_avg=dist.season_avg,
            recent_avg=dist.recent_avg,
        )

        # For each unique line in this market, find prices and compute edge.
        # Alt-ladder markets (is_alt=True) are typically Over-only — we still
        # price them by stripping an assumed 5% vig from the single side. Main
        # markets require both sides for proper 2-way devig.
        for line, line_grp in grp.groupby("line"):
            for ou in ("Over", "Under"):
                side = line_grp[line_grp["over_under"] == ou]
                opp  = line_grp[line_grp["over_under"] != ou]
                if side.empty:
                    continue
                if opp.empty and not is_alt:
                    # Main markets without both sides → likely stale/ungraded; skip
                    continue
                prices = [int(p) for p in side["price"].tolist() if pd.notna(p)]
                opp_p  = [int(p) for p in opp["price"].tolist() if pd.notna(p)]
                novig  = _no_vig_consensus(prices, opp_p)
                if novig is None:
                    continue

                # Best price for the bettor
                best_idx = side["price"].idxmax()
                best_price = int(side.loc[best_idx, "price"])
                best_book  = str(side.loc[best_idx, "book"])

                model_prob = (adjusted_dist.prob_over(float(line)) if ou == "Over"
                              else adjusted_dist.prob_under(float(line)))
                edge = model_prob - novig

                k_full, k_half, k_qtr = kelly_dollars(model_prob, best_price)

                edges.append({
                    "id":                  _make_id(game_id, player_norm, stat, line, ou),
                    "game_id":             game_id,
                    "game_date":           game_date,
                    "player_name":         str(side.iloc[0].get("player_name") or ""),
                    "player_name_norm":    player_norm,
                    "team_abbr":           team_abbr,
                    "market_base":         stat,
                    "line":                float(line),
                    "over_under":          ou,
                    "best_price":          best_price,
                    "best_book":           best_book,
                    "model_prob":          round(float(model_prob), 4),
                    "market_prob_novig":   round(float(novig), 4),
                    "edge":                round(float(edge), 4),
                    "kelly_full":          k_full,
                    "kelly_half":          k_half,
                    "kelly_quarter":       k_qtr,
                    "fitted_mu":           round(adjusted_dist.mu, 3),
                    "fitted_alpha":        round(adjusted_dist.alpha, 4),
                    "matchup_mult":        round(adj.matchup, 4),
                    "rest_mult":           round(adj.rest, 4),
                    "playoff_mult":        round(adj.playoff, 4),
                    "injury_mult":         round(adj.injury, 4),
                    "is_alt":              is_alt,
                    "updated_at":          now,
                })

    return edges


def calculate_all_edges() -> int:
    """Compute edges for every game on today's slate. Returns count written."""
    from datetime import date as _d
    today = _d.today().isoformat()

    games = fetch("nba_games", filters={"game_date": today})
    if games.empty:
        print("[edge] No games for today.")
        return 0

    target_ids = games["id"].tolist()
    props = fetch_in("nba_props", "game_id", target_ids)
    if props.empty:
        print("[edge] No props for today's games — run odds_sync first.")
        return 0
    props["player_name_norm"] = props["player_name"].apply(normalize_player_name)

    # Pull only logs for players we need (props joined to logs by name) — much
    # faster than fetching the full 55K-row table, and avoids Supabase's silent
    # 1000-row cap that bites a naive fetch().
    needed_players = props["player_name_norm"].dropna().unique().tolist()
    if not needed_players:
        print("[edge] No props with player names.")
        return 0

    # Fetch logs in chunks of 100 names at a time (URL length safety)
    log_frames = []
    for i in range(0, len(needed_players), 100):
        batch = needed_players[i:i+100]
        # The DB stores raw player_name; normalize on the fly. Get all rows for
        # any player whose normalized name is in our batch via a paginated read.
        chunk = fetch_all("nba_player_logs")
        if chunk.empty:
            break
        chunk["player_name_norm"] = chunk["player_name"].apply(normalize_player_name)
        log_frames.append(chunk[chunk["player_name_norm"].isin(needed_players)])
        break  # full table already pulled by fetch_all
    logs = pd.concat(log_frames, ignore_index=True) if log_frames else pd.DataFrame()
    if logs.empty:
        print("[edge] No player logs match props — run player_logs_sync backfill.")
        return 0
    print(f"[edge] Loaded {len(logs)} log rows covering {logs['player_name_norm'].nunique()} players")

    # Player meta (position, mpg) from the most recent log per player
    meta_rows = []
    for pn, sub in logs.groupby("player_name_norm"):
        sub = sub.sort_values("game_date").tail(20)
        meta_rows.append({
            "player_name_norm": pn,
            "player_id":        int(sub.iloc[-1].get("player_id") or 0),
            "team_abbr":        str(sub.iloc[-1].get("team_abbr") or ""),
            "position":         "SG",                      # TODO: pull from commonplayerinfo
            "minutes_per_game": float(sub["minutes"].mean() or 0),
        })
    meta_df = pd.DataFrame(meta_rows)

    pos_def   = fetch("nba_pos_def")
    injuries  = fetch("nba_injuries")
    series    = fetch("nba_playoff_series")

    all_edges: list[dict] = []
    for _, g in games.iterrows():
        all_edges.extend(calculate_edges_for_game(
            g.to_dict(), props, logs, meta_df, pos_def, injuries, series,
        ))

    if not all_edges:
        print("[edge] 0 edges produced.")
        return 0

    # Upsert via the helper — handles NaN cleaning + dedupe-by-id automatically
    upsert("nba_prop_edges", all_edges, on_conflict="id")

    soft = sum(1 for e in all_edges if e["edge"] >= EDGE_SOFT_THRESHOLD)
    print(f"[edge] {len(all_edges)} priced | {soft} ≥{int(EDGE_SOFT_THRESHOLD*100)}% edge")
    return len(all_edges)


if __name__ == "__main__":
    calculate_all_edges()

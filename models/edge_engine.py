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

from config import CURRENT_SEASON, EDGE_SOFT_THRESHOLD, BOOKS, ALT_LADDER_VIG_DEFAULT, TEAM_ABBR_TO_ID
from models.distribution import fit_distribution, StatDistribution
from models.adjustments import compose
from models.kelly import kelly_dollars
from utils.db import get_client, fetch, fetch_in, fetch_all, upsert
from utils.helpers import american_to_implied, remove_vig, normalize_player_name
from utils.positions import bulk_get_positions
from models.calibration import load_calibration_lookup, calibrate_prob, load_market_confidence
from models.auto_log_picks import shadow_log_edges


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

# Stat-level gating — same audit-driven pattern as F5's ENABLED_MARKETS.
# After 884 settled NBA shadow picks (audit 2026-05-04):
#   pts   46.3% / +25.5% ROI  ✅
#   pra   50.6% / +12.8% ROI  ✅
#   fg3m  43.2% / +81.2% ROI  ✅ (n=74)
#   blk   50.0% /  +1.3% ROI  ≈ (n=20)
#   stl   50.0% /  -9.4% ROI  ≈ (n=18, breakeven)
#   reb   40.1% / -12.8% ROI  ❌
#   ast   34.5% / -27.5% ROI  ❌  (model overshoots predicted by 6pp)
# Cut ast + reb pending rebuild. Re-enable by adding to ENABLED_STATS.
ENABLED_STATS = {
    "pts",
    "pra",
    "fg3m",
    "blk",
    "stl",
    # "reb",   # disabled 2026-05-04 — pending rebuild (-12.8% ROI on n=162)
    # "ast",   # disabled 2026-05-04 — pending rebuild (-27.5% ROI on n=116)
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


def _player_playoff_aware_logs(
    logs_df: pd.DataFrame,
    player_name_norm: str,
    stat: str,
    is_playoff_context: bool,
    n: int = 60,
    min_playoff_samples: int = 5,
) -> list[float]:
    """Playoff-aware variant of `_player_recent_logs`.

    Why this exists: in playoffs, role-player minutes contract sharply
    (e.g. Sam Merrill went from 28-32 min in regular season to 17-23 min
    in Round 1). The default last-60 window contaminates the fit with
    high-minute regular-season games that no longer reflect the player's
    role. When a player has at least `min_playoff_samples` playoff games,
    use ONLY playoff games for the recent window — `fit_distribution`
    will then blend that against the full-season μ correctly.

    Falls back to the regular last-`n` window when:
      - is_playoff_context is False, OR
      - the player has fewer than `min_playoff_samples` playoff games
        (sample too small to trust)
    """
    if logs_df.empty:
        return []
    sub = logs_df[logs_df["player_name_norm"] == player_name_norm].copy()
    if sub.empty:
        return []
    sub["game_date"] = pd.to_datetime(sub["game_date"], errors="coerce")
    sub = sub.sort_values("game_date")

    if is_playoff_context and "season_type" in sub.columns:
        playoff_sub = sub[sub["season_type"].astype(str).str.lower() == "playoffs"]
        if len(playoff_sub) >= min_playoff_samples:
            sub = playoff_sub
    sub = sub.tail(n)
    return [float(v) for v in sub[stat].tolist() if pd.notna(v)]


def _no_vig_consensus(
    prices: list[int],
    opp_prices: list[int],
    single_side_vig: float = ALT_LADDER_VIG_DEFAULT,
) -> Optional[float]:
    """Strip vig per book, then average across books.

    Per-book devig is more accurate than averaging implied probs first — books
    can run different holds and the order of operations changes the answer.

    If both sides exist (typical main markets): proper 2-way devig per book.
    If only one side exists (alt ladders — books post Over-only deep tails):
    strip `single_side_vig` per price. The default is ~10% (a realistic estimate
    of alt-prop hold; tunable via config). Underestimating vig understates real
    edge, so this errs slightly conservative.
    """
    if not prices:
        return None

    # Two-way devig per book when we have paired sides
    if opp_prices and len(opp_prices) == len(prices):
        devigged = []
        for pp, pn in zip(prices, opp_prices):
            p_imp = american_to_implied(pp)
            o_imp = american_to_implied(pn)
            if p_imp <= 0 or o_imp <= 0:
                continue
            novig, _ = remove_vig(p_imp, o_imp)
            devigged.append(novig)
        return float(np.mean(devigged)) if devigged else None

    # Two-way devig with mismatched book sets — fall back to averaged-implied devig
    if opp_prices:
        p_imp = float(np.mean([american_to_implied(p) for p in prices]))
        o_imp = float(np.mean([american_to_implied(p) for p in opp_prices]))
        if p_imp <= 0 or o_imp <= 0:
            return None
        novig, _ = remove_vig(p_imp, o_imp)
        return novig

    # One-side market (alt ladder) — strip assumed vig per price, then average
    devigged = []
    for p in prices:
        p_imp = american_to_implied(p)
        if p_imp <= 0:
            continue
        devigged.append(p_imp / (1.0 + single_side_vig))
    return float(np.mean(devigged)) if devigged else None


def _series_context(series_df: pd.DataFrame, home_abbr: str, away_abbr: str) -> dict:
    """Resolve playoff series state for a matchup.

    Primary source: ESPN scoreboard via `utils.series_state.get_series_state`
    (cached). The official `nba_playoff_series` table is checked as a
    secondary signal but typically shows 0-0 — `commonplayoffseries` doesn't
    return game scores so the sync can't count wins.
    """
    default = {
        "is_game7":                False,
        "is_elimination":          False,
        "series_fatigue_home":     0.0,
        "series_fatigue_away":     0.0,
    }
    # Primary: ESPN-derived
    try:
        from utils.series_state import get_series_state
        st = get_series_state(home_abbr, away_abbr)
        if st:
            return {
                "is_game7":            st["is_game7"],
                "is_elimination":      st["is_elimination"],
                "series_fatigue_home": st["series_fatigue_home"],
                "series_fatigue_away": st["series_fatigue_away"],
            }
    except Exception as e:
        print(f"[edge] series_state ESPN fallback error: {e}")

    # Secondary: nba_playoff_series table (currently broken due to sync bug,
    # kept as a fallback in case the sync is fixed later).
    if series_df is None or series_df.empty:
        return default
    home_id = TEAM_ABBR_TO_ID.get(home_abbr)
    away_id = TEAM_ABBR_TO_ID.get(away_abbr)
    if home_id is None or away_id is None:
        return default
    h, a = str(home_id), str(away_id)
    mask = (((series_df["team1_abbr"].astype(str) == h) & (series_df["team2_abbr"].astype(str) == a)) |
            ((series_df["team1_abbr"].astype(str) == a) & (series_df["team2_abbr"].astype(str) == h)))
    match = series_df[mask]
    if match.empty:
        return default
    r = match.iloc[0]
    if str(r.get("team1_abbr")) == h:
        fatigue_home = float(r.get("series_fatigue_team1") or 0.0)
        fatigue_away = float(r.get("series_fatigue_team2") or 0.0)
    else:
        fatigue_home = float(r.get("series_fatigue_team2") or 0.0)
        fatigue_away = float(r.get("series_fatigue_team1") or 0.0)
    return {
        "is_game7":            bool(r.get("is_game7", False)),
        "is_elimination":      bool(r.get("is_elimination", False)),
        "series_fatigue_home": fatigue_home,
        "series_fatigue_away": fatigue_away,
    }


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

    # Resolve series state (Game 7, elimination, fatigue) once per game.
    series_ctx = _series_context(
        series_df, str(game_row.get("home_abbr", "")), str(game_row.get("away_abbr", ""))
    ) if is_playoff else {
        "is_game7": False, "is_elimination": False,
        "series_fatigue_home": 0.0, "series_fatigue_away": 0.0,
    }

    # Cache fitted distributions per (player, stat) — same player, multiple lines
    fit_cache: dict[tuple[str, str], Optional[StatDistribution]] = {}
    # Group by (player_name_norm, market_base) to find both Over/Under sides per line
    if props_df.empty:
        return edges

    # Reduce props to this game only
    g_props = props_df[props_df["game_id"] == game_id]
    if g_props.empty:
        return edges

    # Build set of player-name-norms ruled out, doubtful, or otherwise
    # confirmed not playing tonight. The Odds API still returns props for
    # late scratches (e.g. KD listed Out for the day-of game), so without
    # this filter the model happily computes edges for players who won't
    # take the floor. Also build a set of player_ids for the same purpose so
    # downstream code can filter consistently by either key.
    out_norms: set[str] = set()
    out_ids: set[int] = set()
    if injuries_df is not None and not injuries_df.empty:
        bad = injuries_df[injuries_df["status"].isin(["out", "doubtful"])]
        if not bad.empty:
            from utils.helpers import normalize_player_name as _norm
            out_norms = {_norm(n) for n in bad["player_name"].astype(str)}
            out_ids = {int(pid) for pid in bad.get("player_id", pd.Series(dtype=int)).dropna().astype(int)}

    # Iterate per (player, market_base)
    for (player_norm, market), grp in g_props.groupby(["player_name_norm", "market"]):
        if player_norm in out_norms:
            continue

        stat_info = MARKET_TO_STAT.get(market)
        if not stat_info:
            continue
        stat, is_alt = stat_info

        # Audit-driven cut: skip stats not in ENABLED_STATS (currently ast + reb
        # disabled; both losing money on settled shadow picks).
        if stat not in ENABLED_STATS:
            continue

        # Skip if we have no values for this stat in logs (e.g., we don't track BLK)
        if stat not in logs_df.columns:
            continue

        meta = _player_meta(meta_df, player_norm)
        # Defense-in-depth: also filter by player_id so we don't slip through
        # late scratches whose name normalization didn't match (Jr/Sr suffixes,
        # accents, etc.).
        if int(meta.get("player_id") or 0) in out_ids:
            continue
        team_abbr = meta.get("team_abbr") or grp.iloc[0].get("team_abbr") or ""
        opp_abbr  = (game_row["away_abbr"] if team_abbr == game_row["home_abbr"]
                     else game_row["home_abbr"])
        days_rest = (game_row.get("rest_days_home") if team_abbr == game_row["home_abbr"]
                     else game_row.get("rest_days_away"))

        # Cache key includes playoff flag — same player gets a separate fit
        # for playoff games so role-minute changes don't contaminate the model.
        cache_key = (player_norm, stat, is_playoff)
        if cache_key not in fit_cache:
            values = _player_playoff_aware_logs(
                logs_df, player_norm, stat, is_playoff_context=is_playoff,
            )
            fit_cache[cache_key] = fit_distribution(values)
        dist = fit_cache[cache_key]
        if dist is None:
            continue

        # Apply adjustments — multipliers on μ. Series context (Game 7,
        # elimination, fatigue) is resolved once per game above; pick the
        # fatigue value matching THIS player's side of the matchup.
        is_home_side = (team_abbr == game_row.get("home_abbr"))
        series_fatigue = (series_ctx["series_fatigue_home"] if is_home_side
                          else series_ctx["series_fatigue_away"])
        adj = compose(
            pos_def_df, injuries_df,
            opponent_abbr=opp_abbr,
            player_position=meta.get("position", "SG"),
            stat=stat,
            days_rest=days_rest,
            is_playoff=is_playoff,
            minutes_per_game=meta.get("minutes_per_game"),
            series_fatigue=series_fatigue,
            is_game7=series_ctx["is_game7"],
            is_elimination=series_ctx["is_elimination"],
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

    # Pull all current-season logs in one paginated read. (We tried filtering
    # by `player_name IN (...)` but DB stores raw names while we match on
    # normalized names — the filter would miss accents/Jr-Sr/etc.)
    logs = fetch_all("nba_player_logs")
    if logs.empty:
        print("[edge] No player logs found — run player_logs_sync backfill.")
        return 0
    logs["player_name_norm"] = logs["player_name"].apply(normalize_player_name)
    logs = logs[logs["player_name_norm"].isin(needed_players)]
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
            "minutes_per_game": float(sub["minutes"].mean() or 0),
        })
    meta_df = pd.DataFrame(meta_rows)

    # Resolve real positions from the disk-cached commonplayerinfo lookup.
    # The cache is warmed by the daily pos_def_sync; misses fall back to "SF".
    if not meta_df.empty:
        all_pids = meta_df["player_id"].astype(int).tolist()
        pos_map = bulk_get_positions(all_pids, fetch_missing=False)
        meta_df["position"] = meta_df["player_id"].map(pos_map).fillna("SF")
    else:
        meta_df["position"] = []

    # nba_pos_def is ~30 teams × 5 positions × 7 stats = 1050 rows — just over
    # the default fetch() cap of 1000. Use fetch_all so nothing silently drops.
    pos_def   = fetch_all("nba_pos_def")
    injuries  = fetch("nba_injuries")
    series    = fetch("nba_playoff_series")

    all_edges: list[dict] = []
    for _, g in games.iterrows():
        all_edges.extend(calculate_edges_for_game(
            g.to_dict(), props, logs, meta_df, pos_def, injuries, series,
        ))

    # Wipe today's edges before re-upserting — players who got injured or
    # scratched after the previous run produce no new edges, but their old
    # rows persist in the table without this cleanup.
    try:
        from utils.db import get_client
        client = get_client()
        client.table("nba_prop_edges").delete().in_("game_id", target_ids).execute()
    except Exception as e:
        print(f"[edge] cleanup warn: {e}")

    if not all_edges:
        print("[edge] 0 edges produced.")
        return 0

    # ── Empirical calibration ────────────────────────────────────────────────
    # Apply per-(market × is_alt × prob_bucket) actual hit rates from settled
    # picks. Alt and main markets are calibrated separately because their
    # hit-rate distributions differ wildly (alt longshots ~5-15%, mains ~50%).
    # Raw model output is preserved as fitted_mu / fitted_alpha for traceability.
    try:
        cal_lookup = load_calibration_lookup(min_n=8)
        if cal_lookup:
            adjusted = 0
            for e in all_edges:
                raw = e.get("model_prob")
                if raw is None:
                    continue
                # Combined situational multiplier — calibration dampens when
                # this is far from 1.0 so Game 7 / extreme matchup signals
                # aren't washed out by historical bucket averages.
                sit_mult = (
                    float(e.get("matchup_mult") or 1.0)
                    * float(e.get("rest_mult")    or 1.0)
                    * float(e.get("playoff_mult") or 1.0)
                    * float(e.get("injury_mult")  or 1.0)
                )
                cal = calibrate_prob(
                    float(raw), e.get("market_base", ""), cal_lookup,
                    is_alt=bool(e.get("is_alt", False)),
                    situational_mult=sit_mult,
                )
                if cal != raw:
                    # Note: model_prob_raw not persisted — schema doesn't have
                    # the column and we don't want a DDL migration. Calibrated
                    # value replaces raw in-place; raw is recoverable via the
                    # in-memory calibration lookup if needed for audit.
                    e["model_prob"] = cal
                    # Recompute edge with calibrated prob
                    novig = e.get("market_prob_novig") or 0
                    e["edge"] = round(cal - float(novig), 4)
                    adjusted += 1
            if adjusted:
                print(f"[edge] calibration applied to {adjusted} legs ({len(cal_lookup)} buckets in lookup)")
    except Exception as cal_err:
        print(f"[edge] calibration skipped: {cal_err}")

    # ── Per-market historical-ROI confidence weighting ─────────────────────
    # Markets the model has historically WON on get a bigger Kelly multiplier;
    # markets it's struggled on get shrunk. Scales Kelly only (no schema
    # change needed). Probabilities and raw edge are unchanged so the
    # underlying signal stays transparent. The dashboard can compute a
    # display-only confidence_edge by looking up load_market_confidence().
    try:
        market_conf = load_market_confidence()
        if market_conf:
            for e in all_edges:
                conf = float(market_conf.get(e.get("market_base", ""), 1.0))
                # Scale Kelly stakes — proven markets bet bigger, weak markets smaller
                for k in ("kelly_full", "kelly_half", "kelly_quarter"):
                    if e.get(k) is not None:
                        e[k] = round(float(e[k]) * conf, 2)
            print(f"[edge] market confidence applied to Kelly: " +
                  ", ".join(f"{m}={market_conf[m]:.2f}" for m in sorted(market_conf)))
    except Exception as conf_err:
        print(f"[edge] market confidence skipped: {conf_err}")

    # Upsert via the helper — handles NaN cleaning + dedupe-by-id automatically
    upsert("nba_prop_edges", all_edges, on_conflict="id")

    # Shadow-log every priced edge so we can grade it after games finish
    try:
        n_logged = shadow_log_edges(all_edges)
        if n_logged:
            print(f"[edge] shadow-logged {n_logged} picks for grading")
    except Exception as log_err:
        print(f"[edge] shadow-log skipped: {log_err}")

    soft = sum(1 for e in all_edges if e["edge"] >= EDGE_SOFT_THRESHOLD)
    print(f"[edge] {len(all_edges)} priced | {soft} ≥{int(EDGE_SOFT_THRESHOLD*100)}% edge")
    return len(all_edges)


if __name__ == "__main__":
    calculate_all_edges()

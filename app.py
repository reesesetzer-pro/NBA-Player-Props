"""
app.py — NBA Player Prop Model dashboard.

Tabs: Tonight · Best Bets · Alt Ladder · Parlay Builder · Bet Journal
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, date, timezone
import pandas as pd
import streamlit as st

from config import BOOKS, EDGE_SOFT_THRESHOLD, EDGE_STRONG_THRESHOLD, KELLY_BANKROLL
from utils.db import fetch, fetch_in, get_client
from utils.helpers import fmt_odds, normalize_player_name
from models.parlay import Leg, build_parlay, rank_combinations


st.set_page_config(
    page_title="NBA Player Props Model",
    page_icon="https://cdn.nba.com/logos/leagues/logo-nba.svg",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ── Helpers ───────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_games(target_date: str) -> pd.DataFrame:
    return fetch("nba_games", filters={"game_date": target_date})


@st.cache_data(ttl=60)
def load_edges_for(game_ids: list[str]) -> pd.DataFrame:
    if not game_ids:
        return pd.DataFrame()
    return fetch_in("nba_prop_edges", "game_id", game_ids)


def _market_label(base: str) -> str:
    return {"pts": "Points", "reb": "Rebounds", "ast": "Assists",
            "fg3m": "3-Pointers", "blk": "Blocks", "stl": "Steals",
            "pra": "P+R+A"}.get(base, base.upper())


# ── Header ────────────────────────────────────────────────────────────────────

today = date.today().isoformat()
st.markdown(
    f"<h1 style='margin:0'>🏀 NBA Player Props Model</h1>"
    f"<div style='color:#888'>{today} · DraftKings + FanDuel · "
    f"alt-ladder pricing</div>",
    unsafe_allow_html=True,
)

tabs = st.tabs(["Tonight", "⭐ Best Bets", "🪜 Alt Ladder", "🧩 Parlay Builder", "📓 Bet Journal"])


# ── Tab 1 — Tonight ───────────────────────────────────────────────────────────
with tabs[0]:
    games = load_games(today)
    if games.empty:
        st.info("No games scheduled for today (or sync hasn't run yet).")
    else:
        st.markdown(f"**{len(games)} games tonight**")
        for _, g in games.iterrows():
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])
                c1.markdown(f"**{g['away_abbr']} @ {g['home_abbr']}**  ·  "
                            f"{g.get('away_team','')} @ {g.get('home_team','')}")
                c2.metric("Rest (away)", g.get("rest_days_away", "—"))
                c3.metric("Rest (home)", g.get("rest_days_home", "—"))


# ── Tab 2 — Best Bets ─────────────────────────────────────────────────────────
with tabs[1]:
    games = load_games(today)
    edges = load_edges_for(games["id"].tolist() if not games.empty else [])

    c1, c2, c3 = st.columns(3)
    min_edge = c1.slider("Min edge", 0.0, 0.20, EDGE_SOFT_THRESHOLD, 0.01,
                         format="%.2f")
    market_filter = c2.multiselect(
        "Market", ["pts", "reb", "ast", "pra", "fg3m", "blk", "stl"],
        default=["pts", "reb", "ast", "pra"],
    )
    book_filter = c3.multiselect("Book", BOOKS, default=BOOKS)

    if edges.empty:
        st.info("No edges yet — run odds_sync + edge_engine first.")
    else:
        view = edges[
            (edges["edge"] >= min_edge)
            & (edges["market_base"].isin(market_filter))
            & (edges["best_book"].isin(book_filter))
        ].sort_values("edge", ascending=False)

        st.markdown(f"**{len(view)} edges ≥ {min_edge*100:.0f}%**")
        for _, r in view.head(40).iterrows():
            tier = "🔥" if r["edge"] >= EDGE_STRONG_THRESHOLD else "✓"
            with st.container(border=True):
                c1, c2, c3, c4, c5 = st.columns([3, 1, 1, 1, 1])
                c1.markdown(
                    f"{tier} **{r['player_name']}** "
                    f"{r['over_under']} {r['line']} {_market_label(r['market_base'])}  "
                    f"<span style='color:#888'>· {r['team_abbr']}</span>",
                    unsafe_allow_html=True,
                )
                c2.metric("Edge", f"{r['edge']*100:+.1f}%")
                c3.metric("Win %", f"{r['model_prob']*100:.1f}%")
                c4.metric("Best", f"{fmt_odds(r['best_price'])}",
                          help=f"on {r['best_book']}")
                c5.metric("Kelly ½", f"${r['kelly_half']:.0f}",
                          help=f"Full ${r['kelly_full']:.0f} · ¼ ${r['kelly_quarter']:.0f}")
                with st.expander("model details", expanded=False):
                    st.json({
                        "fitted_mu": r["fitted_mu"],
                        "fitted_alpha": r["fitted_alpha"],
                        "matchup_mult": r["matchup_mult"],
                        "rest_mult": r["rest_mult"],
                        "playoff_mult": r["playoff_mult"],
                        "injury_mult": r["injury_mult"],
                        "is_alt": bool(r["is_alt"]),
                    })


# ── Tab 3 — Alt Ladder Explorer ───────────────────────────────────────────────
with tabs[2]:
    games = load_games(today)
    edges = load_edges_for(games["id"].tolist() if not games.empty else [])

    if edges.empty:
        st.info("Run sync first.")
    else:
        players = sorted(edges["player_name"].dropna().unique().tolist())
        col1, col2 = st.columns([2, 1])
        player = col1.selectbox("Player", players, index=0 if players else None)
        market = col2.selectbox("Market",
                                ["pts", "reb", "ast", "pra", "fg3m"], index=0)

        ladder = (edges[(edges["player_name"] == player)
                        & (edges["market_base"] == market)]
                  .sort_values(["over_under", "line"]))
        if ladder.empty:
            st.info("No ladder data for this player + market.")
        else:
            st.markdown(f"### {player} · {_market_label(market)}")
            mu = ladder.iloc[0].get("fitted_mu", "—")
            st.caption(f"Fitted μ = {mu} · the line books should be near is the mean.")
            st.dataframe(
                ladder[["over_under", "line", "best_book", "best_price",
                        "model_prob", "market_prob_novig", "edge",
                        "kelly_half", "is_alt"]]
                .rename(columns={
                    "over_under": "O/U", "line": "Line", "best_book": "Book",
                    "best_price": "Price", "model_prob": "Model %",
                    "market_prob_novig": "Mkt no-vig", "edge": "Edge",
                    "kelly_half": "Kelly ½", "is_alt": "Alt?",
                })
                .style.format({
                    "Model %": "{:.1%}", "Mkt no-vig": "{:.1%}",
                    "Edge": "{:+.1%}", "Kelly ½": "${:.0f}",
                }),
                use_container_width=True, hide_index=True,
            )


# ── Tab 4 — Parlay Builder ────────────────────────────────────────────────────
with tabs[3]:
    games = load_games(today)
    edges = load_edges_for(games["id"].tolist() if not games.empty else [])

    if edges.empty:
        st.info("No edges yet.")
    else:
        st.markdown("Pick legs from positive-edge plays — see live correlation-adjusted EV.")
        candidates = edges[edges["edge"] >= 0.02].copy()
        candidates["label"] = (candidates["player_name"] + " " + candidates["over_under"]
                               + " " + candidates["line"].astype(str)
                               + " " + candidates["market_base"].apply(_market_label)
                               + "  (" + candidates["best_book"] + " "
                               + candidates["best_price"].apply(fmt_odds) + ")")
        if candidates.empty:
            st.info("No positive-edge legs available.")
        else:
            picked_labels = st.multiselect(
                "Legs", candidates["label"].tolist(),
                max_selections=6,
            )
            if picked_labels:
                picked = candidates[candidates["label"].isin(picked_labels)]
                legs = [Leg(
                    player_name=r["player_name"],
                    team_abbr=r["team_abbr"],
                    market_base=r["market_base"],
                    line=float(r["line"]),
                    over_under=r["over_under"],
                    price=int(r["best_price"]),
                    model_prob=float(r["model_prob"]),
                    game_id=str(r["game_id"]),
                    book=str(r["best_book"]),
                ) for _, r in picked.iterrows()]
                p = build_parlay(legs)
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Legs", len(legs))
                c2.metric("Combined win %", f"{p.adjusted_prob*100:.1f}%",
                          help=f"Independence: {p.independent_prob*100:.1f}%")
                c3.metric("Payout", fmt_odds(p.american_odds))
                c4.metric("Edge", f"{p.edge*100:+.1f}%")
                if p.notes:
                    st.caption(" · ".join(p.notes))

            st.markdown("---")
            st.markdown("**🤖 Auto-suggest 3-leg parlays** (one leg per game, edge ≥5%)")
            if st.button("Generate"):
                cand_legs = [Leg(
                    player_name=r["player_name"], team_abbr=r["team_abbr"],
                    market_base=r["market_base"], line=float(r["line"]),
                    over_under=r["over_under"], price=int(r["best_price"]),
                    model_prob=float(r["model_prob"]), game_id=str(r["game_id"]),
                    book=str(r["best_book"]),
                ) for _, r in candidates.head(50).iterrows()]
                ranked = rank_combinations(cand_legs, n_legs=3,
                                           min_edge=0.05, one_per_game=True)[:5]
                if not ranked:
                    st.info("No qualifying 3-leg parlays at this edge threshold.")
                else:
                    for p in ranked:
                        with st.container(border=True):
                            st.markdown(
                                " · ".join(f"**{L.player_name}** {L.over_under} "
                                           f"{L.line} {_market_label(L.market_base)}"
                                           for L in p.legs)
                            )
                            c1, c2, c3 = st.columns(3)
                            c1.metric("Win %", f"{p.adjusted_prob*100:.1f}%")
                            c2.metric("Payout", fmt_odds(p.american_odds))
                            c3.metric("Edge", f"{p.edge*100:+.1f}%")


# ── Tab 5 — Bet Journal ───────────────────────────────────────────────────────
with tabs[4]:
    bets = fetch("nba_bets", limit=500)
    if bets.empty:
        st.info("No bets logged yet.")
    else:
        bets = bets.sort_values("placed_at", ascending=False)
        c1, c2, c3, c4 = st.columns(4)
        n = len(bets); n_settled = (bets["result"].isin(["Win", "Loss", "Push"])).sum()
        wins = (bets["result"] == "Win").sum()
        pl = bets["profit_loss"].fillna(0).sum()
        c1.metric("Total bets", n)
        c2.metric("Settled", n_settled)
        c3.metric("Win rate",
                  f"{(wins / n_settled * 100) if n_settled else 0:.1f}%")
        c4.metric("P/L", f"${pl:+.2f}")
        st.dataframe(bets[[
            "placed_at", "player_name", "market_base", "line", "over_under",
            "book", "price", "stake", "result", "profit_loss"
        ]], use_container_width=True, hide_index=True)

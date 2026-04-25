"""
app.py — NBA Player Props Model dashboard.

Tabs: Tonight · Best Bets · 🎯 Alt Line Builder · Player Intel · Bet Journal
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, date, timezone
from itertools import combinations
import pandas as pd
import streamlit as st

from config import BOOKS, EDGE_SOFT_THRESHOLD, EDGE_STRONG_THRESHOLD, KELLY_BANKROLL
from utils.db import fetch, fetch_in, get_client
from utils.helpers import fmt_odds, normalize_player_name, american_to_implied, implied_to_american
from models.parlay import Leg, build_parlay, rank_combinations


st.set_page_config(
    page_title="NBA Player Props Model",
    page_icon="https://cdn.nba.com/logos/leagues/logo-nba.svg",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ── Global CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  .block-container { padding-top: 1.5rem; padding-bottom: 3rem; max-width: 1400px; }
  [data-testid="stHeader"] { background: transparent; }

  /* Tabs */
  .stTabs [data-baseweb="tab-list"] { gap: 4px; border-bottom: 1px solid #1E1E30; }
  .stTabs [data-baseweb="tab"] {
    background: transparent; color: #8888AA; padding: 10px 16px;
    font-weight: 600; font-size: 13px; border-radius: 0;
  }
  .stTabs [aria-selected="true"] { color: #00D4FF !important; border-bottom: 2px solid #00D4FF; }

  /* Edge badges */
  .edge-badge { display:inline-block; padding:5px 12px; border-radius:6px;
                font-family:'Space Mono', monospace; font-weight:800; font-size:14px;
                letter-spacing:-0.02em; }
  .edge-strong { background:#00D4FF20; color:#00D4FF; border:1px solid #00D4FF60; }
  .edge-soft   { background:#FFD70020; color:#FFD700; border:1px solid #FFD70060; }
  .edge-fade   { background:#FF6B3520; color:#FF6B35; border:1px solid #FF6B3560; }

  /* Cards */
  .nba-card {
    background:#0D0D18; border:1px solid #1E1E30; border-radius:12px;
    padding:18px 20px; margin-bottom:12px;
  }
  .nba-card-strong { border-left: 3px solid #00D4FF; }
  .nba-card-soft   { border-left: 3px solid #FFD700; }
  .nba-card-hammer {
    border-left: 4px solid #00FF88;
    background: linear-gradient(135deg, #0D0D18 0%, #0D2018 100%);
  }

  /* Metric blocks inside cards */
  .stat-block { text-align:center; }
  .stat-label { font-size:10px; color:#444466; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:3px; }
  .stat-value { font-family:'Space Mono', monospace; font-size:15px; color:#E2E2EE; font-weight:700; }
  .stat-sub   { font-size:10px; color:#666688; margin-top:2px; }

  /* Tag chips */
  .tag-alt    { background:#9D4EDD20; color:#9D4EDD; border:1px solid #9D4EDD60;
                padding:2px 8px; border-radius:4px; font-size:10px; font-weight:700; }
  .tag-main   { background:#4477AA20; color:#88AACC; border:1px solid #4477AA60;
                padding:2px 8px; border-radius:4px; font-size:10px; font-weight:700; }
  .tag-hammer { background:#00FF8820; color:#00FF88; border:1px solid #00FF8860;
                padding:2px 8px; border-radius:4px; font-size:10px; font-weight:700; }

  /* Sticky header */
  .header-bar {
    display:flex; align-items:center; gap:14px; margin-bottom:18px;
    padding:14px 0; border-bottom:1px solid #1E1E30;
  }
  .header-logo { width:40px; height:40px; }
  .header-title { font-size:24px; font-weight:800; letter-spacing:-0.02em; color:#E2E2EE; margin:0; }
  .header-sub   { font-size:12px; color:#666688; font-family:'Space Mono', monospace; }
</style>
""", unsafe_allow_html=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_games(target_date: str) -> pd.DataFrame:
    return fetch("nba_games", filters={"game_date": target_date})


@st.cache_data(ttl=60)
def load_edges_for(game_ids: tuple[str, ...]) -> pd.DataFrame:
    if not game_ids:
        return pd.DataFrame()
    return fetch_in("nba_prop_edges", "game_id", list(game_ids))


def _market_label(base: str) -> str:
    return {"pts": "Points", "reb": "Rebounds", "ast": "Assists",
            "fg3m": "3-Pointers", "blk": "Blocks", "stl": "Steals",
            "pra": "P+R+A"}.get(base, base.upper())


def _market_short(base: str) -> str:
    return {"pts": "PTS", "reb": "REB", "ast": "AST",
            "fg3m": "3PM", "blk": "BLK", "stl": "STL", "pra": "P+R+A"}.get(base, base.upper())


def _book_short(b: str) -> str:
    return {"draftkings": "DK", "fanduel": "FD"}.get(b, b)


def _edge_badge(edge: float) -> str:
    tier = "strong" if edge >= EDGE_STRONG_THRESHOLD else ("soft" if edge >= EDGE_SOFT_THRESHOLD else "fade")
    sign = "+" if edge >= 0 else ""
    return f'<span class="edge-badge edge-{tier}">{sign}{edge*100:.1f}%</span>'


# ── Header ────────────────────────────────────────────────────────────────────

today = date.today().isoformat()
st.markdown(f"""
<div class="header-bar">
  <img src="https://cdn.nba.com/logos/leagues/logo-nba.svg" class="header-logo" alt="NBA"/>
  <div>
    <h1 class="header-title">NBA Player Props Model</h1>
    <div class="header-sub">{today} · DraftKings + FanDuel · alt-ladder pricing · neg-bin distribution model</div>
  </div>
</div>
""", unsafe_allow_html=True)

tabs = st.tabs(["Tonight", "⭐ Best Bets", "🎯 Alt Line Builder", "🪜 Player Intel", "📓 Bet Journal"])


# Pre-load shared data
games_df = load_games(today)
edges_df = load_edges_for(tuple(games_df["id"].tolist()) if not games_df.empty else tuple())


# ── TAB 1 — Tonight ───────────────────────────────────────────────────────────
with tabs[0]:
    if games_df.empty:
        st.info("No games scheduled for today (or sync hasn't run yet).")
    else:
        st.markdown(f"#### {len(games_df)} games tonight")
        for _, g in games_df.iterrows():
            n_edges = len(edges_df[edges_df["game_id"] == g["id"]]) if not edges_df.empty else 0
            n_strong = len(edges_df[(edges_df["game_id"] == g["id"]) & (edges_df["edge"] >= EDGE_STRONG_THRESHOLD)]) if not edges_df.empty else 0
            st.markdown(f"""
            <div class="nba-card">
              <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:14px;">
                <div>
                  <div style="font-size:18px; font-weight:700; color:#E2E2EE;">
                    {g['away_abbr']} <span style="color:#444466">@</span> {g['home_abbr']}
                  </div>
                  <div style="font-size:12px; color:#666688; margin-top:4px;">
                    {g.get('away_team','')} @ {g.get('home_team','')}
                  </div>
                </div>
                <div style="display:flex; gap:24px;">
                  <div class="stat-block"><div class="stat-label">Rest · away</div>
                    <div class="stat-value">{g.get('rest_days_away','—')}d</div></div>
                  <div class="stat-block"><div class="stat-label">Rest · home</div>
                    <div class="stat-value">{g.get('rest_days_home','—')}d</div></div>
                  <div class="stat-block"><div class="stat-label">Total edges</div>
                    <div class="stat-value">{n_edges}</div></div>
                  <div class="stat-block"><div class="stat-label">≥7% strong</div>
                    <div class="stat-value" style="color:#00D4FF;">{n_strong}</div></div>
                </div>
              </div>
            </div>
            """, unsafe_allow_html=True)


# ── TAB 2 — Best Bets ─────────────────────────────────────────────────────────
with tabs[1]:
    if edges_df.empty:
        st.info("No edges yet — run odds_sync + edge_engine first.")
    else:
        c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
        min_edge = c1.slider("Min edge", 0.0, 0.30, EDGE_SOFT_THRESHOLD, 0.01, format="%.2f")
        market_filter = c2.multiselect(
            "Markets", ["pts", "reb", "ast", "pra", "fg3m", "blk", "stl"],
            default=["pts", "reb", "ast", "pra"],
        )
        book_filter = c3.multiselect("Books", BOOKS, default=BOOKS)
        sort_by = c4.selectbox("Sort by", ["Edge", "Win %", "Kelly $"])

        view = edges_df[
            (edges_df["edge"] >= min_edge)
            & (edges_df["market_base"].isin(market_filter))
            & (edges_df["best_book"].isin(book_filter))
        ].copy()

        if sort_by == "Edge":      view = view.sort_values("edge", ascending=False)
        elif sort_by == "Win %":   view = view.sort_values("model_prob", ascending=False)
        else:                      view = view.sort_values("kelly_half", ascending=False)

        st.markdown(f"**{len(view)} edges meeting filter · {(view['edge']>=EDGE_STRONG_THRESHOLD).sum()} strong (≥7%)**")

        # Hero hammer card
        if not view.empty:
            r = view.iloc[0]
            st.markdown(f"""
            <div class="nba-card nba-card-hammer">
              <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:16px;">
                <div style="flex:1; min-width:280px;">
                  <span class="tag-hammer">🔨 HAMMER OF THE NIGHT</span>
                  <div style="font-size:22px; font-weight:800; margin-top:10px; color:#E2E2EE;">
                    {r['player_name']}
                  </div>
                  <div style="font-size:14px; color:#B8B8D4; margin-top:4px;">
                    {r['over_under']} {r['line']} {_market_label(r['market_base'])} · {r['team_abbr']}
                  </div>
                </div>
                <div style="display:flex; gap:24px; flex-wrap:wrap;">
                  <div class="stat-block"><div class="stat-label">EDGE</div>
                    <div class="stat-value" style="color:#00FF88; font-size:20px;">{r['edge']*100:+.1f}%</div></div>
                  <div class="stat-block"><div class="stat-label">WIN %</div>
                    <div class="stat-value" style="color:#00D4FF; font-size:20px;">{r['model_prob']*100:.1f}%</div></div>
                  <div class="stat-block"><div class="stat-label">BEST PRICE</div>
                    <div class="stat-value" style="font-size:20px;">{fmt_odds(r['best_price'])}</div>
                    <div class="stat-sub">{_book_short(r['best_book'])}</div></div>
                  <div class="stat-block"><div class="stat-label">KELLY ½</div>
                    <div class="stat-value" style="color:#00FF88; font-size:18px;">${r['kelly_half']:.0f}</div></div>
                </div>
              </div>
            </div>
            """, unsafe_allow_html=True)

        # Rest of the edges
        for _, r in view.iloc[1:31].iterrows():
            tier_class = "nba-card-strong" if r["edge"] >= EDGE_STRONG_THRESHOLD else "nba-card-soft"
            tag = '<span class="tag-alt">🪜 ALT</span>' if r.get("is_alt") else '<span class="tag-main">MAIN</span>'
            st.markdown(f"""
            <div class="nba-card {tier_class}">
              <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:14px;">
                <div style="flex:1; min-width:260px;">
                  <div style="font-size:15px; font-weight:700; color:#E2E2EE;">
                    {r['player_name']} <span style="color:#888;font-weight:400;">({r['team_abbr']})</span>
                  </div>
                  <div style="font-size:13px; color:#B8B8D4; margin-top:3px;">
                    {r['over_under']} <strong>{r['line']}</strong> {_market_label(r['market_base'])}  &nbsp; {tag}
                  </div>
                </div>
                <div style="display:flex; gap:18px; flex-wrap:wrap;">
                  <div class="stat-block"><div class="stat-label">EDGE</div>{_edge_badge(r['edge'])}</div>
                  <div class="stat-block"><div class="stat-label">WIN %</div>
                    <div class="stat-value" style="color:#00D4FF;">{r['model_prob']*100:.1f}%</div></div>
                  <div class="stat-block"><div class="stat-label">BEST</div>
                    <div class="stat-value">{fmt_odds(r['best_price'])}</div>
                    <div class="stat-sub">{_book_short(r['best_book'])}</div></div>
                  <div class="stat-block"><div class="stat-label">KELLY ½</div>
                    <div class="stat-value" style="color:#00FF88;">${r['kelly_half']:.0f}</div></div>
                </div>
              </div>
            </div>
            """, unsafe_allow_html=True)


# ── TAB 3 — 🎯 Alt Line Builder (the new centerpiece) ─────────────────────────
with tabs[2]:
    st.markdown("""
    <div style="margin-bottom:18px;">
      <div style="font-size:18px; font-weight:700; color:#E2E2EE;">
        🎯 Alt Line Builder
      </div>
      <div style="font-size:13px; color:#888; margin-top:4px;">
        Combine the highest-probability alt-line OVERs across different players into a parlay.
        These are the picks the model thinks are <strong>most likely to actually hit</strong> —
        not necessarily the highest-edge plays. Designed for high-hit-rate parlays, not chasing variance.
      </div>
    </div>
    """, unsafe_allow_html=True)

    if edges_df.empty:
        st.info("No edges yet.")
    else:
        # Pool: alt OVER lines, prioritized by win prob, with optional edge floor
        c1, c2, c3, c4 = st.columns(4)
        min_prob = c1.slider("Min win %", 0.50, 0.99, 0.85, 0.01, format="%.2f",
                             help="Each leg must clear this probability")
        min_edge_floor = c2.slider("Min edge", -0.05, 0.20, 0.00, 0.01, format="%.2f",
                                   help="0 = include legs where model agrees with market")
        n_legs = c3.selectbox("Legs", [2, 3, 4, 5], index=1)
        market_pick = c4.multiselect(
            "Markets", ["pts", "reb", "ast", "pra", "fg3m"],
            default=["pts", "reb", "ast", "pra"],
        )

        # Build pool: only OVERs (alt ladders are over-only on most books)
        pool = edges_df[
            (edges_df["over_under"] == "Over")
            & (edges_df["model_prob"] >= min_prob)
            & (edges_df["edge"] >= min_edge_floor)
            & (edges_df["market_base"].isin(market_pick))
        ].copy()

        # For each player+market_base, keep only the line with the LOWEST line value
        # that still has model_prob ≥ min_prob — that's the "safest clearable" line.
        # (Higher lines = lower prob; we want the easy clears, not the moonshots.)
        pool = pool.sort_values(["player_name", "market_base", "line"])
        pool = pool.drop_duplicates(subset=["player_name", "market_base"], keep="first")
        pool = pool.sort_values("model_prob", ascending=False).reset_index(drop=True)

        st.markdown(f"**{len(pool)} qualifying alt-line legs** "
                    f"(after filtering & per-player dedupe)")

        if pool.empty:
            st.info("No legs match the filters. Try lowering Min win % or expanding markets.")
        else:
            # ── AUTO-SUGGESTED PARLAYS ────────────────────────────────────────
            st.markdown("### 🤖 Top suggested parlays")
            st.caption(f"Best {n_legs}-leg combinations from different games (low correlation), ranked by combined win probability.")

            cand_legs = [Leg(
                player_name=r["player_name"], team_abbr=r["team_abbr"],
                market_base=r["market_base"], line=float(r["line"]),
                over_under=r["over_under"], price=int(r["best_price"]),
                model_prob=float(r["model_prob"]), game_id=str(r["game_id"]),
                book=str(r["best_book"]),
            ) for _, r in pool.head(40).iterrows()]

            # Generate combinations from different games for low correlation
            combos = []
            for combo in combinations(cand_legs, n_legs):
                if len({L.game_id for L in combo}) < n_legs:
                    continue
                p = build_parlay(list(combo))
                combos.append(p)

            combos.sort(key=lambda p: p.adjusted_prob, reverse=True)
            top = combos[:5]

            if not top:
                st.info("No multi-game parlays available — try fewer legs or expand filters.")
            else:
                for i, p in enumerate(top):
                    legs_html = "".join(
                        f"<div style='display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px dashed #1E1E30;'>"
                        f"<div><strong>{L.player_name}</strong> "
                        f"<span style='color:#888'>({L.team_abbr})</span> · "
                        f"Over <strong>{L.line}</strong> {_market_short(L.market_base)}</div>"
                        f"<div style='font-family:Space Mono,monospace; color:#B8B8D4;'>"
                        f"{L.model_prob*100:.1f}% · {fmt_odds(L.price)} ({_book_short(L.book)})</div>"
                        f"</div>"
                        for L in p.legs
                    )
                    badge = "🔨 HAMMER" if i == 0 else f"#{i+1}"
                    border = "nba-card-hammer" if i == 0 else "nba-card-strong"
                    st.markdown(f"""
                    <div class="nba-card {border}">
                      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                        <span class="tag-hammer">{badge}</span>
                        <div style="display:flex; gap:20px;">
                          <div class="stat-block"><div class="stat-label">COMBINED WIN %</div>
                            <div class="stat-value" style="color:#00D4FF; font-size:18px;">{p.adjusted_prob*100:.1f}%</div></div>
                          <div class="stat-block"><div class="stat-label">PAYOUT</div>
                            <div class="stat-value" style="font-size:18px;">{fmt_odds(p.american_odds)}</div></div>
                          <div class="stat-block"><div class="stat-label">EDGE</div>
                            <div class="stat-value" style="color:{'#00FF88' if p.edge>0 else '#FF6B35'};">{p.edge*100:+.1f}%</div></div>
                        </div>
                      </div>
                      {legs_html}
                    </div>
                    """, unsafe_allow_html=True)

            # ── MANUAL BUILDER ────────────────────────────────────────────────
            st.markdown("---")
            st.markdown("### 🛠 Manual builder")
            st.caption("Pick exact legs yourself — combined win% and payout update live.")

            pool["label"] = (
                pool["player_name"] + "  ·  Over " + pool["line"].astype(str) + " "
                + pool["market_base"].apply(_market_short)
                + "  ·  " + (pool["model_prob"]*100).round(1).astype(str) + "%  "
                + pool["best_price"].apply(fmt_odds) + " ("
                + pool["best_book"].apply(_book_short) + ")"
            )
            picked = st.multiselect(
                "Select legs", pool["label"].tolist(), max_selections=8,
                key="manual_legs",
            )
            if picked:
                p_rows = pool[pool["label"].isin(picked)]
                legs = [Leg(
                    player_name=r["player_name"], team_abbr=r["team_abbr"],
                    market_base=r["market_base"], line=float(r["line"]),
                    over_under=r["over_under"], price=int(r["best_price"]),
                    model_prob=float(r["model_prob"]), game_id=str(r["game_id"]),
                    book=str(r["best_book"]),
                ) for _, r in p_rows.iterrows()]
                pp = build_parlay(legs)
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Legs", len(legs))
                c2.metric("Combined win %", f"{pp.adjusted_prob*100:.1f}%",
                          help=f"Independence math: {pp.independent_prob*100:.1f}%")
                c3.metric("Payout", fmt_odds(pp.american_odds))
                c4.metric("Edge", f"{pp.edge*100:+.1f}%")
                if pp.notes:
                    st.caption("Adjustments: " + " · ".join(pp.notes))


# ── TAB 4 — 🪜 Player Intel (alt ladder for one player) ───────────────────────
with tabs[3]:
    if edges_df.empty:
        st.info("Run sync first.")
    else:
        st.markdown("Inspect a single player's full alt ladder vs DK + FD pricing — see exactly where the soft lines are.")
        players = sorted(edges_df["player_name"].dropna().unique().tolist())
        col1, col2 = st.columns([2, 1])
        player = col1.selectbox("Player", players, index=0 if players else None)
        market = col2.selectbox("Market", ["pts", "reb", "ast", "pra", "fg3m"], index=0)

        ladder = (edges_df[(edges_df["player_name"] == player)
                           & (edges_df["market_base"] == market)]
                  .sort_values(["over_under", "line"]))
        if ladder.empty:
            st.info("No ladder data for this combo.")
        else:
            mu = ladder.iloc[0].get("fitted_mu", "—")
            alpha = ladder.iloc[0].get("fitted_alpha", "—")
            c1, c2 = st.columns(2)
            c1.metric("Fitted μ (expected)", f"{mu}")
            c2.metric("Fitted α (dispersion)", f"{alpha}")

            disp = (ladder[["over_under", "line", "best_book", "best_price",
                            "model_prob", "market_prob_novig", "edge",
                            "kelly_half", "is_alt"]]
                    .rename(columns={
                        "over_under": "O/U", "line": "Line", "best_book": "Book",
                        "best_price": "Price", "model_prob": "Win %",
                        "market_prob_novig": "Mkt no-vig", "edge": "Edge",
                        "kelly_half": "Kelly ½", "is_alt": "Alt?",
                    }))
            disp["Book"] = disp["Book"].map(_book_short)
            disp["Price"] = disp["Price"].apply(fmt_odds)
            st.dataframe(
                disp.style.format({
                    "Win %": "{:.1%}", "Mkt no-vig": "{:.1%}",
                    "Edge": "{:+.1%}", "Kelly ½": "${:.0f}",
                }),
                use_container_width=True, hide_index=True,
            )


# ── TAB 5 — 📓 Bet Journal ────────────────────────────────────────────────────
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

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
games_df_all = load_games(today)

# Hide games that are already done. A game is hidden if either:
#   * game_state is "final" / "live" / "postponed" (NBA-authoritative)
#   * commence_time is in the past OR > 24h in the future (out-of-window).
# Games with no time AND scheduled state stay visible (safer default).
_now_dt = datetime.now(timezone.utc)
if not games_df_all.empty:
    def _is_pregame(row):
        state = str(row.get("game_state") or "scheduled").lower()
        if state in ("final", "live", "postponed"):
            return False
        ct = row.get("commence_time")
        if ct is None or pd.isna(ct):
            return True
        try:
            ct_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
        except Exception:
            return True
        hours_away = (ct_dt - _now_dt).total_seconds() / 3600
        return 0 < hours_away <= 24

    games_df = games_df_all[games_df_all.apply(_is_pregame, axis=1)].copy()
    n_started = len(games_df_all) - len(games_df)
else:
    games_df = games_df_all
    n_started = 0

edges_df = load_edges_for(tuple(games_df["id"].tolist()) if not games_df.empty else tuple())


# ── TAB 1 — Tonight ───────────────────────────────────────────────────────────
with tabs[0]:
    if games_df.empty:
        if n_started:
            st.info(f"All {n_started} of today's games have already started — no pre-game opportunities left. Check back tomorrow.")
        else:
            st.info("No games scheduled for today (or sync hasn't run yet).")
    else:
        sub = f" · {n_started} already tipped off (hidden)" if n_started else ""
        st.markdown(f"#### {len(games_df)} games remaining tonight{sub}")
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
        c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1, 1])
        min_edge = c1.slider("Min edge", 0.0, 0.30, EDGE_SOFT_THRESHOLD, 0.01, format="%.2f")
        min_price_bb = c2.slider("Min price (American)", -1000, +200, -550, 10,
                                 help="Cap how juiced you'll see. Default -550 hides extreme chalk.")
        market_filter = c3.multiselect(
            "Markets", ["pts", "reb", "ast", "pra", "fg3m", "blk", "stl"],
            default=["pts", "reb", "ast", "pra"],
        )
        book_filter = c4.multiselect("Books", BOOKS, default=BOOKS)
        sort_by = c5.selectbox("Sort by", ["Edge", "Win %", "Kelly $"])

        view = edges_df[
            (edges_df["edge"] >= min_edge)
            & (edges_df["best_price"] >= min_price_bb)
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
        # Pool filters — min win % is the primary lever; min price is a safety
        # net to keep extreme chalk out of the suggester.
        c1, c2, c3, c4, c5 = st.columns(5)
        min_prob = c1.slider("Min win %", 0.60, 0.99, 0.67, 0.01, format="%.2f",
                             help="Each leg must clear this model probability. Default 67% — slide down to 60% for more leg options, up to ~75% for safer combos.")
        min_edge_floor = c2.slider("Min edge", -0.05, 0.20, 0.00, 0.01, format="%.2f",
                                   help="0 = include legs where model agrees with market")
        min_price = c3.slider("Min price (American)", -500, +200, -200, 5,
                              help="Extreme-chalk guard. -200 default = nothing juicier than -200; rarely binds when min win % is set sensibly.")
        n_legs = c4.selectbox("Legs", [2, 3, 4, 5], index=1)
        market_pick = c5.multiselect(
            "Markets", ["pts", "reb", "ast", "pra", "fg3m"],
            default=["pts", "reb", "ast", "pra"],
        )

        # Build pool: only OVERs (alt ladders are over-only on most books)
        pool = edges_df[
            (edges_df["over_under"] == "Over")
            & (edges_df["model_prob"] >= min_prob)
            & (edges_df["edge"] >= min_edge_floor)
            & (edges_df["best_price"] >= min_price)
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

        # ── TRACKED PLAYERS panel ────────────────────────────────────────────
        tracked = st.session_state.get("tracked_players", [])
        if tracked:
            st.markdown(f"### ⭐ Tracked Players  <span style='color:#888;font-weight:400;font-size:14px;'>· {len(tracked)} on watchlist</span>",
                        unsafe_allow_html=True)
            for tp in tracked:
                t_lines = edges_df[
                    (edges_df["player_name"] == tp)
                    & (edges_df["over_under"] == "Over")
                    & (edges_df["market_base"].isin(market_pick))
                ].copy()
                if t_lines.empty:
                    st.markdown(f"<div class='nba-card' style='padding:10px 16px;'>"
                                f"⭐ <strong>{tp}</strong> "
                                f"<span style='color:#888;font-size:12px;'>· no lines match current market filter</span></div>",
                                unsafe_allow_html=True)
                    continue
                team = t_lines.iloc[0]["team_abbr"]
                # Top 3 plays for this player by edge
                top_picks = t_lines.sort_values("edge", ascending=False).head(3)
                picks_html = "".join(
                    f"<div style='display:flex;justify-content:space-between;padding:4px 0;'>"
                    f"<span>Over <strong>{r['line']}</strong> {_market_short(r['market_base'])}"
                    f"{' 🪜' if r.get('is_alt') else ''}</span>"
                    f"<span style='font-family:Space Mono,monospace;color:#B8B8D4;'>"
                    f"<span style='color:#00D4FF;'>{r['model_prob']*100:.1f}%</span> · "
                    f"<span style='color:{'#00FF88' if r['edge']>=EDGE_STRONG_THRESHOLD else '#FFD700'};'>{r['edge']*100:+.1f}%</span> · "
                    f"{fmt_odds(r['best_price'])} ({_book_short(r['best_book'])})</span>"
                    f"</div>"
                    for _, r in top_picks.iterrows()
                )
                untrack_cols = st.columns([5, 1])
                untrack_cols[0].markdown(f"""
                <div class="nba-card nba-card-strong" style="margin-bottom:6px;">
                  <div style="font-size:15px;font-weight:700;color:#E2E2EE;">
                    ⭐ {tp} <span style='color:#888;font-weight:400;font-size:12px;'>({team})</span>
                  </div>
                  <div style="margin-top:8px;font-size:13px;">{picks_html}</div>
                </div>
                """, unsafe_allow_html=True)
                if untrack_cols[1].button("✕ Untrack", key=f"untrack_{tp}", use_container_width=True):
                    st.session_state.tracked_players = [p for p in tracked if p != tp]
                    st.rerun()
            st.markdown("---")

        if pool.empty:
            st.info("No legs match the filters. Try lowering Min win % or expanding markets.")
        else:
            # ── AUTO-SUGGESTED PARLAYS — HIGH-PROB ────────────────────────────
            st.markdown(f"### 🟢 Best {int(min_prob*100)}%+ legs — by win likelihood")
            st.caption(f"Highest combined-win-% parlays where every leg has model probability ≥ {int(min_prob*100)}% "
                       f"and price ≥ {min_price:+d}. Designed for high-hit-rate plays.")
            cand_legs = [Leg(
                player_name=r["player_name"], team_abbr=r["team_abbr"],
                market_base=r["market_base"], line=float(r["line"]),
                over_under=r["over_under"], price=int(r["best_price"]),
                model_prob=float(r["model_prob"]), game_id=str(r["game_id"]),
                book=str(r["best_book"]),
            ) for _, r in pool.head(60).iterrows()]

            # Auto-cap legs at the # of unique games available (one-per-game keeps
            # correlation low). On a 2-game slate, default 3-leg becomes 2-leg
            # rather than returning empty. If user explicitly wants more legs,
            # we relax to allow same-game pairs but never the same player twice.
            unique_games = len({L.game_id for L in cand_legs})
            effective_legs = min(n_legs, max(2, unique_games)) if unique_games > 0 else 0
            allow_same_game = n_legs > unique_games and unique_games > 0

            mode_note = (
                f"Best {effective_legs}-leg combos from different games (low correlation)"
                if not allow_same_game
                else f"Only {unique_games} games available — relaxing to {effective_legs}-leg with up to 2 legs per game"
            )
            st.caption(mode_note + ", ranked by combined win probability.")

            combos = []
            if effective_legs > 0:
                for combo in combinations(cand_legs, effective_legs):
                    games_in_combo = [L.game_id for L in combo]
                    players_in_combo = [L.player_name.lower() for L in combo]
                    # Always: never the same player twice
                    if len(set(players_in_combo)) < effective_legs:
                        continue
                    # Strict mode: every leg from a different game
                    if not allow_same_game and len(set(games_in_combo)) < effective_legs:
                        continue
                    # Relaxed mode: cap at 2 per game
                    if allow_same_game and any(games_in_combo.count(g) > 2 for g in set(games_in_combo)):
                        continue
                    p = build_parlay(list(combo))
                    combos.append(p)

            combos.sort(key=lambda p: p.adjusted_prob, reverse=True)
            top = combos[:5]

            if not top:
                st.info(
                    f"No qualifying parlays. Try lowering Min win % (currently {min_prob*100:.0f}%), "
                    f"reducing Legs (currently {n_legs}), or expanding markets. "
                    f"Pool has {len(cand_legs)} legs across {unique_games} games."
                )
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

            # ── 💰 BEST +300 OR BETTER PARLAY ─────────────────────────────────
            st.markdown("---")
            st.markdown("### 💰 Best +300 or better payout")
            st.caption("Highest combined-win-% parlay whose payout pays at least 3-to-1. Same min-price filter applied; uses a wider pool (drops the per-player dedupe so you can stack alt lines for bigger payouts).")

            # Wider pool for the +300 hunt: include ALL alt lines (no per-player
            # dedupe) so we can find combos like Player A Over 18 + Player A Over 22
            # only if user wants — though we still enforce different players.
            big_pool = edges_df[
                (edges_df["over_under"] == "Over")
                & (edges_df["model_prob"] >= max(0.55, min_prob - 0.20))
                & (edges_df["edge"] >= min_edge_floor)
                & (edges_df["best_price"] >= min_price)
                & (edges_df["market_base"].isin(market_pick))
            ].copy()
            # Dedupe still per-player to avoid stacking the same player's lines
            big_pool = big_pool.sort_values(["player_name", "best_price"], ascending=[True, False])
            big_pool = big_pool.drop_duplicates(subset=["player_name"], keep="first")
            big_pool = big_pool.sort_values("model_prob", ascending=False).head(60)

            big_legs = [Leg(
                player_name=r["player_name"], team_abbr=r["team_abbr"],
                market_base=r["market_base"], line=float(r["line"]),
                over_under=r["over_under"], price=int(r["best_price"]),
                model_prob=float(r["model_prob"]), game_id=str(r["game_id"]),
                book=str(r["best_book"]),
            ) for _, r in big_pool.iterrows()]

            big_unique = len({L.game_id for L in big_legs})
            big_combos = []
            # Try 2-leg, 3-leg, 4-leg combos and find ones with combined ≥ +300
            for n in (2, 3, 4):
                if n > len(big_legs):
                    break
                for combo in combinations(big_legs, n):
                    games_in = [L.game_id for L in combo]
                    players_in = [L.player_name.lower() for L in combo]
                    if len(set(players_in)) < n:        # never same player twice
                        continue
                    if any(games_in.count(g) > 2 for g in set(games_in)):  # max 2 per game
                        continue
                    p = build_parlay(list(combo))
                    if p.american_odds >= 300:
                        big_combos.append(p)

            # Sort by combined win % desc — we want the SAFEST +300 combo
            big_combos.sort(key=lambda p: p.adjusted_prob, reverse=True)
            top_300 = big_combos[:5]

            if not top_300:
                st.info("No qualifying +300 parlays at current filter settings. Try lowering Min win % "
                        "or expanding markets to widen the pool.")
            else:
                for i, p in enumerate(top_300):
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
                    badge = "💰 BEST 3+ TO 1" if i == 0 else f"#{i+1}"
                    border = "nba-card-hammer" if i == 0 else "nba-card-soft"
                    st.markdown(f"""
                    <div class="nba-card {border}">
                      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                        <span class="tag-hammer">{badge}</span>
                        <div style="display:flex; gap:20px;">
                          <div class="stat-block"><div class="stat-label">COMBINED WIN %</div>
                            <div class="stat-value" style="color:#00D4FF; font-size:18px;">{p.adjusted_prob*100:.1f}%</div></div>
                          <div class="stat-block"><div class="stat-label">PAYOUT</div>
                            <div class="stat-value" style="color:#00FF88; font-size:18px;">{fmt_odds(p.american_odds)}</div></div>
                          <div class="stat-block"><div class="stat-label">EDGE</div>
                            <div class="stat-value" style="color:{'#00FF88' if p.edge>0 else '#FF6B35'};">{p.edge*100:+.1f}%</div></div>
                          <div class="stat-block"><div class="stat-label">LEGS</div>
                            <div class="stat-value" style="font-size:18px;">{len(p.legs)}</div></div>
                        </div>
                      </div>
                      {legs_html}
                    </div>
                    """, unsafe_allow_html=True)

            # ── BROWSE A PLAYER + BASKET ──────────────────────────────────────
            st.markdown("---")
            st.markdown("### 🪜 Browse a player's full alt ladder")
            st.caption("Pick any player to see every alt line they have on DK + FD. Click **+ Basket** on lines you want — bottom of page shows combined parlay math.")

            # Browse pool: ALL Over lines for any player (ignore min_prob/min_edge filters
            # so you can see the full ladder even on safer/longer-shot lines).
            browse_df = edges_df[
                (edges_df["over_under"] == "Over")
                & (edges_df["market_base"].isin(market_pick))
            ].copy().sort_values(["player_name", "market_base", "line"])

            all_players = sorted(browse_df["player_name"].dropna().unique().tolist())
            selected_player = st.selectbox(
                "Player", ["— select —"] + all_players, index=0, key="alt_browse_player",
            )

            if selected_player != "— select —":
                p_lines = browse_df[browse_df["player_name"] == selected_player]
                if p_lines.empty:
                    st.info("No lines available for this player + market filter.")
                else:
                    team = p_lines.iloc[0]["team_abbr"]
                    n_lines = len(p_lines)
                    n_strong = (p_lines["edge"] >= EDGE_STRONG_THRESHOLD).sum()
                    fitted_mu = p_lines.iloc[0].get("fitted_mu", "—")

                    # Player header card with Track button
                    is_tracked = selected_player in st.session_state.get("tracked_players", [])
                    star = "⭐" if is_tracked else "☆"
                    h_cols = st.columns([4, 1])
                    with h_cols[0]:
                        st.markdown(f"""
                        <div class="nba-card nba-card-strong" style="margin-bottom:8px;">
                          <div style="display:flex; align-items:center; gap:14px;">
                            <div>
                              <div style="font-size:20px; font-weight:800; color:#E2E2EE;">
                                {selected_player} <span style="color:#888; font-weight:400; font-size:14px;">({team})</span>
                              </div>
                              <div style="font-size:12px; color:#888; margin-top:4px;">
                                {n_lines} priced lines · {n_strong} strong (≥7% edge) · fitted μ={fitted_mu}
                              </div>
                            </div>
                          </div>
                        </div>
                        """, unsafe_allow_html=True)
                    with h_cols[1]:
                        track_label = f"{star} Untrack" if is_tracked else f"{star} Track player"
                        if st.button(track_label, key=f"track_{selected_player}", use_container_width=True):
                            tracked = st.session_state.get("tracked_players", [])
                            if is_tracked:
                                st.session_state.tracked_players = [p for p in tracked if p != selected_player]
                            else:
                                st.session_state.tracked_players = tracked + [selected_player]
                            st.rerun()

                    # Group by market — render each as a section with Add buttons per line
                    for mkt, mkt_grp in p_lines.groupby("market_base"):
                        n_alt = mkt_grp["is_alt"].fillna(False).sum()
                        st.markdown(f"##### {_market_label(mkt)}  "
                                    f"<span style='color:#888;font-size:12px;'>"
                                    f"({len(mkt_grp)} lines · {n_alt} alt)</span>",
                                    unsafe_allow_html=True)

                        # Header row
                        h = st.columns([1.2, 1.4, 1.4, 1.2, 0.8, 0.9])
                        h[0].markdown("<div class='stat-label'>LINE</div>", unsafe_allow_html=True)
                        h[1].markdown("<div class='stat-label'>WIN %</div>", unsafe_allow_html=True)
                        h[2].markdown("<div class='stat-label'>EDGE</div>", unsafe_allow_html=True)
                        h[3].markdown("<div class='stat-label'>PRICE</div>", unsafe_allow_html=True)
                        h[4].markdown("<div class='stat-label'>BOOK</div>", unsafe_allow_html=True)
                        h[5].markdown("&nbsp;", unsafe_allow_html=True)

                        for _, r in mkt_grp.iterrows():
                            c = st.columns([1.2, 1.4, 1.4, 1.2, 0.8, 0.9])
                            alt_tag = " 🪜" if r.get("is_alt") else ""
                            c[0].markdown(f"**Over {r['line']}**{alt_tag}")
                            # Win % bar
                            prob = float(r["model_prob"])
                            color = "#00FF88" if prob >= 0.85 else ("#00D4FF" if prob >= 0.65 else "#FFD700")
                            c[1].markdown(
                                f"<div style='font-family:Space Mono,monospace; color:{color}; font-weight:700;'>"
                                f"{prob*100:.1f}%</div>", unsafe_allow_html=True,
                            )
                            edge = float(r["edge"])
                            edge_color = "#00FF88" if edge >= EDGE_STRONG_THRESHOLD else (
                                         "#FFD700" if edge >= EDGE_SOFT_THRESHOLD else (
                                         "#888" if edge > -0.02 else "#FF6B35"))
                            c[2].markdown(
                                f"<div style='font-family:Space Mono,monospace; color:{edge_color}; font-weight:700;'>"
                                f"{edge*100:+.1f}%</div>", unsafe_allow_html=True,
                            )
                            c[3].markdown(
                                f"<div style='font-family:Space Mono,monospace;'>{fmt_odds(r['best_price'])}</div>",
                                unsafe_allow_html=True,
                            )
                            c[4].markdown(f"<span style='color:#888;'>{_book_short(r['best_book'])}</span>",
                                          unsafe_allow_html=True)
                            if c[5].button("+ Basket", key=f"basket_add_{r['id']}", use_container_width=True):
                                if "alt_basket" not in st.session_state:
                                    st.session_state.alt_basket = []
                                if r["id"] not in [x["id"] for x in st.session_state.alt_basket]:
                                    st.session_state.alt_basket.append({
                                        "id":          r["id"],
                                        "player_name": r["player_name"],
                                        "team_abbr":   r["team_abbr"],
                                        "market_base": r["market_base"],
                                        "line":        float(r["line"]),
                                        "over_under":  r["over_under"],
                                        "best_price":  int(r["best_price"]),
                                        "best_book":   r["best_book"],
                                        "model_prob":  float(r["model_prob"]),
                                        "game_id":     r["game_id"],
                                        "edge":        float(r["edge"]),
                                    })
                                    st.rerun()

            # ── PARLAY BASKET ─────────────────────────────────────────────────
            st.markdown("---")
            basket = st.session_state.get("alt_basket", [])
            head_cols = st.columns([5, 1])
            head_cols[0].markdown(f"### 🛒 Parlay Basket  <span style='color:#888;font-weight:400;font-size:14px;'>· {len(basket)} leg{'s' if len(basket)!=1 else ''}</span>", unsafe_allow_html=True)
            if basket and head_cols[1].button("🗑 Clear", key="basket_clear"):
                st.session_state.alt_basket = []
                st.rerun()

            if not basket:
                st.info("Empty — click **+ Basket** on any line above to add it. The basket persists while you browse different players.")
            else:
                # Render legs with remove buttons
                for leg in basket:
                    c = st.columns([3, 1, 1, 1, 0.6])
                    c[0].markdown(f"**{leg['player_name']}** "
                                  f"<span style='color:#888;'>({leg['team_abbr']})</span> · "
                                  f"Over <strong>{leg['line']}</strong> {_market_short(leg['market_base'])}",
                                  unsafe_allow_html=True)
                    c[1].markdown(f"<span style='color:#00D4FF;'>{leg['model_prob']*100:.1f}%</span>",
                                  unsafe_allow_html=True)
                    c[2].markdown(f"{fmt_odds(leg['best_price'])}")
                    c[3].markdown(f"<span style='color:#888;'>{_book_short(leg['best_book'])}</span>",
                                  unsafe_allow_html=True)
                    if c[4].button("✕", key=f"basket_remove_{leg['id']}"):
                        st.session_state.alt_basket = [x for x in basket if x["id"] != leg["id"]]
                        st.rerun()

                # Combined math
                legs_obj = [Leg(
                    player_name=L["player_name"], team_abbr=L["team_abbr"],
                    market_base=L["market_base"], line=L["line"],
                    over_under=L["over_under"], price=L["best_price"],
                    model_prob=L["model_prob"], game_id=L["game_id"],
                    book=L["best_book"],
                ) for L in basket]
                pp = build_parlay(legs_obj)
                st.markdown("")
                m = st.columns(4)
                m[0].metric("Legs", len(basket))
                m[1].metric("Combined win %", f"{pp.adjusted_prob*100:.1f}%",
                            help=f"Independence math: {pp.independent_prob*100:.1f}%")
                m[2].metric("Payout", fmt_odds(pp.american_odds))
                m[3].metric("Edge", f"{pp.edge*100:+.1f}%")
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

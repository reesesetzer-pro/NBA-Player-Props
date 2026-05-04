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
    df = fetch_in("nba_prop_edges", "game_id", list(game_ids))
    if df.empty:
        return df
    # Layer in per-market confidence (historical ROI on settled shadow picks).
    # `confidence_edge = edge × market_confidence` is a rank metric — markets
    # that have proven profitable get prioritized; underperformers shrink.
    # Probabilities are NOT modified (transparency); Kelly was already scaled
    # at edge_engine time so we re-derive `confidence_edge` only here.
    try:
        from models.calibration import load_market_confidence
        conf_map = load_market_confidence()
        df["market_confidence"] = df["market_base"].map(conf_map).fillna(1.0)
        df["confidence_edge"]   = (df["edge"] * df["market_confidence"]).round(4)
    except Exception:
        df["market_confidence"] = 1.0
        df["confidence_edge"]   = df["edge"]
    return df


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
hdr1, hdr2 = st.columns([3, 2])
with hdr1:
    st.markdown(f"""
    <div class="header-bar" style="border:none; padding:14px 0 0 0;">
      <img src="https://cdn.nba.com/logos/leagues/logo-nba.svg" class="header-logo" alt="NBA"/>
      <div>
        <h1 class="header-title">NBA Player Props Model</h1>
        <div class="header-sub">{today} · DK + FD + Fanatics · alt-ladder pricing · neg-bin distribution</div>
      </div>
    </div>
    """, unsafe_allow_html=True)
with hdr2:
    st.markdown("<div style='padding-top:24px;'></div>", unsafe_allow_html=True)
    book_choice_pretty = st.multiselect(
        "📍 Books to show (applies to every tab)",
        options=["DraftKings", "FanDuel", "Fanatics"],
        default=["DraftKings", "FanDuel", "Fanatics"],
        key="global_book_filter",
    )

# Map pretty names → internal book keys used in nba_props.book / nba_prop_edges.best_book
_BOOK_PRETTY_TO_KEY = {"DraftKings": "draftkings", "FanDuel": "fanduel", "Fanatics": "fanatics"}
selected_books = [_BOOK_PRETTY_TO_KEY[b] for b in book_choice_pretty] or list(_BOOK_PRETTY_TO_KEY.values())

st.markdown("<hr style='margin:8px 0 16px 0; border:none; border-top:1px solid #1E1E30;' />",
            unsafe_allow_html=True)

tabs = st.tabs(["🎯 MUST TAKE", "Tonight", "⭐ Best Bets", "🎰 +400 Longshots",
                "🚀 +500 Moonshots", "🎯 Alt Line Builder", "🪜 Player Intel", "📓 Bet Journal"])


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
        odds_eid = row.get("odds_event_id")
        if ct is None or pd.isna(ct):
            # No commence_time AND no odds_event_id = TBD placeholder game (NBA
            # Stats API often returns playoff games pending series advancement
            # that don't have markets yet). Hide to keep dashboard clean.
            if not odds_eid or pd.isna(odds_eid):
                return False
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

edges_df_all = load_edges_for(tuple(games_df["id"].tolist()) if not games_df.empty else tuple())

# Apply global book filter — restrict to edges where the BEST price came from a
# selected book. (Note: edges store best_price across all books; if user wants
# only DK plays we filter to those where DK had the best.)
if not edges_df_all.empty and selected_books and "best_book" in edges_df_all.columns:
    edges_df = edges_df_all[edges_df_all["best_book"].isin(selected_books)].copy()
else:
    edges_df = edges_df_all


# ── TAB 0 — 🎯 MUST TAKE ──────────────────────────────────────────────────────
with tabs[0]:
    st.markdown("## 🎯 Must Take")
    st.caption("Tiered by win-probability conviction. Same filters across tiers: "
               "enabled markets only, edge ≥ 6%, market confidence ≥ 1.05, ≤1 pick "
               "per player. Empty list = sit it out.")

    if edges_df.empty:
        st.info("No edges yet — run odds_sync + edge_engine first.")
    else:
        MUST_MIN_PROB       = 0.60
        MUST_MIN_EDGE       = 0.06
        MUST_MIN_CONFIDENCE = 1.05

        candidates = edges_df[
            (edges_df["model_prob"]        >= MUST_MIN_PROB)
            & (edges_df["edge"]            >= MUST_MIN_EDGE)
            & (edges_df["market_confidence"] >= MUST_MIN_CONFIDENCE)
        ].copy().sort_values("confidence_edge", ascending=False)

        # Dedupe one pick per player slate-wide
        seen = set(); picks_all = []
        for _, r in candidates.iterrows():
            if r["player_name"] in seen:
                continue
            seen.add(r["player_name"])
            picks_all.append(r)

        if not picks_all:
            st.warning(
                "⚠️ Zero picks pass the Must-Take filter tonight. **Sit it out** — "
                "don't force action. Other tabs have softer plays at your own risk."
            )
        else:
            # ── Tier picks by win-probability ────────────────────────────
            #   🟢 LOCK   ≥75%
            #   🟡 STRONG 65-75%
            #   🔴 EDGE   60-65% (gets in only via high edge / confidence)
            tiers = [
                ("🟢", "LOCKS",  "≥ 75% win prob — lowest variance",   0.75, 1.01,  "#00FF88", "nba-card-strong"),
                ("🟡", "STRONG", "65–75% win prob — solid signal",     0.65, 0.75,  "#FFD700", "nba-card-soft"),
                ("🔴", "EDGE",   "60–65% — pure edge plays",          0.60, 0.65,  "#FF6B35", "nba-card-soft"),
            ]

            for emoji, tier_name, tier_sub, lo, hi, accent, css_class in tiers:
                tier_picks = [p for p in picks_all if lo <= p["model_prob"] < hi]
                if not tier_picks:
                    continue

                # Header metrics — 3-column row to mirror Auto Parlays' PARLAY/HIT%/EDGE
                avg_win  = sum(p["model_prob"]*100 for p in tier_picks) / len(tier_picks)
                avg_edge = sum(p["edge"]*100      for p in tier_picks) / len(tier_picks)
                kelly_total = sum(p.get("kelly_quarter", 0) or 0 for p in tier_picks)

                # Render the tier card with bulleted picks (mirrors Auto Parlays)
                bullet_html = ""
                for p in tier_picks:
                    line = p["line"]
                    line_str = f"{line:g}" if line is not None else ""
                    market_lbl = _market_label(p["market_base"])
                    price = fmt_odds(p["best_price"])
                    book  = _book_short(p["best_book"])
                    bullet_html += (
                        f'<div style="font-size:14px;color:#E2E2EE;margin:8px 0;line-height:1.5">'
                        f'• <strong>{p["player_name"]}</strong> '
                        f'<span style="color:#666688">({p["team_abbr"]})</span> — '
                        f'{p["over_under"]} <strong>{line_str}</strong> {market_lbl} '
                        f'<span style="color:#888">@ {price} ({book})</span> '
                        f'&nbsp;<span style="color:#00D4FF;font-size:12px;font-weight:600">'
                        f'{p["model_prob"]*100:.1f}%</span> '
                        f'<span style="color:#00FF88;font-size:12px;font-weight:600">'
                        f'+{p["edge"]*100:.1f}%</span>'
                        f'</div>'
                    )

                st.markdown(f"""
                <div class="nba-card {css_class}" style="border-left:3px solid {accent};">
                  <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:14px;margin-bottom:8px;">
                    <div style="flex:1;min-width:280px">
                      <div style="font-size:16px;font-weight:800;color:{accent}">
                        {emoji} {tier_name}
                      </div>
                      <div style="font-size:12px;color:#888;margin-top:2px">{tier_sub}</div>
                    </div>
                    <div style="display:flex;gap:24px;flex-wrap:wrap">
                      <div class="stat-block"><div class="stat-label">PICKS</div>
                        <div class="stat-value">{len(tier_picks)}</div></div>
                      <div class="stat-block"><div class="stat-label">AVG WIN %</div>
                        <div class="stat-value" style="color:#00D4FF">{avg_win:.1f}%</div></div>
                      <div class="stat-block"><div class="stat-label">AVG EDGE</div>
                        <div class="stat-value" style="color:#00FF88">+{avg_edge:.1f}%</div></div>
                      <div class="stat-block"><div class="stat-label">¼K TOTAL</div>
                        <div class="stat-value" style="color:#00FF88">${kelly_total:.0f}</div></div>
                    </div>
                  </div>
                  <div style="margin-top:6px">
                    {bullet_html}
                  </div>
                </div>
                """, unsafe_allow_html=True)

            # Slate summary footer
            total_picks = len(picks_all)
            total_kelly = sum(p.get("kelly_quarter", 0) or 0 for p in picks_all)
            st.caption(
                f"💡 **{total_picks} total plays · ¼-Kelly ${total_kelly:.0f}** total exposure. "
                f"Filters: prob ≥{MUST_MIN_PROB*100:.0f}% · edge ≥{MUST_MIN_EDGE*100:.0f}% · "
                f"market_conf ≥{MUST_MIN_CONFIDENCE}. ¼-Kelly already factors in market "
                f"historical ROI — sizing reflects which markets the model has proven it can beat."
            )


# ── TAB 1 — Tonight ───────────────────────────────────────────────────────────
with tabs[1]:
    if games_df.empty:
        if n_started:
            st.info(f"All {n_started} of today's games have already started — no pre-game opportunities left. Check back tomorrow.")
        else:
            st.info("No games scheduled for today (or sync hasn't run yet).")
    else:
        sub = f" · {n_started} already tipped off (hidden)" if n_started else ""
        st.markdown(f"#### {len(games_df)} games remaining tonight{sub}")

        # Per-game controls
        ctl1, ctl2, ctl3 = st.columns([1.2, 1.2, 1])
        sort_per_game = ctl1.radio(
            "Sort top picks by", ["Edge", "Win %"], horizontal=True, key="tonight_sort",
        )
        min_price_tonight = ctl2.slider(
            "Min price (American)", -1000, +200, -550, 10, key="tonight_min_price",
            help="Cap how juiced you'll see. Default -550 hides extreme chalk.",
        )
        st.caption(f"Top 5 picks across the whole slate, then **each team's top 5** under its game card. "
                   f"Same min-edge floor as Best Bets (4%) and prices ≥ {min_price_tonight:+d}.")

        # ── Helper: render one pick card ────────────────────────────────────
        def _render_pick(r, indent_px: int = 24):
            tier_class = "nba-card-strong" if r["edge"] >= EDGE_STRONG_THRESHOLD else "nba-card-soft"
            tag = '<span class="tag-alt">🪜 ALT</span>' if r.get("is_alt") else '<span class="tag-main">MAIN</span>'
            st.markdown(f"""
            <div class="nba-card {tier_class}" style="margin-left:{indent_px}px; padding:12px 16px;">
              <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;">
                <div style="flex:1; min-width:240px;">
                  <div style="font-size:14px; font-weight:700; color:#E2E2EE;">
                    {r['player_name']} <span style="color:#888;font-weight:400;">({r['team_abbr']})</span>
                  </div>
                  <div style="font-size:12px; color:#B8B8D4; margin-top:3px;">
                    {r['over_under']} <strong>{r['line']}</strong> {_market_label(r['market_base'])}  &nbsp; {tag}
                  </div>
                </div>
                <div style="display:flex; gap:14px; flex-wrap:wrap;">
                  <div class="stat-block"><div class="stat-label">EDGE</div>{_edge_badge(r['edge'])}</div>
                  <div class="stat-block"><div class="stat-label">WIN %</div>
                    <div class="stat-value" style="color:#00D4FF;">{r['model_prob']*100:.1f}%</div></div>
                  <div class="stat-block"><div class="stat-label">BEST</div>
                    <div class="stat-value">{fmt_odds(r['best_price'])}</div>
                    <div class="stat-sub">{_book_short(r['best_book'])}</div></div>
                </div>
              </div>
            </div>
            """, unsafe_allow_html=True)

        # ── Top 5 picks across the whole slate ──────────────────────────────
        slate_qualifying = pd.DataFrame()
        if not edges_df.empty:
            slate_qualifying = edges_df[
                (edges_df["edge"] >= EDGE_SOFT_THRESHOLD)
                & (edges_df["best_price"] >= min_price_tonight)
            ].copy()
            if not slate_qualifying.empty:
                slate_sort_col = "edge" if sort_per_game == "Edge" else "model_prob"
                slate_top5 = (
                    slate_qualifying.sort_values(slate_sort_col, ascending=False)
                    .drop_duplicates(subset=["player_name", "market_base"])
                    .head(5)
                )
                st.markdown(
                    "<div style='margin:12px 0 4px 0; font-size:16px; font-weight:700; color:#FFD66B;'>"
                    "🔥 Top 5 Picks Tonight"
                    "</div>",
                    unsafe_allow_html=True,
                )
                for _, r in slate_top5.iterrows():
                    _render_pick(r, indent_px=0)
                st.markdown("")

        # ── Auto Parlays: three tiers, brute-force best combo per band ──────
        # Pool: one leg per player (highest model_prob), capped at top 20.
        # No leg-count cap — but we prune any branch whose minimum-possible
        # combined decimal already exceeds the band's max, which collapses the
        # search to just the leg counts that can actually fit the band.
        def _best_parlay_in_band(legs_pool, min_american, max_american):
            # American → decimal max for early-stop pruning
            max_decimal = (
                (max_american / 100.0) + 1.0 if max_american > 0
                else (100.0 / abs(max_american)) + 1.0
            )
            # Sort ascending by decimal so we know the floor of combined decimal
            sorted_pool = sorted(
                legs_pool,
                key=lambda L: ((L.price / 100.0) + 1.0 if L.price > 0
                               else (100.0 / abs(L.price)) + 1.0),
            )
            decimals = [
                (L.price / 100.0) + 1.0 if L.price > 0 else (100.0 / abs(L.price)) + 1.0
                for L in sorted_pool
            ]

            best = None
            n = 2
            while n <= len(sorted_pool):
                # Floor of combined decimal at this n = product of n smallest
                floor = 1.0
                for d in decimals[:n]:
                    floor *= d
                if floor > max_decimal:
                    break  # any larger n will only overshoot further
                for combo in combinations(sorted_pool, n):
                    if len({L.player_name.lower() for L in combo}) != n:
                        continue
                    try:
                        p = build_parlay(list(combo))
                    except Exception:
                        continue
                    if not (min_american <= p.american_odds <= max_american):
                        continue
                    if best is None or p.adjusted_prob > best.adjusted_prob:
                        best = p
                n += 1
            return best

        if not slate_qualifying.empty:
            pool_df = (
                slate_qualifying.sort_values("model_prob", ascending=False)
                .drop_duplicates(subset=["player_name"])
                .head(20)
            )
            leg_pool: list[Leg] = []
            for _, r in pool_df.iterrows():
                leg_pool.append(Leg(
                    player_name = str(r["player_name"]),
                    team_abbr   = str(r.get("team_abbr") or ""),
                    market_base = str(r.get("market_base") or ""),
                    line        = float(r["line"]),
                    over_under  = str(r["over_under"]),
                    price       = int(r["best_price"]),
                    model_prob  = float(r["model_prob"]),
                    game_id     = str(r["game_id"]),
                    book        = str(r["best_book"]),
                ))

            tiers = [
                ("🟢 Safer  (≥ -130)",  -130,   99, "Most-likely combo whose parlay still pays at least -130."),
                ("🟡 Medium (≤ +200)",  100,  200, "Most-likely parlay capped at +200."),
                ("🔴 Longer (≤ +300)",  201,  300, "Most-likely parlay stretching out to +300."),
            ]

            parlays = []
            for label, lo, hi, blurb in tiers:
                p = _best_parlay_in_band(leg_pool, lo, hi)
                if p is not None:
                    parlays.append((label, blurb, p))

            if parlays:
                st.markdown(
                    "<div style='margin:18px 0 4px 0; font-size:16px; font-weight:700; color:#FFD66B;'>"
                    "🎰 Auto Parlays"
                    "</div>",
                    unsafe_allow_html=True,
                )
                for label, blurb, p in parlays:
                    legs_html = "".join([
                        f"<div style='font-size:12px; color:#B8B8D4; margin:2px 0;'>"
                        f"&nbsp;&nbsp;• <strong>{L.player_name}</strong> "
                        f"<span style='color:#888'>({L.team_abbr})</span> &mdash; "
                        f"{L.over_under} <strong>{L.line}</strong> {_market_label(L.market_base)} "
                        f"@ {fmt_odds(L.price)} <span style='color:#666'>({_book_short(L.book)})</span>"
                        f"</div>"
                        for L in p.legs
                    ])
                    st.markdown(f"""
                    <div class="nba-card" style="margin:6px 0; padding:14px 16px;">
                      <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:14px; flex-wrap:wrap;">
                        <div style="flex:1; min-width:280px;">
                          <div style="font-size:13px; font-weight:700; color:#FFD66B; margin-bottom:6px;">{label}</div>
                          {legs_html}
                          <div style="font-size:11px; color:#777; margin-top:6px;">{blurb}</div>
                        </div>
                        <div style="display:flex; gap:14px; flex-wrap:wrap;">
                          <div class="stat-block"><div class="stat-label">PARLAY</div>
                            <div class="stat-value">{fmt_odds(p.american_odds)}</div></div>
                          <div class="stat-block"><div class="stat-label">HIT %</div>
                            <div class="stat-value" style="color:#00D4FF;">{p.adjusted_prob*100:.1f}%</div></div>
                          <div class="stat-block"><div class="stat-label">EDGE</div>{_edge_badge(p.edge)}</div>
                        </div>
                      </div>
                    </div>
                    """, unsafe_allow_html=True)
                st.markdown("")

        for _, g in games_df.iterrows():
            game_edges = edges_df[edges_df["game_id"] == g["id"]] if not edges_df.empty else pd.DataFrame()
            n_edges  = len(game_edges)
            n_strong = (game_edges["edge"] >= EDGE_STRONG_THRESHOLD).sum() if not game_edges.empty else 0

            # Game header card
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

            # Each team's top 5 plays for this game
            if game_edges.empty:
                st.caption("  · no edges priced for this game yet")
                st.markdown("")
                continue
            qualifying = game_edges[
                (game_edges["edge"] >= EDGE_SOFT_THRESHOLD)
                & (game_edges["best_price"] >= min_price_tonight)
            ].copy()
            if qualifying.empty:
                st.caption("  · no plays meet the 4% edge floor")
                st.markdown("")
                continue
            sort_col = "edge" if sort_per_game == "Edge" else "model_prob"

            for team_abbr, team_label in (
                (g["away_abbr"], g.get("away_team", g["away_abbr"])),
                (g["home_abbr"], g.get("home_team", g["home_abbr"])),
            ):
                team_picks = (
                    qualifying[qualifying["team_abbr"] == team_abbr]
                    .sort_values(sort_col, ascending=False)
                    .drop_duplicates(subset=["player_name", "market_base"])
                    .head(5)
                )
                st.markdown(
                    f"<div style='margin:8px 0 2px 12px; font-size:13px; font-weight:600; "
                    f"color:#9DB4FF; letter-spacing:0.5px;'>"
                    f"📊 {team_abbr} — Top 5"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                if team_picks.empty:
                    st.markdown(
                        "<div style='margin-left:24px; color:#666688; font-size:12px;'>"
                        "no qualifying plays for this team</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    for _, r in team_picks.iterrows():
                        _render_pick(r, indent_px=24)
            st.markdown("")  # spacer between games


# ── TAB 2 — Best Bets ─────────────────────────────────────────────────────────
with tabs[2]:
    if edges_df.empty:
        st.info("No edges yet — run odds_sync + edge_engine first.")
    else:
        c1, c2, c3, c4 = st.columns([1, 1, 1.5, 1])
        min_edge = c1.slider("Min edge", 0.0, 0.30, EDGE_SOFT_THRESHOLD, 0.01, format="%.2f")
        min_price_bb = c2.slider("Min price (American)", -1000, +200, -550, 10,
                                 help="Cap how juiced you'll see. Default -550 hides extreme chalk.")
        market_filter = c3.multiselect(
            "Markets", ["pts", "reb", "ast", "pra", "fg3m", "blk", "stl"],
            default=["pts", "reb", "ast", "pra"],
        )
        sort_by = c4.selectbox("Sort by", ["Confidence-Adj Edge", "Edge", "Win %", "Kelly $"])

        view = edges_df[
            (edges_df["edge"] >= min_edge)
            & (edges_df["best_price"] >= min_price_bb)
            & (edges_df["market_base"].isin(market_filter))
        ].copy()

        if sort_by == "Confidence-Adj Edge":
            view = view.sort_values("confidence_edge", ascending=False)
        elif sort_by == "Edge":
            view = view.sort_values("edge", ascending=False)
        elif sort_by == "Win %":
            view = view.sort_values("model_prob", ascending=False)
        else:
            view = view.sort_values("kelly_half", ascending=False)

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


# ── TAB 3 — 🎰 +400 Longshots ─────────────────────────────────────────────────
def _render_longshot_tab(min_price: int, label: str, threshold_text: str):
    """Filter edges to picks priced at or above min_price (American odds)."""
    if edges_df.empty:
        st.info("No edges yet — run odds_sync + edge_engine first.")
        return

    st.markdown(f"### {label}")
    st.caption(threshold_text)

    c1, c2, c3 = st.columns([1, 1.3, 1])
    min_edge = c1.slider("Min edge", -0.10, 0.50, 0.04, 0.01,
                         format="%.2f", key=f"ls_edge_{min_price}")
    market_filter = c2.multiselect(
        "Markets", ["pts", "reb", "ast", "pra", "fg3m", "blk", "stl"],
        default=["pts", "reb", "ast", "pra", "fg3m"],
        key=f"ls_mkts_{min_price}",
    )
    sort_by = c3.selectbox("Sort by", ["Edge", "Win %", "Best Price"],
                           key=f"ls_sort_{min_price}")

    view = edges_df[
        (edges_df["best_price"] >= min_price)
        & (edges_df["edge"] >= min_edge)
        & (edges_df["market_base"].isin(market_filter))
    ].copy()

    if sort_by == "Edge":         view = view.sort_values("edge", ascending=False)
    elif sort_by == "Win %":      view = view.sort_values("model_prob", ascending=False)
    else:                         view = view.sort_values("best_price", ascending=False)

    st.markdown(f"**{len(view)} picks ≥ {min_price:+d}** "
                f"· {(view['edge']>=0.10).sum()} with ≥10% edge "
                f"· {(view['is_alt']==True).sum()} alt lines")

    # Calibration warning — longshots are where model overshoot bites hardest
    st.warning(
        "⚠️ **Longshot caveat:** at +400 / +500 odds, the implied probability is "
        "≤20% / 17%. Model overshoots — even after calibration — can manufacture "
        "fake edges in this zone. Cross-check against teammate-out / Game-7 / "
        "matchup signals before sizing up.",
        icon="🚨",
    )

    if view.empty:
        st.info("No picks meeting filters at this odds threshold.")
        return

    for _, r in view.head(40).iterrows():
        tier_class = "nba-card-strong" if r["edge"] >= 0.10 else "nba-card-soft"
        tag = '<span class="tag-alt">🪜 ALT</span>' if r.get("is_alt") else '<span class="tag-main">MAIN</span>'
        # Implied % for context
        ml = float(r["best_price"])
        implied = (100/(ml+100) if ml > 0 else abs(ml)/(abs(ml)+100)) * 100
        st.markdown(f"""
        <div class="nba-card {tier_class}">
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:14px;">
            <div style="flex:1; min-width:260px;">
              <div style="font-size:15px; font-weight:700; color:#E2E2EE;">
                {r['player_name']} <span style="color:#888;font-weight:400;">({r['team_abbr']})</span>
              </div>
              <div style="font-size:13px; color:#B8B8D4; margin-top:3px;">
                {r['over_under']} <strong>{r['line']}</strong> {_market_label(r['market_base'])} &nbsp; {tag}
              </div>
            </div>
            <div style="display:flex; gap:18px; flex-wrap:wrap;">
              <div class="stat-block"><div class="stat-label">EDGE</div>{_edge_badge(r['edge'])}</div>
              <div class="stat-block"><div class="stat-label">WIN %</div>
                <div class="stat-value" style="color:#00D4FF;">{r['model_prob']*100:.1f}%</div>
                <div class="stat-sub">implied {implied:.1f}%</div></div>
              <div class="stat-block"><div class="stat-label">PRICE</div>
                <div class="stat-value" style="font-size:18px;">{fmt_odds(r['best_price'])}</div>
                <div class="stat-sub">{_book_short(r['best_book'])}</div></div>
              <div class="stat-block"><div class="stat-label">KELLY ¼</div>
                <div class="stat-value" style="color:#00FF88;">${r['kelly_quarter']:.0f}</div></div>
            </div>
          </div>
        </div>
        """, unsafe_allow_html=True)


with tabs[3]:
    _render_longshot_tab(
        min_price=400, label="🎰 +400 Longshots",
        threshold_text="Picks priced at +400 or higher (≤20% implied). "
                       "Quarter-Kelly suggested — variance is brutal in this zone.",
    )

# ── TAB 4 — 🚀 +500 Moonshots ─────────────────────────────────────────────────
with tabs[4]:
    _render_longshot_tab(
        min_price=500, label="🚀 +500 Moonshots",
        threshold_text="Picks priced at +500 or higher (≤17% implied). "
                       "Hit rate is low even when right; size for survival, not cashout.",
    )

# ── TAB 5 — 🎯 Alt Line Builder ──────────────────────────────────────────────
with tabs[5]:
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

        # Build pool: only OVERs (alt ladders are over-only on most books).
        # IMPORTANT: nba_prop_edges only stores ONE row per (player, market, line, O/U)
        # using best_price across books — meaning the per-leg "book" is whichever
        # book had the best number. To run same-book parlays we need to re-pull
        # the underlying nba_props rows so we can see each book's price for each
        # qualifying leg.
        edge_pool = edges_df[
            (edges_df["over_under"] == "Over")
            & (edges_df["model_prob"] >= min_prob)
            & (edges_df["edge"] >= min_edge_floor)
            & (edges_df["best_price"] >= min_price)
            & (edges_df["market_base"].isin(market_pick))
        ].copy()
        # Per-player dedupe: keep the LOWEST line for each (player, market_base)
        # so we never stack a player's own ladder.
        edge_pool = edge_pool.sort_values(["player_name", "market_base", "line"])
        edge_pool = edge_pool.drop_duplicates(subset=["player_name", "market_base"], keep="first")

        # Pull the per-book props rows for these qualifying (player, market, line)
        # combinations — gives us a real per-book price grid.
        @st.cache_data(ttl=60)
        def _load_props_for_combos(game_ids: tuple) -> pd.DataFrame:
            if not game_ids:
                return pd.DataFrame()
            return fetch_in("nba_props", "game_id", list(game_ids))

        props_pool = _load_props_for_combos(tuple(games_df["id"].tolist()) if not games_df.empty else tuple())

        # Build per-book pool: each row is (player, market, line, book, price, model_prob)
        # Restrict to globally-selected books only — empty multiselect = all
        per_book_legs: dict[str, list[Leg]] = {b: [] for b in selected_books}
        if not props_pool.empty:
            props_pool["player_name_norm"] = props_pool["player_name"].apply(normalize_player_name)
            # Map (player_norm, line, market_base) → model_prob from edge_pool
            edge_pool["_market_real"] = edge_pool["market_base"]
            edge_pool["player_name_norm"] = edge_pool["player_name"].apply(normalize_player_name)
            edge_index = edge_pool.set_index(
                ["player_name_norm", "_market_real", "line"]
            )[["model_prob", "team_abbr", "game_id"]].to_dict(orient="index")

            # Map Odds API market keys → our internal market_base
            from models.edge_engine import MARKET_TO_STAT
            for _, p in props_pool.iterrows():
                if p["over_under"] != "Over":
                    continue
                stat_info = MARKET_TO_STAT.get(p["market"])
                if not stat_info:
                    continue
                base, _is_alt = stat_info
                if base not in market_pick:
                    continue
                key = (p["player_name_norm"], base, float(p["line"]))
                meta = edge_index.get(key)
                if not meta:
                    continue
                price = int(p["price"]) if pd.notna(p["price"]) else None
                if price is None or price < min_price:
                    continue
                book = str(p["book"])
                if book not in per_book_legs:
                    continue
                per_book_legs[book].append(Leg(
                    player_name=p["player_name"],
                    team_abbr=str(meta["team_abbr"]),
                    market_base=base,
                    line=float(p["line"]),
                    over_under="Over",
                    price=price,
                    model_prob=float(meta["model_prob"]),
                    game_id=str(meta["game_id"]),
                    book=book,
                ))

        # Per-book per-player dedupe — keep the LOWEST line per (player, market) per book
        for book in per_book_legs:
            seen: dict[tuple, Leg] = {}
            # Sort by line asc, so first-seen = lowest line
            per_book_legs[book].sort(key=lambda L: (L.player_name.lower(), L.market_base, L.line))
            for L in per_book_legs[book]:
                k = (L.player_name.lower(), L.market_base)
                if k not in seen:
                    seen[k] = L
            per_book_legs[book] = sorted(seen.values(), key=lambda L: L.model_prob, reverse=True)

        total_legs_all_books = sum(len(v) for v in per_book_legs.values())
        per_book_summary = " · ".join(f"**{_book_short(b)}**: {len(v)}" for b, v in per_book_legs.items())
        st.markdown(f"**{total_legs_all_books} qualifying legs total** ({per_book_summary})")
        st.caption("Parlays below are filtered to **single-book legs only** — what you actually need to place the bet at one sportsbook.")

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

        # Helper: generate same-book combos across all books, return ranked list
        def _same_book_combos(per_book: dict[str, list[Leg]], n_legs_req: int,
                              filter_fn=None) -> list:
            """Run combinations within each book separately. filter_fn(parlay) → bool."""
            all_combos = []
            for book, legs in per_book.items():
                if not legs:
                    continue
                # Trim to top 40 by model_prob per book to keep combinatorics tractable
                book_legs = legs[:40]
                if len(book_legs) < n_legs_req:
                    continue
                unique_games_b = len({L.game_id for L in book_legs})
                eff = min(n_legs_req, max(2, unique_games_b))
                allow_same_game_b = n_legs_req > unique_games_b
                for combo in combinations(book_legs, eff):
                    games_in = [L.game_id for L in combo]
                    players_in = [L.player_name.lower() for L in combo]
                    if len(set(players_in)) < eff:
                        continue
                    if not allow_same_game_b and len(set(games_in)) < eff:
                        continue
                    if allow_same_game_b and any(games_in.count(g) > 2 for g in set(games_in)):
                        continue
                    p = build_parlay(list(combo))
                    if filter_fn and not filter_fn(p):
                        continue
                    all_combos.append(p)
            return all_combos

        if total_legs_all_books == 0:
            st.info("No legs match the filters. Try lowering Min win % or expanding markets.")
        else:
            # ── AUTO-SUGGESTED PARLAYS — HIGH-PROB, SAME BOOK ─────────────────
            st.markdown(f"### 🟢 Best {int(min_prob*100)}%+ legs — by win likelihood")
            st.caption(f"Highest combined-win-% parlays where every leg has model probability ≥ {int(min_prob*100)}% "
                       f"and price ≥ {min_price:+d}. **All legs from the same book** so you can place the parlay as one slip.")

            combos = _same_book_combos(per_book_legs, n_legs)
            combos.sort(key=lambda p: p.adjusted_prob, reverse=True)
            top = combos[:5]

            def _book_pill(book: str) -> str:
                colors = {"draftkings": "#00FF88", "fanduel": "#3B82F6", "fanatics": "#9D4EDD"}
                bg = colors.get(book, "#888")
                return (f"<span style='background:{bg}20;color:{bg};border:1px solid {bg}60;"
                        f"padding:3px 10px;border-radius:6px;font-size:11px;font-weight:700;'>"
                        f"📍 {_book_short(book).upper()}</span>")

            def _render_parlay_card(p, badge: str, border_class: str, payout_color: str = None):
                book = p.legs[0].book   # all legs same book by construction
                legs_html = "".join(
                    f"<div style='display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px dashed #1E1E30;'>"
                    f"<div><strong>{L.player_name}</strong> "
                    f"<span style='color:#888'>({L.team_abbr})</span> · "
                    f"Over <strong>{L.line}</strong> {_market_short(L.market_base)}</div>"
                    f"<div style='font-family:Space Mono,monospace; color:#B8B8D4;'>"
                    f"{L.model_prob*100:.1f}% · {fmt_odds(L.price)}</div>"
                    f"</div>"
                    for L in p.legs
                )
                payout_style = f"color:{payout_color};" if payout_color else ""
                st.markdown(f"""
                <div class="nba-card {border_class}">
                  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:10px;">
                    <div style="display:flex; align-items:center; gap:10px;">
                      <span class="tag-hammer">{badge}</span>
                      {_book_pill(book)}
                    </div>
                    <div style="display:flex; gap:20px;">
                      <div class="stat-block"><div class="stat-label">COMBINED WIN %</div>
                        <div class="stat-value" style="color:#00D4FF; font-size:18px;">{p.adjusted_prob*100:.1f}%</div></div>
                      <div class="stat-block"><div class="stat-label">PAYOUT</div>
                        <div class="stat-value" style="font-size:18px;{payout_style}">{fmt_odds(p.american_odds)}</div></div>
                      <div class="stat-block"><div class="stat-label">EDGE</div>
                        <div class="stat-value" style="color:{'#00FF88' if p.edge>0 else '#FF6B35'};">{p.edge*100:+.1f}%</div></div>
                      <div class="stat-block"><div class="stat-label">LEGS</div>
                        <div class="stat-value" style="font-size:18px;">{len(p.legs)}</div></div>
                    </div>
                  </div>
                  {legs_html}
                </div>
                """, unsafe_allow_html=True)

            if not top:
                st.info(
                    f"No qualifying same-book parlays. Try lowering Min win % "
                    f"(currently {min_prob*100:.0f}%), reducing Legs (currently {n_legs}), "
                    f"or expanding markets. Pool: {per_book_summary}."
                )
            else:
                for i, p in enumerate(top):
                    badge = "🔨 HAMMER" if i == 0 else f"#{i+1}"
                    border = "nba-card-hammer" if i == 0 else "nba-card-strong"
                    _render_parlay_card(p, badge, border)

            # ── 💰 BEST +300 OR BETTER PARLAY ─────────────────────────────────
            st.markdown("---")
            st.markdown("### 💰 Best +300 or better payout")
            st.caption("Highest combined-win-% parlay whose payout pays at least 3-to-1. **All legs from the same book** so you can place it as one slip. Uses a slightly wider pool to find more combos.")

            # Per-book pools for the +300 hunt — slightly wider min_prob threshold
            wider_min_prob = max(0.55, min_prob - 0.10)
            big_per_book: dict[str, list[Leg]] = {}
            for book, legs in per_book_legs.items():
                big_per_book[book] = [L for L in legs if L.model_prob >= wider_min_prob]

            big_combos = []
            for n in (2, 3, 4):
                book_combos = _same_book_combos(big_per_book, n,
                                                filter_fn=lambda p: p.american_odds >= 300)
                big_combos.extend(book_combos)

            big_combos.sort(key=lambda p: p.adjusted_prob, reverse=True)
            top_300 = big_combos[:5]

            if not top_300:
                st.info("No qualifying +300 parlays at current filter settings. Try lowering Min win % "
                        "or expanding markets to widen the pool.")
            else:
                for i, p in enumerate(top_300):
                    badge = "💰 BEST 3+ TO 1" if i == 0 else f"#{i+1}"
                    border = "nba-card-hammer" if i == 0 else "nba-card-soft"
                    _render_parlay_card(p, badge, border, payout_color="#00FF88")

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


# ── TAB 6 — 🪜 Player Intel (alt ladder for one player) ───────────────────────
with tabs[6]:
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


# ── TAB 7 — 📓 Bet Journal ────────────────────────────────────────────────────
with tabs[7]:
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

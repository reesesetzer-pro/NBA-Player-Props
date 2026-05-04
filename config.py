"""
config.py — central configuration for NBA Player Prop Model.

Loads secrets from .env (local) or Streamlit secrets (cloud), defines NBA
season detection, restricts books to DraftKings + FanDuel, and lists every
prop market we'll request from The Odds API.
"""
from __future__ import annotations
import os
from datetime import date
from dotenv import load_dotenv

load_dotenv()


# ── Secrets resolution (local .env → Streamlit secrets) ───────────────────────
def _secret(key: str, default: str = "") -> str:
    """Resolve a secret from Streamlit Cloud first (st.secrets), then env vars.

    Streamlit Cloud injects secrets into st.secrets at app startup; checking
    that source first avoids a cold-start race where os.getenv() runs before
    Streamlit has propagated the secrets to the process environment.
    """
    try:
        import streamlit as st
        if hasattr(st, "secrets"):
            try:
                if key in st.secrets:
                    return str(st.secrets[key])
            except Exception:
                pass
    except Exception:
        pass
    return os.getenv(key, default)


ODDS_API_KEY  = _secret("ODDS_API_KEY")
SUPABASE_URL  = _secret("SUPABASE_URL")
SUPABASE_KEY  = _secret("SUPABASE_KEY")
KELLY_BANKROLL = float(_secret("KELLY_BANKROLL", "1000"))


# ── NBA season detection ──────────────────────────────────────────────────────
# NBA season label = "YYYY-YY" where YYYY is the year the season started.
# Season runs Oct → Jun, so games in Jan-Jun belong to the season that started
# the prior October.
def _current_season() -> str:
    today = date.today()
    start_year = today.year if today.month >= 10 else today.year - 1
    return f"{start_year}-{str(start_year + 1)[-2:]}"


CURRENT_SEASON = _secret("NBA_SEASON", _current_season())  # "2025-26"
PRIOR_SEASON   = (
    f"{int(CURRENT_SEASON[:4]) - 1}-{str(int(CURRENT_SEASON[:4]))[-2:]}"
)


# ── The Odds API ──────────────────────────────────────────────────────────────
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
NBA_SPORT_KEY = "basketball_nba"

# Restricted to DraftKings + FanDuel — both sharp on player props, used as
# no-vig consensus and as the only books we'll log bets at.
BOOKS = ["draftkings", "fanduel", "fanatics"]

# Game-level markets (bulk endpoint supports h2h/spreads/totals)
MARKETS_GAME = ["h2h", "spreads", "totals"]

# Player props — main lines (event-specific endpoint, one market per request)
MARKETS_PROPS_MAIN = [
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_threes",
    "player_blocks",
    "player_steals",
    "player_points_rebounds_assists",  # PRA combo
    "player_double_double",
]

# Player props — alternate ladders (the killer feature for edge hunting).
# Each market returns multiple lines per player, all with their own price.
MARKETS_PROPS_ALT = [
    "player_points_alternate",
    "player_rebounds_alternate",
    "player_assists_alternate",
    "player_threes_alternate",
    "player_points_rebounds_assists_alternate",
]

ALL_PROP_MARKETS = MARKETS_PROPS_MAIN + MARKETS_PROPS_ALT

ODDS_FORMAT = "american"
REGIONS     = "us"


# ── NBA Stats API ─────────────────────────────────────────────────────────────
NBA_STATS_BASE = "https://stats.nba.com/stats"

# Position groups — used for opponent-vs-position defensive matchup adjustments.
# NBA Stats reports per-position defense; we map players to one of these.
POSITION_GROUPS = ["PG", "SG", "SF", "PF", "C"]


# ── Edge thresholds ───────────────────────────────────────────────────────────
EDGE_SOFT_THRESHOLD   = 0.04   # 4% — surfaced in dashboard
EDGE_STRONG_THRESHOLD = 0.07   # 7% — auto-flagged
MIN_MODEL_PROB        = 0.55   # don't suggest a leg below this win %
PARLAY_MAX_LEGS       = 5

# Alt-ladder one-side vig assumption. Books post Over-only deep tails and we
# need to strip an assumed hold to estimate true price. 10% is a realistic
# midpoint for DK/FD long-tail props (5-15% typical, 20%+ on the deepest tails).
# Conservative bias: underestimating vig understates real edge.
ALT_LADDER_VIG_DEFAULT = 0.10


# ── Distribution fitting ──────────────────────────────────────────────────────
DIST_ROLLING_WINDOW = 15        # last N games for recent-form mean
DIST_SEASON_WEIGHT  = 0.40      # weight on full-season μ
DIST_RECENT_WEIGHT  = 0.60      # weight on rolling-15 μ — recency bias
MIN_GAMES_FOR_FIT   = 5         # below this, fall back to season avg only


# ── Sync intervals (seconds) ──────────────────────────────────────────────────
SYNC_PLAYER_LOGS_INTERVAL = 86400   # 24h — only changes after games
SYNC_TEAM_STATS_INTERVAL  = 43200   # 12h
SYNC_INJURIES_INTERVAL    = 600     # 10m
SYNC_LINEUPS_INTERVAL     = 1200    # 20m
SYNC_ODDS_INTERVAL        = 1800    # 30m
SYNC_PROPS_INTERVAL       = 900     # 15m — alt lines move faster


# ── Playoff modifiers ─────────────────────────────────────────────────────────
# Series fatigue — additive penalty to μ for the team coming off a long series
PLAYOFF_SERIES_FATIGUE_PENALTY = 0.03   # 3% scoring drop after 6+ game series
PLAYOFF_STAR_MIN_BOOST         = 0.10   # 10% mean boost for stars (≥30 mpg)
PLAYOFF_BENCH_MIN_PENALTY      = 0.30   # 30% mean penalty for bench (<18 mpg)

# Game 7 / elimination — stack on TOP of base playoff multiplier.
# Empirical NBA Game 7 effects (rotations contract from 9-10 to 7-8):
#   Stars (≥30 mpg) play even more (less rest, fewer subs)
#   Bench (<18 mpg) often glued to the floor — 7th-9th men barely play
#   Mid-rotation: roughly neutral
PLAYOFF_GAME7_STAR_BOOST          = 0.05   # +5% additional on top of star_min_boost
PLAYOFF_GAME7_BENCH_PENALTY       = 0.10   # +10pp additional bench penalty
# Elimination game (one team at 3 wins, not 3-3) — same direction, smaller magnitude
PLAYOFF_ELIM_STAR_BOOST           = 0.03
PLAYOFF_ELIM_BENCH_PENALTY        = 0.05


# ── NBA team metadata ─────────────────────────────────────────────────────────
NBA_TEAMS = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}
TEAM_NAME_TO_ABBR = {v: k for k, v in NBA_TEAMS.items()}

# NBA Stats team_id → abbreviation (used to translate playoff_series rows
# whose `team1_abbr`/`team2_abbr` columns actually store team IDs).
TEAM_ID_TO_ABBR = {
    1610612737: "ATL", 1610612738: "BOS", 1610612751: "BKN",
    1610612766: "CHA", 1610612741: "CHI", 1610612739: "CLE",
    1610612742: "DAL", 1610612743: "DEN", 1610612765: "DET",
    1610612744: "GSW", 1610612745: "HOU", 1610612754: "IND",
    1610612746: "LAC", 1610612747: "LAL", 1610612763: "MEM",
    1610612748: "MIA", 1610612749: "MIL", 1610612750: "MIN",
    1610612740: "NOP", 1610612752: "NYK", 1610612760: "OKC",
    1610612753: "ORL", 1610612755: "PHI", 1610612756: "PHX",
    1610612757: "POR", 1610612758: "SAC", 1610612759: "SAS",
    1610612761: "TOR", 1610612762: "UTA", 1610612764: "WAS",
}
TEAM_ABBR_TO_ID = {v: k for k, v in TEAM_ID_TO_ABBR.items()}

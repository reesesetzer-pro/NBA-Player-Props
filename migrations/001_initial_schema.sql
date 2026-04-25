-- NBA Player Prop Model — Supabase schema
-- All tables prefixed `nba_` to avoid collision with NHL/Golf in shared project.
-- Run this once in Supabase SQL Editor before first sync.

-- ── Schedule + games ──────────────────────────────────────────────────────────
create table if not exists nba_games (
    id              text primary key,           -- NBA game_id (10-digit string)
    game_date       date,
    season          text,                       -- "2025-26"
    season_type     text,                       -- "regular" | "playoffs"
    home_abbr       text,
    away_abbr       text,
    home_team       text,
    away_team       text,
    commence_time   timestamptz,
    odds_event_id   text,                       -- The Odds API event id (joins nba_odds/props)
    game_state      text,                       -- "scheduled" | "live" | "final"
    is_b2b_home     boolean,
    is_b2b_away     boolean,
    rest_days_home  integer,
    rest_days_away  integer,
    updated_at      timestamptz default now()
);
create index if not exists nba_games_date_idx on nba_games(game_date);
create index if not exists nba_games_odds_event_idx on nba_games(odds_event_id);

-- ── Player game logs (the distribution input) ─────────────────────────────────
create table if not exists nba_player_logs (
    id              text primary key,
    game_id         text,
    game_date       date,
    season          text,
    season_type     text,
    player_id       integer,
    player_name     text,
    team_abbr       text,
    opponent_abbr   text,
    is_home         boolean,
    minutes         numeric,
    pts             integer,
    reb             integer,
    ast             integer,
    fg3m            integer,
    blk             integer,
    stl             integer,
    tov             integer,
    pra             integer,
    plus_minus      integer,
    updated_at      timestamptz default now()
);
create index if not exists nba_player_logs_player_idx on nba_player_logs(player_id);
create index if not exists nba_player_logs_date_idx on nba_player_logs(game_date);

-- ── Team-level stats (matchup multiplier source) ─────────────────────────────
create table if not exists nba_team_stats (
    id              text primary key,           -- md5(team_abbr|season|season_type|measure_type)
    team_abbr       text,
    season          text,
    season_type     text,
    measure_type    text,                       -- "Base" | "Advanced" | "Defense"
    games_played    integer,
    pace            numeric,
    off_rating      numeric,
    def_rating      numeric,
    net_rating      numeric,
    opp_pts_per100  numeric,
    opp_efg_pct     numeric,
    opp_tov_pct     numeric,
    opp_oreb_pct    numeric,
    opp_ftr         numeric,
    updated_at      timestamptz default now()
);

-- ── Position-vs-defense splits ───────────────────────────────────────────────
-- One row per (defending team, opponent position, stat). Aggregated nightly
-- from player logs: how many points/rebs/asts each team gives up to PGs/SGs/...
create table if not exists nba_pos_def (
    id              text primary key,           -- md5(team|pos|stat|season)
    team_abbr       text,                       -- the defending team
    opp_position    text,                       -- "PG" | "SG" | "SF" | "PF" | "C"
    stat            text,                       -- "pts" | "reb" | "ast" | "pra" | "fg3m"
    season          text,
    season_type     text,
    avg_allowed     numeric,
    games_n         integer,
    league_avg      numeric,
    rank            integer,                    -- 1=best defense, 30=worst
    multiplier      numeric,                    -- avg_allowed / league_avg (1.0 = neutral)
    updated_at      timestamptz default now()
);
create index if not exists nba_pos_def_team_idx on nba_pos_def(team_abbr);

-- ── Per-defender matchup data (Tier 1) ───────────────────────────────────────
create table if not exists nba_defender_stats (
    id              text primary key,           -- md5(player_id|season|season_type)
    player_id       integer,
    player_name     text,
    team_abbr       text,
    position        text,
    season          text,
    season_type     text,
    matchup_min     numeric,                    -- avg defended minutes per game
    pts_allowed_per_chance  numeric,            -- normalized
    fg_pct_allowed  numeric,
    fg3_pct_allowed numeric,
    plus_minus      numeric,
    updated_at      timestamptz default now()
);

-- ── Player splits (Tier 2 — home/road, B2B, vs-opponent) ─────────────────────
create table if not exists nba_player_splits (
    id              text primary key,           -- md5(player_id|split_type|split_value|season)
    player_id       integer,
    player_name     text,
    season          text,
    season_type     text,
    split_type      text,                       -- "home_road" | "b2b" | "rest_days" | "vs_team"
    split_value     text,                       -- "home" | "0_days" | "vs_BOS" etc
    games_n         integer,
    pts_avg         numeric,
    reb_avg         numeric,
    ast_avg         numeric,
    minutes_avg     numeric,
    updated_at      timestamptz default now()
);

-- ── Advanced per-game stats (Tier 2) ─────────────────────────────────────────
create table if not exists nba_player_advanced (
    id              text primary key,           -- md5(game_id|player_id)
    game_id         text,
    game_date       date,
    season          text,
    season_type     text,
    player_id       integer,
    player_name     text,
    team_abbr       text,
    minutes         numeric,
    usage_pct       numeric,
    true_shooting_pct numeric,
    eff_fg_pct      numeric,
    ast_pct         numeric,
    reb_pct         numeric,
    off_rating      numeric,
    def_rating      numeric,
    pace            numeric,
    updated_at      timestamptz default now()
);

-- ── Lineups (Tier 2) ─────────────────────────────────────────────────────────
create table if not exists nba_lineups (
    id              text primary key,           -- md5(team|date|player_id)
    team_abbr       text,
    game_date       date,
    player_id       integer,
    player_name     text,
    role            text,                       -- "starter" | "rotation" | "bench"
    projected_min   numeric,
    confirmed       boolean,
    source          text,                       -- "nba_api" | "rotowire" | "model"
    updated_at      timestamptz default now()
);

-- ── Injuries (Tier 2) ────────────────────────────────────────────────────────
create table if not exists nba_injuries (
    id              text primary key,           -- md5(player_name|team_abbr)
    player_id       integer,
    player_name     text,
    team_abbr       text,
    status          text,                       -- "out" | "doubtful" | "questionable" | "probable" | "day-to-day"
    notes           text,
    minutes_impact  numeric,                    -- minutes redistributed when this player is out
    updated_at      timestamptz default now()
);

-- ── Player tracking (Tier 3) ─────────────────────────────────────────────────
create table if not exists nba_player_tracking (
    id              text primary key,           -- md5(player_id|season|metric)
    player_id       integer,
    player_name     text,
    team_abbr       text,
    season          text,
    season_type     text,
    metric          text,                       -- "shots_zone" | "drives" | "catch_shoot" | "pullup"
    value           numeric,
    rank            integer,
    updated_at      timestamptz default now()
);

-- ── Playoff series (Tier 3) ──────────────────────────────────────────────────
create table if not exists nba_playoff_series (
    id              text primary key,           -- md5(season|round|series_letter)
    season          text,
    round_number    integer,                    -- 1 | 2 | 3 (conf finals) | 4 (finals)
    round_name      text,
    series_letter   text,
    team1_abbr      text,
    team2_abbr      text,
    team1_wins      integer,
    team2_wins      integer,
    games_played    integer,
    is_complete     boolean,
    is_elimination  boolean,                    -- next game eliminates someone
    is_game7        boolean,
    series_fatigue_team1 numeric,               -- 0.0 → 1.0
    series_fatigue_team2 numeric,
    updated_at      timestamptz default now()
);

-- ── Odds (game-level) ────────────────────────────────────────────────────────
create table if not exists nba_odds (
    id              text primary key,
    game_id         text,                       -- nba_games.id (joined via odds_event_id)
    odds_event_id   text,
    book            text,                       -- "draftkings" | "fanduel"
    market          text,                       -- "h2h" | "spreads" | "totals"
    outcome         text,
    price           integer,
    point           numeric,
    updated_at      timestamptz default now()
);

-- ── Props (the alt-ladder source of truth) ───────────────────────────────────
create table if not exists nba_props (
    id              text primary key,           -- md5(game_id|book|market|player|line|over_under)
    game_id         text,
    odds_event_id   text,
    book            text,                       -- "draftkings" | "fanduel"
    market          text,                       -- "player_points" | "alternate_player_points" | etc
    player_name     text,
    player_name_norm text,
    team_abbr       text,
    line            numeric,                    -- the prop line (.5 boundaries)
    over_under      text,                       -- "Over" | "Under"
    price           integer,                    -- American odds
    updated_at      timestamptz default now()
);
create index if not exists nba_props_game_idx on nba_props(game_id);
create index if not exists nba_props_player_idx on nba_props(player_name_norm);

-- ── Props history (for CLV) ──────────────────────────────────────────────────
create table if not exists nba_props_history (
    id              bigserial primary key,
    game_id         text,
    book            text,
    market          text,
    player_name_norm text,
    line            numeric,
    over_under      text,
    price           integer,
    snapshot_at     timestamptz default now()
);
create index if not exists nba_props_history_game_idx on nba_props_history(game_id, snapshot_at);

-- ── Prop edges (model output) ────────────────────────────────────────────────
create table if not exists nba_prop_edges (
    id              text primary key,           -- md5(game_id|player|market_base|line|over_under)
    game_id         text,
    game_date       date,
    player_name     text,
    player_name_norm text,
    team_abbr       text,
    market_base     text,                       -- "pts" | "reb" | "ast" | "pra" | "fg3m"
    line            numeric,
    over_under      text,
    -- Best price across DK + FD
    best_price      integer,
    best_book       text,
    -- Model output
    model_prob      numeric,
    market_prob_novig numeric,
    edge            numeric,                    -- model_prob - market_prob_novig
    -- Sizing
    kelly_full      numeric,
    kelly_half      numeric,
    kelly_quarter   numeric,
    -- Distribution snapshot
    fitted_mu       numeric,
    fitted_alpha    numeric,
    -- Adjustments applied
    matchup_mult    numeric,
    rest_mult       numeric,
    playoff_mult    numeric,
    injury_mult     numeric,
    -- Bookkeeping
    is_alt          boolean,
    updated_at      timestamptz default now()
);
create index if not exists nba_prop_edges_date_idx on nba_prop_edges(game_date);
create index if not exists nba_prop_edges_edge_idx on nba_prop_edges(edge desc);

-- ── Bet journal ──────────────────────────────────────────────────────────────
create table if not exists nba_bets (
    id              bigserial primary key,
    placed_at       timestamptz default now(),
    game_id         text,
    game_date       date,
    player_name     text,
    market_base     text,
    line            numeric,
    over_under      text,
    book            text,
    price           integer,
    stake           numeric,
    to_win          numeric,
    model_prob      numeric,
    edge_at_bet     numeric,
    closing_price   integer,                    -- captured at game time
    closing_prob    numeric,
    clv             numeric,                    -- model_prob - closing_prob_novig
    result          text,                       -- "Pending" | "Win" | "Loss" | "Push"
    profit_loss     numeric,
    notes           text
);

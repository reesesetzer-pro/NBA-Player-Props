-- 002_grading.sql — historical pick log + grading + calibration
-- Run after 001_initial_schema.sql

-- ── Pick history (append-only — every priced edge becomes a "shadow bet") ─────
-- nba_prop_edges is refreshed each sync (cleared + rewritten with today's data)
-- so it's not a historical record. nba_prop_picks captures every priced edge
-- at the moment it was generated, so we can grade every prop the model
-- recommended after the fact.
create table if not exists nba_prop_picks (
    id              text primary key,           -- md5(game_id|player|market|line|ou|sync_date)
    sync_date       date,
    game_id         text,
    game_date       date,
    player_name     text,
    player_name_norm text,
    team_abbr       text,
    opponent_abbr   text,
    market_base     text,                       -- "pts" | "reb" | "ast" | "pra" | "fg3m"
    line            numeric,
    over_under      text,                       -- "Over" | "Under"
    book            text,
    price           integer,
    model_prob      numeric,
    market_prob_novig numeric,
    edge            numeric,
    is_alt          boolean,
    -- Grading
    result          text,                       -- "Pending" | "Win" | "Loss" | "Push"
    actual_value    numeric,                    -- the player's actual stat
    profit_loss     numeric,                    -- $ P/L assuming $1 stake
    graded_at       timestamptz,
    created_at      timestamptz default now()
);
create index if not exists nba_prop_picks_date_idx on nba_prop_picks(game_date);
create index if not exists nba_prop_picks_result_idx on nba_prop_picks(result);
create index if not exists nba_prop_picks_market_idx on nba_prop_picks(market_base);

-- ── Calibration tables ───────────────────────────────────────────────────────
-- Per (market_base, probability_bucket) actual hit rate. Written by the
-- calibration script after grading runs. Read by edge_engine to adjust
-- model_prob before display.
create table if not exists nba_calibration (
    id              text primary key,           -- md5(market_base|prob_bucket)
    market_base     text,
    prob_bucket     text,                       -- "55-60%" | "60-70%" | "70-80%" | "80%+"
    n_settled       integer,
    n_wins          integer,
    avg_predicted   numeric,
    actual_hit_rate numeric,
    overconfidence  numeric,                    -- predicted - actual (positive = model too confident)
    updated_at      timestamptz default now()
);

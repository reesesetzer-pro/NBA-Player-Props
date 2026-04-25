# NBA Player Prop Model

Edge-hunting on NBA player props (DraftKings + FanDuel). Built around the alt-ladder pricing edge: fit one negative-binomial distribution per (player, stat), then price every alternate line in one shot.

## Phase plan

- **Phase 1** (in progress): alt-ladder pricing + parlay combinator
- **Phase 2**: matchup adjustments (opponent DRtg, position-vs-defense splits)
- **Phase 3**: rest / playoff context (series fatigue, rotation tightening, star minute boosts)

## Local setup

```bash
cd "Betting Models/NBA_Player_Props"
python3 -m venv .venv && source .venv/bin/activate     # or reuse the parent shared venv
pip install -r requirements.txt
cp .env.example .env                                    # fill ODDS_API_KEY, SUPABASE_URL/KEY
python3 sync/player_logs_sync.py --mode backfill        # 2 seasons of player logs
pytest tests/                                           # verify the math
streamlit run app.py                                    # dashboard (when built)
```

## Streamlit Cloud

Pushes to `main` deploy automatically. Cloud needs the same secrets in **Settings → Secrets**:

```toml
ODDS_API_KEY  = "..."
SUPABASE_URL  = "..."
SUPABASE_KEY  = "..."
KELLY_BANKROLL = 1000
NBA_SEASON    = "2025-26"   # optional override
```

## Architecture

| Layer | Responsibility |
|---|---|
| `sync/` | Pull from NBA Stats API + The Odds API → write to Supabase |
| `models/distribution.py` | Fit NegBin per (player, stat) — the foundation |
| `models/edge_engine.py` | Price every alt line, compare to book, write `nba_prop_edges` |
| `models/parlay.py` | Combine legs with correlation adjustment, rank by EV |
| `models/adjustments.py` | Multipliers on μ for matchup, rest, playoff context |
| `app.py` | Streamlit dashboard |

## Supabase tables

All NBA tables prefixed `nba_` to avoid collision with NHL/Golf in the shared project.

- `nba_player_logs` — per-game stat lines (feeds distribution)
- `nba_games` — schedule + opponent + game_type
- `nba_team_stats` — DRtg, ORtg, pace, position-vs-defense
- `nba_props` — current alt-ladder offerings from DK + FD
- `nba_props_history` — line snapshots for CLV
- `nba_prop_edges` — model output (model_prob, edge, kelly per line)
- `nba_injuries` — active list + minutes-impact
- `nba_playoff_series` — round, game#, fatigue
- `nba_bets` — Bet Journal

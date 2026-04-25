"""
sync/scheduler.py — APScheduler wiring all NBA syncs.

Run from the project root:
    python -m sync.scheduler

Cadence is tuned for NBA's evening-heavy schedule (most games tip off
7-10:30 PM ET) plus once-daily refreshes after games complete (~2 AM ET).
"""
from __future__ import annotations
import logging
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import (
    SYNC_PLAYER_LOGS_INTERVAL, SYNC_TEAM_STATS_INTERVAL,
    SYNC_INJURIES_INTERVAL, SYNC_LINEUPS_INTERVAL,
    SYNC_ODDS_INTERVAL, SYNC_PROPS_INTERVAL,
)

from sync.player_logs_sync import run_incremental as player_logs_inc
from sync.games_sync import run_games_sync
from sync.team_stats_sync import run_team_stats_sync
from sync.pos_def_sync import run_pos_def_sync
from sync.defender_sync import run_defender_sync
from sync.splits_sync import run_splits_sync
from sync.advanced_sync import run_advanced_sync
from sync.lineups_sync import run_lineups_sync
from sync.injuries_sync import run_injuries_sync
from sync.tracking_sync import run_tracking_sync
from sync.playoff_sync import run_playoff_sync
from sync.odds_sync import run_game_odds_sync, run_props_sync
from models.edge_engine import calculate_all_edges


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nba-scheduler")


def cold_start():
    """Run the full sync chain in dependency order."""
    log.info("─── COLD START ───")
    run_games_sync()
    run_playoff_sync()
    run_team_stats_sync()
    run_injuries_sync()
    run_lineups_sync()
    run_game_odds_sync()
    run_props_sync()
    calculate_all_edges()
    log.info("─── cold start complete ───")


def main():
    sched = BlockingScheduler(timezone="America/New_York")

    # Core: keep odds + edges fresh during action hours
    sched.add_job(run_props_sync,   IntervalTrigger(seconds=SYNC_PROPS_INTERVAL),
                  next_run_time=None)
    sched.add_job(run_game_odds_sync, IntervalTrigger(seconds=SYNC_ODDS_INTERVAL))
    sched.add_job(calculate_all_edges, IntervalTrigger(seconds=SYNC_PROPS_INTERVAL + 30))

    # Lineups + injuries (faster cadence)
    sched.add_job(run_injuries_sync, IntervalTrigger(seconds=SYNC_INJURIES_INTERVAL))
    sched.add_job(run_lineups_sync,  IntervalTrigger(seconds=SYNC_LINEUPS_INTERVAL))

    # Daily 8 AM ET — schedule + game-meta refresh
    sched.add_job(run_games_sync,   CronTrigger(hour=8, minute=0))
    sched.add_job(run_playoff_sync, CronTrigger(hour=8, minute=5))

    # Nightly 3 AM ET — recompute everything that depends on game results
    sched.add_job(player_logs_inc,    CronTrigger(hour=3, minute=0))
    sched.add_job(run_advanced_sync,  CronTrigger(hour=3, minute=15))
    sched.add_job(run_team_stats_sync, CronTrigger(hour=3, minute=30))
    sched.add_job(run_pos_def_sync,   CronTrigger(hour=3, minute=45))
    sched.add_job(run_splits_sync,    CronTrigger(hour=4, minute=0))
    sched.add_job(run_defender_sync,  CronTrigger(hour=4, minute=15))
    sched.add_job(run_tracking_sync,  CronTrigger(hour=4, minute=30))

    log.info("Scheduler started. Press Ctrl+C to stop.")
    cold_start()
    sched.start()


if __name__ == "__main__":
    main()

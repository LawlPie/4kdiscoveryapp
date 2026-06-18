"""
Background scheduler.

Uses APScheduler's `BackgroundScheduler` so the scrape job runs inside the same
container/process as the web server — no separate cron daemon required. The job
runs on a configurable interval (default 24h) and can be triggered on demand
from the web UI.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings
from .imusic_scraper import run_imusic_scrape
from .scraper import run_scrape

logger = logging.getLogger("scheduler")

_scheduler: BackgroundScheduler | None = None
# Guards against overlapping scrapes (scheduled tick + manual trigger at once).
_scrape_lock = threading.Lock()
_last_result: dict | None = None


def _job() -> None:
    """Wrapper that serialises scrapes and records the latest result."""
    global _last_result
    if not _scrape_lock.acquire(blocking=False):
        logger.info("A scrape is already running; skipping this trigger.")
        return
    try:
        result = run_scrape()
        # iMusic runs after Platekompaniet so EANs exist to compare against; a
        # failure there must not lose the Platekompaniet result.
        try:
            result["imusic"] = run_imusic_scrape()
        except Exception:
            logger.exception("iMusic scrape failed (Platekompaniet result kept)")
            result["imusic"] = {"error": True}
        result["finished_at"] = datetime.utcnow().isoformat(timespec="seconds")
        _last_result = result
    except Exception:
        logger.exception("Scheduled scrape failed")
    finally:
        _scrape_lock.release()


def trigger_scrape_async() -> bool:
    """
    Kick off a scrape in a background thread (used by the manual 'Refresh' button).
    Returns False if one is already running.
    """
    if _scrape_lock.locked():
        return False
    threading.Thread(target=_job, name="manual-scrape", daemon=True).start()
    return True


def get_last_result() -> dict | None:
    return _last_result


def start_scheduler() -> None:
    """Initialise the scheduler and register the recurring scrape job."""
    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(timezone="UTC")
    interval_seconds = int(settings.SCRAPE_INTERVAL_HOURS * 3600)
    _scheduler.add_job(
        _job,
        trigger=IntervalTrigger(seconds=interval_seconds),
        id="scrape-4k",
        name="Scrape Platekompaniet 4K deals",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        "Scheduler started — scraping every %.1f hour(s).",
        settings.SCRAPE_INTERVAL_HOURS,
    )

    if settings.SCRAPE_ON_STARTUP:
        logger.info("Running initial scrape on startup...")
        trigger_scrape_async()


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None

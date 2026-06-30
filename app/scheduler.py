"""
Background scheduler.

Uses APScheduler's `BackgroundScheduler` so the scrape job runs inside the same
container/process as the web server — no separate cron daemon required. The job
runs once a day at a fixed local time (default 05:00 Europe/Oslo) and can be
triggered on demand from the web UI. A routine container restart does NOT
re-scrape; we only scrape on startup when the stored data is stale or empty.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

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


def _data_is_stale() -> bool:
    """True if there's no scraped data yet, or it's older than the interval."""
    from .database import get_stats

    last = get_stats().get("last_updated")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - dt
    return age > timedelta(hours=settings.SCRAPE_INTERVAL_HOURS)


def start_scheduler() -> None:
    """Register the daily scrape job; scrape now only if data is stale/empty."""
    global _scheduler
    if _scheduler is not None:
        return

    try:
        _scheduler = BackgroundScheduler(timezone=settings.SCRAPE_TIMEZONE)
    except Exception:
        logger.warning(
            "Unknown timezone %r; falling back to UTC.", settings.SCRAPE_TIMEZONE
        )
        _scheduler = BackgroundScheduler(timezone="UTC")

    _scheduler.add_job(
        _job,
        trigger=CronTrigger(hour=settings.SCRAPE_HOUR, minute=settings.SCRAPE_MINUTE),
        id="scrape-4k",
        name="Daily 4K scrape",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        "Scheduler started — daily scrape at %02d:%02d %s.",
        settings.SCRAPE_HOUR, settings.SCRAPE_MINUTE, settings.SCRAPE_TIMEZONE,
    )

    # Only scrape on startup when there's nothing fresh — so restarting the
    # container does not kick off a scrape every time.
    if settings.SCRAPE_ON_STARTUP and _data_is_stale():
        logger.info("No fresh data found — running an initial scrape now.")
        trigger_scrape_async()
    else:
        logger.info("Recent data present — next scrape at the scheduled time.")


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None

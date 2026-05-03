"""Process-level APScheduler singleton.

See https://apscheduler.readthedocs.io/en/3.x/modules/schedulers/asyncio.html
for the AsyncIOScheduler API: ``start()`` is synchronous (it schedules into
the running event loop and returns immediately) and ``shutdown(wait=False)``
is also synchronous.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.jobs.retention import run_retention_job

logger = logging.getLogger(__name__)
_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    sched = AsyncIOScheduler()
    sched.add_job(
        run_retention_job,
        "cron",
        hour=3,
        minute=0,
        id="retention",
        replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    logger.info("scheduler started: retention=cron 03:00 UTC")


async def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("scheduler shut down")


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler

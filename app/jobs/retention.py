"""Nightly retention job: delete events older than per-project retention_days.

Wraps app.services.aggregation.run_retention_cron in a session+transaction
boundary so it can be scheduled by APScheduler.
"""

from __future__ import annotations

import logging

from app.core.database import get_session_factory
from app.services.aggregation import run_retention_cron

logger = logging.getLogger(__name__)


async def run_retention_job() -> int:
    factory = get_session_factory()
    async with factory() as session, session.begin():
        deleted = await run_retention_cron(session)
    logger.info("retention_job_complete", extra={"deleted_rows": deleted})
    return deleted

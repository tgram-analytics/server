"""User resolution for bot and internal-API call sites.

The default resolver returns the singleton ``User`` bootstrapped from
``ADMIN_CHAT_ID`` at startup and cached in-process. Deployments may
override the strategy by registering a custom callable via
:func:`app.extensions.register_user_resolver`; in that case the
singleton path is unused and the registered resolver is the sole source
of truth.
"""

from __future__ import annotations

import functools
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


# Module-level cache of the singleton user's UUID. Populated by ``init_bot``
# at startup. Remains ``None`` if a custom resolver is registered and the
# singleton path is unused.
_singleton_user_id: uuid.UUID | None = None


async def ensure_singleton_user(session: AsyncSession, telegram_user_id: int) -> User:
    """Return the ``User`` row for *telegram_user_id*, creating it if missing.

    Idempotent: safe to call repeatedly. Handles concurrent inserts via
    ``IntegrityError`` re-select (the ``telegram_user_id`` UNIQUE constraint
    on ``users`` makes a duplicate INSERT race-safe).

    The caller is responsible for committing the session.
    """
    # SQLAlchemy 2.0 select pattern (cf. app/services/projects.py:53-55).
    result = await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
    user = result.scalar_one_or_none()
    if user is not None:
        return user

    user = User(telegram_user_id=telegram_user_id)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        # Lost an INSERT race against another worker — roll back the failed
        # insert and re-select the row that the winner committed.
        await session.rollback()
        result = await session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        user = result.scalar_one()
    return user


async def get_current_user(session: AsyncSession, update: Update) -> User | None:
    """Resolve the ``User`` for the caller of a Telegram ``Update``.

    If a custom resolver is registered via
    :func:`app.extensions.register_user_resolver`, that callable is used
    instead. Otherwise returns the singleton bootstrapped from
    ``ADMIN_CHAT_ID`` and cached in ``_singleton_user_id``. Returns
    ``None`` if no resolver could authorize this caller — for the default
    resolver this only happens if the singleton has not been bootstrapped
    yet (defensive; should not happen in production).
    """
    from app.extensions import get_user_resolver

    resolver = get_user_resolver()
    if resolver is not None:
        return await resolver(session, update)

    # Default: singleton bootstrapped from ``ADMIN_CHAT_ID``.
    if _singleton_user_id is None:
        return None
    result = await session.execute(select(User).where(User.id == _singleton_user_id))
    return result.scalar_one_or_none()


# Type alias for a PTB handler that has been augmented with ``user`` and
# ``session`` keyword arguments by ``@requires_user``.
_AuthedHandler = Callable[..., Awaitable[Any]]


def requires_user(handler: _AuthedHandler) -> _AuthedHandler:
    """Decorator that resolves the current ``User`` and injects it.

    Wraps a python-telegram-bot handler whose signature is::

        async def h(update, ctx, *, user: User, session: AsyncSession): ...

    On invocation:

    * Opens a new ``AsyncSession`` from ``get_session_factory()``.
    * Calls :func:`get_current_user`. If it returns ``None`` (no resolver
      could authorize this caller) the handler replies "Not authorized"
      and short-circuits.
    * Otherwise calls the wrapped handler with ``user`` and ``session``
      injected as keyword arguments.
    * Commits the session on a clean return; rolls back on any exception
      so the wrapped handler doesn't have to.

    The decorator uses local imports to avoid circular imports during
    bot module load (``app.bot.handlers.*`` import this module, and the
    decorator depends on ``app.core.database``).
    """

    @functools.wraps(handler)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Any:
        from app.core.database import get_session_factory

        session_factory = get_session_factory()
        async with session_factory() as session:
            user = await get_current_user(session, update)
            if user is None:
                # Resolver returned None — caller is not authorized.
                if update.effective_message is not None:
                    try:
                        await update.effective_message.reply_text("Not authorized.")
                    except Exception:  # pragma: no cover - best-effort reply
                        logger.exception("Failed to send 'Not authorized' reply")
                elif update.callback_query is not None:
                    try:
                        await update.callback_query.answer("Not authorized.", show_alert=True)
                    except Exception:  # pragma: no cover
                        logger.exception("Failed to answer callback with 'Not authorized'")
                return None

            try:
                result = await handler(update, ctx, user=user, session=session)
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()
                return result

    return wrapper

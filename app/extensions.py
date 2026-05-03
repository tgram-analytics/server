"""Registry for OSS-internal extension points.

The OSS server exposes a small, stable set of hooks so that downstream
integrators can swap in custom user resolution, augment project-creation
policy, or add bot middleware without forking the codebase. The default
behavior is a fully working single-tenant install gated by ``ADMIN_CHAT_ID``;
extensions are an opt-in customization seam.

All registration MUST happen at startup (before the FastAPI lifespan
yields). Registering during request handling is unsupported.

Public surface (stable):

* :func:`register_user_resolver` — replace the default resolver
* :func:`register_project_pre_create` — append a pre-flush quota/policy hook
* :func:`register_bot_filter` — append a bot-handler filter (AND-combined)
* :class:`ExtensionError` — base class for extension-raised errors that
  should be rendered to the end user (Telegram message / API response)
  instead of being treated as a server bug

Each ``register_*`` is matched by a ``get_*`` accessor that internal call
sites use to consume the registry. The accessors return immutable views
(``tuple`` or ``Optional``) so callers cannot mutate the registry.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from telegram import Update
    from telegram.ext import filters as ptb_filters

    from app.models.user import User


class ExtensionError(Exception):
    """Base class for plugin-raised errors meant to be shown to the end user.

    OSS handlers (``add_command``, etc.) catch this class specifically and
    render ``str(exc)`` back to the caller (Telegram reply / HTTP response)
    instead of letting the exception propagate as a 500. Plugins should
    subclass this when they want the message to be seen — e.g. quota
    violations, plan-tier limits, policy rejections.

    Anything that is genuinely a bug (a TypeError, an unexpected None) must
    NOT inherit from ExtensionError; let it propagate so the bug is logged.
    """


class UserResolver(Protocol):
    """Strategy for resolving a ``User`` from an incoming Telegram ``Update``."""

    def __call__(self, session: AsyncSession, update: Update) -> Awaitable[User | None]: ...


class ProjectPreCreate(Protocol):
    """Pre-flush hook called inside ``services.projects.create_project``.

    Receives all inputs to project creation and may raise to abort. Hooks
    must be pure-ish: side effects belong elsewhere, since a later hook
    raising will not roll back earlier hooks' side effects.

    Return value: ``None`` (no overrides) or a ``dict[str, Any]`` of
    column overrides to apply to the new ``Project`` row. Only a
    whitelisted set of fields is honored — see
    :data:`PROJECT_OVERRIDABLE_FIELDS`. Unknown keys are ignored with a
    warning log. Multiple hooks' dicts are merged with last-write-wins
    in registration order.
    """

    def __call__(
        self,
        session: AsyncSession,
        *,
        name: str,
        owner_user_id: Any,  # uuid.UUID — kept as Any to avoid an import cycle
        domain_allowlist: list[str],
    ) -> Awaitable[dict[str, Any] | None]: ...


# Whitelists of columns a pre-create hook may set via its return dict.
# Kept narrow on purpose: this is not a general "let plugins write
# arbitrary columns" door. Add new keys here only after deliberate
# review. Entries in :data:`PROJECT_OVERRIDABLE_FIELDS` apply to the
# ``Project`` row; entries in :data:`PROJECT_SETTINGS_OVERRIDABLE_FIELDS`
# apply to the auto-created ``ProjectSettings`` row. The two sets must
# stay disjoint so a single hook return value can be split unambiguously.
PROJECT_OVERRIDABLE_FIELDS: frozenset[str] = frozenset({"rate_limit_per_second"})
PROJECT_SETTINGS_OVERRIDABLE_FIELDS: frozenset[str] = frozenset({"retention_days"})


_user_resolver: UserResolver | None = None
_project_pre_create: list[ProjectPreCreate] = []
_bot_filters: list[ptb_filters.BaseFilter] = []


def register_user_resolver(resolver: UserResolver) -> None:
    """Replace the default singleton user resolver.

    Raises ``RuntimeError`` if called more than once. There can be only
    one strategy at a time; to layer behavior, compose at the resolver
    level.
    """
    global _user_resolver
    if _user_resolver is not None:
        raise RuntimeError("user resolver already registered")
    _user_resolver = resolver


def register_project_pre_create(hook: ProjectPreCreate) -> None:
    """Add a pre-flush hook called inside ``create_project``.

    Hooks run in registration order. Any hook may raise to abort
    creation; the exception propagates to the caller and no project row
    is inserted.
    """
    _project_pre_create.append(hook)


def register_bot_filter(f: ptb_filters.BaseFilter) -> None:
    """Add a filter that gates every bot handler.

    Filters are AND-combined with the default ``filters.Chat(admin_chat_id)``
    in ``app.bot.setup.build_application``.
    """
    _bot_filters.append(f)


def get_user_resolver() -> UserResolver | None:
    """Return the registered user resolver, or ``None`` for the default."""
    return _user_resolver


def get_project_pre_create_hooks() -> tuple[ProjectPreCreate, ...]:
    """Return all registered pre-create hooks in registration order."""
    return tuple(_project_pre_create)


def get_bot_filters() -> tuple[ptb_filters.BaseFilter, ...]:
    """Return all registered bot filters in registration order."""
    return tuple(_bot_filters)


def _reset_for_tests() -> None:
    """Test-only: clear all registries between tests.

    Not part of the stable surface; the leading underscore is the contract.
    """
    global _user_resolver
    _user_resolver = None
    _project_pre_create.clear()
    _bot_filters.clear()

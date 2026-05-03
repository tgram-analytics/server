"""Unit tests for ``app.extensions`` — the bare registry module.

These tests exercise the registration / accessor / reset surface in
isolation, without involving any consumer (auth.py, services/projects.py,
bot/setup.py). Consumer-level wiring is covered by phase-specific test
modules.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app import extensions as ext


@pytest.fixture(autouse=True)
def _clear_registry():
    ext._reset_for_tests()
    yield
    ext._reset_for_tests()


# ── User resolver ─────────────────────────────────────────────────────────────


def test_user_resolver_default_is_none() -> None:
    assert ext.get_user_resolver() is None


def test_user_resolver_register_then_get() -> None:
    resolver = AsyncMock()
    ext.register_user_resolver(resolver)
    assert ext.get_user_resolver() is resolver


def test_user_resolver_double_registration_raises() -> None:
    ext.register_user_resolver(AsyncMock())
    with pytest.raises(RuntimeError, match="already registered"):
        ext.register_user_resolver(AsyncMock())


def test_user_resolver_reset_allows_re_registration() -> None:
    """After ``_reset_for_tests``, a fresh registration must succeed.

    Guards against a regression where the duplicate-registration check
    leaks state across resets.
    """
    ext.register_user_resolver(AsyncMock())
    ext._reset_for_tests()
    ext.register_user_resolver(AsyncMock())  # must not raise
    assert ext.get_user_resolver() is not None


# ── Project pre-create hooks ──────────────────────────────────────────────────


def test_project_pre_create_default_is_empty_tuple() -> None:
    hooks = ext.get_project_pre_create_hooks()
    assert hooks == ()
    assert isinstance(hooks, tuple)


def test_project_pre_create_returns_immutable_view() -> None:
    """Callers cannot mutate the registry via the accessor.

    The accessor returns a tuple snapshot; appending to it does not
    affect future calls.
    """
    hook = AsyncMock()
    ext.register_project_pre_create(hook)

    snapshot = ext.get_project_pre_create_hooks()
    assert snapshot == (hook,)

    # Mutating the snapshot is impossible (tuples), and even if we
    # round-trip through a list the registry is unaffected.
    list(snapshot).clear()
    assert ext.get_project_pre_create_hooks() == (hook,)


def test_project_pre_create_preserves_registration_order() -> None:
    a, b, c = AsyncMock(), AsyncMock(), AsyncMock()
    ext.register_project_pre_create(a)
    ext.register_project_pre_create(b)
    ext.register_project_pre_create(c)
    assert ext.get_project_pre_create_hooks() == (a, b, c)


def test_project_pre_create_supports_many_hooks() -> None:
    """No artificial cap — registering many hooks is fine."""
    hooks = [AsyncMock() for _ in range(50)]
    for h in hooks:
        ext.register_project_pre_create(h)
    assert ext.get_project_pre_create_hooks() == tuple(hooks)


def test_project_pre_create_reset_clears_all() -> None:
    for _ in range(3):
        ext.register_project_pre_create(AsyncMock())
    ext._reset_for_tests()
    assert ext.get_project_pre_create_hooks() == ()


# ── Bot filters ───────────────────────────────────────────────────────────────


def test_bot_filters_default_is_empty_tuple() -> None:
    filters = ext.get_bot_filters()
    assert filters == ()
    assert isinstance(filters, tuple)


def test_bot_filter_register_and_order() -> None:
    f1, f2 = object(), object()
    ext.register_bot_filter(f1)  # type: ignore[arg-type]
    ext.register_bot_filter(f2)  # type: ignore[arg-type]
    assert ext.get_bot_filters() == (f1, f2)


def test_bot_filters_reset_clears() -> None:
    ext.register_bot_filter(object())  # type: ignore[arg-type]
    ext._reset_for_tests()
    assert ext.get_bot_filters() == ()


# ── Cross-cutting: independence of registries ─────────────────────────────────


def test_registries_are_independent() -> None:
    """Registering one kind of hook does not pollute the others."""
    ext.register_user_resolver(AsyncMock())
    assert ext.get_project_pre_create_hooks() == ()
    assert ext.get_bot_filters() == ()

    ext.register_project_pre_create(AsyncMock())
    assert ext.get_bot_filters() == ()


def test_reset_clears_all_three_registries() -> None:
    ext.register_user_resolver(AsyncMock())
    ext.register_project_pre_create(AsyncMock())
    ext.register_bot_filter(object())  # type: ignore[arg-type]

    ext._reset_for_tests()

    assert ext.get_user_resolver() is None
    assert ext.get_project_pre_create_hooks() == ()
    assert ext.get_bot_filters() == ()


# ── Public surface guard ──────────────────────────────────────────────────────


def test_public_surface_is_stable() -> None:
    """If this test fails, you've changed the documented public API.

    The OSS extension surface is small and load-bearing — every name
    here is part of the contract relied on by downstream packages.
    Removing or renaming any of these is a breaking change and must be
    deliberate.
    """
    expected_public = {
        "UserResolver",
        "ProjectPreCreate",
        "ExtensionError",
        "register_user_resolver",
        "register_project_pre_create",
        "register_bot_filter",
        "get_user_resolver",
        "get_project_pre_create_hooks",
        "get_bot_filters",
    }
    actual_public = {name for name in vars(ext) if not name.startswith("_")}
    # Allow imports to leak in but require all expected names present.
    missing = expected_public - actual_public
    assert not missing, f"public API regressed; missing: {missing}"

"""Tests for ``app.plugins.load_plugins`` (Phase 5).

Covers both discovery mechanisms (entry points and env var), failure
modes, ordering, and plumbing into the live ``app.extensions`` registry.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from importlib.metadata import EntryPoint
from unittest.mock import patch

import pytest

from app import extensions as ext
from app.plugins import ENTRY_POINT_GROUP, ENV_VAR, load_plugins


@pytest.fixture(autouse=True)
def _clear_state() -> Iterator[None]:
    """Clear registry, env var, and dummy_plugin counter between tests."""
    import os

    ext._reset_for_tests()
    prev_var = os.environ.pop(ENV_VAR, None)
    yield
    ext._reset_for_tests()
    if prev_var is not None:
        os.environ[ENV_VAR] = prev_var
    # Reset dummy plugin counter if it got imported.
    if "tests.fixtures.dummy_plugin" in sys.modules:
        sys.modules["tests.fixtures.dummy_plugin"].register_call_count = 0


# ── No plugins configured ─────────────────────────────────────────────────────


def test_no_entry_points_no_env_returns_empty() -> None:
    """Default OSS install — no plugins anywhere."""
    with patch("app.plugins.entry_points", return_value=[]):
        loaded = load_plugins()
    assert loaded == []
    assert ext.get_user_resolver() is None
    assert ext.get_project_pre_create_hooks() == ()
    assert ext.get_bot_filters() == ()


def test_empty_env_var_treated_as_no_plugins() -> None:
    """``TGA_EXTENSIONS=`` (set but empty) loads nothing."""
    import os

    os.environ[ENV_VAR] = ""
    with patch("app.plugins.entry_points", return_value=[]):
        loaded = load_plugins()
    assert loaded == []


def test_whitespace_only_env_var_treated_as_no_plugins() -> None:
    """Whitespace-only env var is normalized to no-plugins."""
    import os

    os.environ[ENV_VAR] = "   "
    with patch("app.plugins.entry_points", return_value=[]):
        loaded = load_plugins()
    assert loaded == []


# ── Env-var discovery ─────────────────────────────────────────────────────────


def test_env_var_loads_dummy_plugin() -> None:
    """Module named in TGA_EXTENSIONS has its register() called."""
    import os

    os.environ[ENV_VAR] = "tests.fixtures.dummy_plugin"

    with patch("app.plugins.entry_points", return_value=[]):
        loaded = load_plugins()

    from tests.fixtures import dummy_plugin

    assert dummy_plugin.register_call_count == 1
    assert loaded == ["env:tests.fixtures.dummy_plugin"]


def test_env_var_multiple_modules_comma_separated() -> None:
    """Multiple modules load in order, comma-separated."""
    import os

    # Reuse the same dummy plugin twice; the counter will reflect both calls.
    os.environ[ENV_VAR] = "tests.fixtures.dummy_plugin,tests.fixtures.dummy_plugin"

    with patch("app.plugins.entry_points", return_value=[]):
        loaded = load_plugins()

    from tests.fixtures import dummy_plugin

    assert dummy_plugin.register_call_count == 2
    assert loaded == [
        "env:tests.fixtures.dummy_plugin",
        "env:tests.fixtures.dummy_plugin",
    ]


def test_env_var_strips_whitespace_around_modules() -> None:
    import os

    os.environ[ENV_VAR] = "  tests.fixtures.dummy_plugin  ,  ,  "
    with patch("app.plugins.entry_points", return_value=[]):
        loaded = load_plugins()
    assert loaded == ["env:tests.fixtures.dummy_plugin"]


def test_env_var_unknown_module_raises_loudly() -> None:
    """Misconfiguration must surface at startup, not be silenced."""
    import os

    os.environ[ENV_VAR] = "nonexistent.module.path.xyz"
    with (
        patch("app.plugins.entry_points", return_value=[]),
        pytest.raises(ModuleNotFoundError),
    ):
        load_plugins()


def test_env_var_module_without_register_does_nothing() -> None:
    """Module with no ``register`` is loaded but contributes no behavior.

    This is by design: a downstream may want to ship a "drop-in import
    triggers side effects at module load" plugin pattern. We tolerate
    that without erroring.
    """
    import os

    # ``app.extensions`` itself has no ``register`` callable.
    os.environ[ENV_VAR] = "app.extensions"
    with patch("app.plugins.entry_points", return_value=[]):
        loaded = load_plugins()
    assert loaded == ["env:app.extensions"]


# ── Plumbing: plugin can register hooks ───────────────────────────────────────


def test_plugin_register_user_resolver_via_env_var() -> None:
    """End-to-end: plugin's register() actually wires the hook."""
    import os

    os.environ[ENV_VAR] = "tests.fixtures.resolver_plugin"
    with patch("app.plugins.entry_points", return_value=[]):
        load_plugins()

    from tests.fixtures import resolver_plugin

    assert ext.get_user_resolver() is resolver_plugin.resolver_mock


# ── Entry-point discovery ─────────────────────────────────────────────────────


def _make_fake_entry_point(name: str, target: str) -> EntryPoint:
    """Build an EntryPoint pointing at ``module:attribute``.

    Uses the actual EntryPoint dataclass so ``ep.load()`` works without
    further mocking.
    """
    return EntryPoint(name=name, value=target, group=ENTRY_POINT_GROUP)


def test_entry_point_loads_and_invokes_register() -> None:
    """An entry point pointing at register() is called with no args."""
    ep = _make_fake_entry_point("dummy", "tests.fixtures.dummy_plugin:register")

    with patch("app.plugins.entry_points", return_value=[ep]):
        loaded = load_plugins()

    from tests.fixtures import dummy_plugin

    assert dummy_plugin.register_call_count == 1
    assert loaded == ["entry_point:dummy"]


def test_entry_point_pointing_at_non_callable_does_not_invoke() -> None:
    """Entry point can point at a non-callable; loader logs and continues."""
    ep = _make_fake_entry_point("non_callable", "tests.fixtures.dummy_plugin:register_call_count")

    with patch("app.plugins.entry_points", return_value=[ep]):
        loaded = load_plugins()

    # Recorded as loaded; counter unchanged because no callable was invoked.
    assert loaded == ["entry_point:non_callable"]


def test_entry_points_and_env_var_together() -> None:
    """Both discovery mechanisms can run in the same call.

    Order: entry points first, env vars second (documented contract).
    """
    import os

    os.environ[ENV_VAR] = "tests.fixtures.dummy_plugin"
    ep = _make_fake_entry_point("ep1", "tests.fixtures.dummy_plugin:register")

    with patch("app.plugins.entry_points", return_value=[ep]):
        loaded = load_plugins()

    from tests.fixtures import dummy_plugin

    # register() was called twice: once via entry point, once via env var.
    assert dummy_plugin.register_call_count == 2
    assert loaded == [
        "entry_point:ep1",
        "env:tests.fixtures.dummy_plugin",
    ]


def test_entry_points_query_uses_correct_group() -> None:
    """Loader queries the documented group name only."""
    with patch("app.plugins.entry_points") as mock_eps:
        mock_eps.return_value = []
        load_plugins()

    mock_eps.assert_called_once_with(group=ENTRY_POINT_GROUP)


def test_load_order_within_entry_points_preserved() -> None:
    """Multiple entry points are invoked in the order returned by importlib."""
    ep_a = _make_fake_entry_point("a", "tests.fixtures.dummy_plugin:register")
    ep_b = _make_fake_entry_point("b", "tests.fixtures.dummy_plugin:register")

    with patch("app.plugins.entry_points", return_value=[ep_a, ep_b]):
        loaded = load_plugins()

    assert loaded == ["entry_point:a", "entry_point:b"]


# ── Public surface ────────────────────────────────────────────────────────────


def test_constants_exposed() -> None:
    """The two constants are part of the documented contract."""
    assert ENTRY_POINT_GROUP == "tgram_analytics.extensions"
    assert ENV_VAR == "TGA_EXTENSIONS"

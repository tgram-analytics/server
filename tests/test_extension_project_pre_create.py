"""Tests for the pre-create-project extension point (Phase 3).

Validates that ``services.projects.create_project``:

* runs all registered hooks in order before any DB write,
* propagates a hook's exception and aborts creation,
* short-circuits subsequent hooks when one raises,
* passes a fresh list-copy of ``domain_allowlist`` to each hook (so
  hooks cannot mutate the caller's list),
* still works with no hooks registered (regression check).

Hook-execution semantics are verified with mocks; integration with a
real DB is covered by the existing ``test_phase2.py`` suite under
DATABASE_URL-gated fixtures.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import extensions as ext


@pytest.fixture(autouse=True)
def _clear_registry():
    ext._reset_for_tests()
    yield
    ext._reset_for_tests()


def _make_session() -> MagicMock:
    """Build a MagicMock session whose async ops are AsyncMocks.

    Mirrors the methods ``create_project`` calls: ``add``, ``flush``,
    ``refresh``. Using ``MagicMock`` plus ``AsyncMock`` for the async
    methods keeps the test honest about which calls are awaited.
    """
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    return session


async def test_no_hooks_registered_creates_normally() -> None:
    """Default OSS path: zero hooks, behavior unchanged.

    We verify the hook loop is a no-op by registering nothing and
    asserting ``session.add`` is called with both the Project and the
    ProjectSettings row, in that order, followed by two flushes.
    """
    from app.services.projects import create_project

    session = _make_session()
    project, api_key = await create_project(
        session,
        name="myapp",
        admin_chat_id=42,
        owner_user_id=uuid.uuid4(),
    )

    # Two add() calls (Project, then ProjectSettings) and two flushes.
    assert session.add.call_count == 2
    assert session.flush.await_count == 2
    assert session.refresh.await_count == 1
    assert api_key.startswith("proj_")
    assert project is not None


async def test_single_hook_called_with_inputs() -> None:
    """A registered hook receives session + the create_project kwargs."""
    from app.services.projects import create_project

    hook = AsyncMock(return_value=None)
    ext.register_project_pre_create(hook)

    session = _make_session()
    owner_id = uuid.uuid4()
    await create_project(
        session,
        name="myapp",
        admin_chat_id=42,
        owner_user_id=owner_id,
        domain_allowlist=["a.com", "b.com"],
    )

    hook.assert_awaited_once()
    args, kwargs = hook.await_args
    assert args[0] is session
    assert kwargs["name"] == "myapp"
    assert kwargs["owner_user_id"] == owner_id
    assert kwargs["domain_allowlist"] == ["a.com", "b.com"]


async def test_hook_runs_before_any_db_write() -> None:
    """Hook is awaited before ``session.add`` is called.

    This guarantees a hook that raises leaves the DB untouched.
    """
    from app.services.projects import create_project

    call_log: list[str] = []

    async def recording_hook(session, **kwargs):  # noqa: ANN001 - test
        call_log.append("hook")

    session = _make_session()
    session.add.side_effect = lambda *a, **kw: call_log.append("add")
    session.flush.side_effect = lambda *a, **kw: call_log.append("flush")

    ext.register_project_pre_create(recording_hook)

    await create_project(
        session,
        name="myapp",
        admin_chat_id=42,
        owner_user_id=uuid.uuid4(),
    )

    assert call_log[0] == "hook"
    assert "add" in call_log
    assert call_log.index("hook") < call_log.index("add")


async def test_hook_raising_aborts_creation() -> None:
    """A hook that raises propagates the exception; no DB write occurs."""
    from app.services.projects import create_project

    class QuotaExceeded(Exception):
        pass

    hook = AsyncMock(side_effect=QuotaExceeded("over limit"))
    ext.register_project_pre_create(hook)

    session = _make_session()
    with pytest.raises(QuotaExceeded, match="over limit"):
        await create_project(
            session,
            name="myapp",
            admin_chat_id=42,
            owner_user_id=uuid.uuid4(),
        )

    session.add.assert_not_called()
    session.flush.assert_not_awaited()


async def test_first_raising_hook_short_circuits_rest() -> None:
    """If hook A raises, hook B never runs."""
    from app.services.projects import create_project

    hook_a = AsyncMock(side_effect=RuntimeError("nope"))
    hook_b = AsyncMock()
    ext.register_project_pre_create(hook_a)
    ext.register_project_pre_create(hook_b)

    session = _make_session()
    with pytest.raises(RuntimeError, match="nope"):
        await create_project(
            session,
            name="x",
            admin_chat_id=1,
            owner_user_id=uuid.uuid4(),
        )

    hook_a.assert_awaited_once()
    hook_b.assert_not_awaited()


async def test_multiple_hooks_run_in_registration_order() -> None:
    """All hooks run sequentially in the order they were registered."""
    from app.services.projects import create_project

    order: list[int] = []

    async def make_recorder(idx: int):
        async def _hook(session, **kwargs):  # noqa: ANN001 - test
            order.append(idx)

        return _hook

    h1 = await make_recorder(1)
    h2 = await make_recorder(2)
    h3 = await make_recorder(3)
    ext.register_project_pre_create(h1)
    ext.register_project_pre_create(h2)
    ext.register_project_pre_create(h3)

    await create_project(
        _make_session(),
        name="x",
        admin_chat_id=1,
        owner_user_id=uuid.uuid4(),
    )

    assert order == [1, 2, 3]


async def test_hook_cannot_mutate_callers_allowlist() -> None:
    """Hook receives a list-copy; mutating it does not affect creation.

    Defensive: if downstream hooks were tempted to "normalize" the
    allowlist by mutating in place, the project row should still get
    the caller's original input (until/unless a future hook is given
    a documented mutation path — there isn't one today).
    """
    from app.services.projects import create_project

    captured: dict = {}

    async def mutating_hook(session, **kwargs):  # noqa: ANN001 - test
        # Snapshot what we got BEFORE mutating.
        captured["received_id"] = id(kwargs["domain_allowlist"])
        captured["received_snapshot"] = list(kwargs["domain_allowlist"])
        kwargs["domain_allowlist"].append("INJECTED.com")

    ext.register_project_pre_create(mutating_hook)

    callers_list = ["a.com"]
    session = _make_session()

    # Capture the args passed to Project() — intercept session.add and
    # inspect the SQLAlchemy model's domain_allowlist.
    added: list = []
    session.add.side_effect = lambda obj: added.append(obj)

    await create_project(
        session,
        name="x",
        admin_chat_id=1,
        owner_user_id=uuid.uuid4(),
        domain_allowlist=callers_list,
    )

    # Hook saw a fresh list (different identity from caller's).
    assert captured["received_snapshot"] == ["a.com"]
    assert captured["received_id"] != id(callers_list)

    # Caller's list is untouched by the hook's mutation.
    assert callers_list == ["a.com"]

    # The Project row was created with a fresh allowlist (no INJECTED).
    project_row = added[0]
    assert "INJECTED.com" not in project_row.domain_allowlist
    assert project_row.domain_allowlist == ["a.com"]


async def test_hook_called_with_none_allowlist_normalizes_to_empty_list() -> None:
    """When the caller passes ``domain_allowlist=None``, the hook gets ``[]``."""
    from app.services.projects import create_project

    captured: dict = {}

    async def hook(session, **kwargs):  # noqa: ANN001 - test
        captured["received"] = kwargs["domain_allowlist"]

    ext.register_project_pre_create(hook)

    await create_project(
        _make_session(),
        name="x",
        admin_chat_id=1,
        owner_user_id=uuid.uuid4(),
        domain_allowlist=None,
    )

    assert captured["received"] == []


async def test_quota_pattern_realistic_use_case() -> None:
    """End-to-end sanity check of the canonical 'free tier = 1 project' pattern.

    A hook counts existing projects for the owner and rejects creation
    if a free-tier limit is reached. This is the kind of policy a
    cloud overlay would register; OSS has no such hook by default.
    """
    from app.services.projects import create_project

    class OverQuota(Exception):
        pass

    project_count = {"value": 1}  # already at the (mocked) limit

    async def quota_hook(session, **kwargs):  # noqa: ANN001 - test
        if project_count["value"] >= 1:
            raise OverQuota(f"owner {kwargs['owner_user_id']} at limit")

    ext.register_project_pre_create(quota_hook)

    session = _make_session()
    with pytest.raises(OverQuota, match="at limit"):
        await create_project(
            session,
            name="second-project",
            admin_chat_id=1,
            owner_user_id=uuid.uuid4(),
        )

    session.add.assert_not_called()


async def test_hook_returning_override_dict_applies_to_project() -> None:
    """A hook may return a dict of whitelisted column overrides.

    The values land on the new ``Project`` row instead of the model
    defaults. Used by cloud overlays to set ``rate_limit_per_second``
    per plan tier at create time.
    """
    from app.services.projects import create_project

    async def overriding_hook(session, **kwargs):  # noqa: ANN001 - test
        return {"rate_limit_per_second": 10}

    ext.register_project_pre_create(overriding_hook)

    session = _make_session()
    added: list = []
    session.add.side_effect = lambda obj: added.append(obj)

    await create_project(
        session,
        name="x",
        admin_chat_id=1,
        owner_user_id=uuid.uuid4(),
    )

    project_row = added[0]
    assert project_row.rate_limit_per_second == 10


async def test_hook_override_targets_project_settings_when_field_in_settings_whitelist() -> None:
    """Whitelisted ``retention_days`` lands on ``ProjectSettings``, not ``Project``.

    The cloud overlay returns a single flat dict with keys destined
    for both rows (e.g., ``rate_limit_per_second`` for Project and
    ``retention_days`` for ProjectSettings). ``create_project`` must
    route each key to the right model based on the whitelist
    membership.
    """
    from app.models.project import Project as ProjectModel
    from app.models.settings import ProjectSettings
    from app.services.projects import create_project

    async def cloud_like_hook(session, **kwargs):  # noqa: ANN001 - test
        return {"rate_limit_per_second": 10, "retention_days": 60}

    ext.register_project_pre_create(cloud_like_hook)

    session = _make_session()
    added: list = []
    session.add.side_effect = lambda obj: added.append(obj)

    await create_project(
        session,
        name="x",
        admin_chat_id=1,
        owner_user_id=uuid.uuid4(),
    )

    project_row = next(o for o in added if isinstance(o, ProjectModel))
    settings_row = next(o for o in added if isinstance(o, ProjectSettings))
    assert project_row.rate_limit_per_second == 10
    assert settings_row.retention_days == 60


async def test_hook_returning_none_keeps_defaults() -> None:
    """A hook returning ``None`` (or implicitly) leaves columns at default."""
    from app.services.projects import create_project

    async def noop_hook(session, **kwargs):  # noqa: ANN001 - test
        return None

    ext.register_project_pre_create(noop_hook)

    session = _make_session()
    added: list = []
    session.add.side_effect = lambda obj: added.append(obj)

    await create_project(
        session,
        name="x",
        admin_chat_id=1,
        owner_user_id=uuid.uuid4(),
    )

    project_row = added[0]
    assert project_row.rate_limit_per_second is None


async def test_multiple_hooks_overrides_merge_last_write_wins() -> None:
    """When two hooks both set the same key, registration order decides."""
    from app.services.projects import create_project

    async def hook_a(session, **kwargs):  # noqa: ANN001 - test
        return {"rate_limit_per_second": 5}

    async def hook_b(session, **kwargs):  # noqa: ANN001 - test
        return {"rate_limit_per_second": 50}

    ext.register_project_pre_create(hook_a)
    ext.register_project_pre_create(hook_b)

    session = _make_session()
    added: list = []
    session.add.side_effect = lambda obj: added.append(obj)

    await create_project(
        session,
        name="x",
        admin_chat_id=1,
        owner_user_id=uuid.uuid4(),
    )

    assert added[0].rate_limit_per_second == 50


async def test_hook_override_with_non_whitelisted_key_is_ignored(caplog) -> None:
    """Returning a key outside ``PROJECT_OVERRIDABLE_FIELDS`` is ignored.

    We do not want plugins to be able to set arbitrary columns through
    this seam (e.g., ``api_key_hash`` or ``owner_user_id``). Unknown
    keys log a warning and are dropped; whitelisted keys still apply.
    """
    import logging

    from app.services.projects import create_project

    async def sneaky_hook(session, **kwargs):  # noqa: ANN001 - test
        return {
            "rate_limit_per_second": 7,
            "owner_user_id": uuid.uuid4(),  # NOT whitelisted
            "api_key_hash": "haxx",  # NOT whitelisted
        }

    ext.register_project_pre_create(sneaky_hook)

    session = _make_session()
    added: list = []
    session.add.side_effect = lambda obj: added.append(obj)

    legit_owner = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger="app.services.projects"):
        await create_project(
            session,
            name="x",
            admin_chat_id=1,
            owner_user_id=legit_owner,
        )

    project_row = added[0]
    assert project_row.rate_limit_per_second == 7  # whitelisted: applied
    assert project_row.owner_user_id == legit_owner  # ignored: caller's value wins
    assert project_row.api_key_hash != "haxx"  # ignored: real hash
    # Each non-whitelisted key produces a warning.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("owner_user_id" in r.getMessage() for r in warnings)
    assert any("api_key_hash" in r.getMessage() for r in warnings)


async def test_hook_returning_non_dict_raises_typeerror() -> None:
    """Non-None, non-dict return is a programming error and raises."""
    from app.services.projects import create_project

    async def buggy_hook(session, **kwargs):  # noqa: ANN001 - test
        return ["not", "a", "dict"]

    ext.register_project_pre_create(buggy_hook)

    session = _make_session()
    with pytest.raises(TypeError, match="expected dict or None"):
        await create_project(
            session,
            name="x",
            admin_chat_id=1,
            owner_user_id=uuid.uuid4(),
        )

    session.add.assert_not_called()


async def test_hook_can_inspect_session_for_db_lookups() -> None:
    """The session passed to a hook is the same object passed to add/flush.

    Hooks that need to count existing rows for the owner (typical quota
    case) can use the session directly — same transactional context.
    """
    from app.services.projects import create_project

    captured = {}

    async def hook(session, **kwargs):  # noqa: ANN001 - test
        captured["session"] = session

    ext.register_project_pre_create(hook)

    session = _make_session()
    await create_project(
        session,
        name="x",
        admin_chat_id=1,
        owner_user_id=uuid.uuid4(),
    )

    assert captured["session"] is session

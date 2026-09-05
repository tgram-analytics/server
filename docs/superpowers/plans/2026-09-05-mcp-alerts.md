# MCP Alert Tools + Delivery History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let MCP clients list, create, pause/resume and delete alerts on a project, and read the history of alert notifications the server sent.

**Architecture:** A new `alert_deliveries` table records one row per fired alert from the existing notification loop in `app/api/ingestion.py`. Three new service functions in `app/services/alerts.py` wrap the table plus an explicit active-flag setter. A new `app/mcp/tools/alerts.py` registers five FastMCP tools that follow the exact pattern of `app/mcp/tools/projects.py`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2, FastMCP (`mcp` package), pytest + pytest-asyncio. Spec: `docs/superpowers/specs/2026-09-05-mcp-alerts-design.md`.

---

## Before you start

Work in the worktree `/root/progetti/tgram-server/.claude/worktrees/mcp-alerts` on branch `worktree-mcp-alerts`. Run every command from there.

**Test database.** DB tests skip without `DATABASE_URL`. Start one and migrate:

```bash
docker run -d --name tga-test-pg -e POSTGRES_USER=tga -e POSTGRES_PASSWORD=password -e POSTGRES_DB=tganalytics_test -p 5432:5432 postgres:16-alpine
export DATABASE_URL="postgresql+asyncpg://tga:password@localhost/tganalytics_test"
uv sync --extra dev
uv run alembic upgrade head
```

Expected: `Running upgrade ... -> 0012`. Re-run `uv run alembic upgrade head` after Task 1 adds `0013`.

**Test command prefix.** Use `uv run pytest ...` and `uv run ruff check .`, `uv run ruff format .`, `uv run mypy app`.

**Conventions you must copy:**
- MCP handlers never raise. They return a Pydantic result on success or `[TextContent(type="text", text=..., isError=True)]` on error.
- Ownership check `assert_project_owned_by(session, pid, owner_user_id)` runs before any service call.
- Services flush, callers commit.

---

### Task 1: `alert_deliveries` table, model, migration

**Files:**
- Create: `app/models/alert_delivery.py`
- Create: `alembic/versions/0013_alert_deliveries.py`
- Modify: `app/models/__init__.py`
- Test: `tests/test_alerts.py` (append at end)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_alerts.py`:

```python
# ── Alert delivery history ────────────────────────────────────────────────────


async def test_alert_delivery_model_roundtrip(db_session, singleton_user):
    """AlertDelivery rows persist a snapshot of the alert that fired."""
    from app.models.alert_delivery import AlertDelivery
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    project, _ = await create_project(
        db_session,
        name="delivery-model.com",
        admin_chat_id=ADMIN_ID,
        owner_user_id=singleton_user.id,
    )
    alert = await create_alert(
        db_session,
        project_id=project.id,
        event_name="signup",
        condition=AlertCondition.every_n,
        threshold_n=10,
    )
    row = AlertDelivery(
        alert_id=alert.id,
        project_id=project.id,
        event_name=alert.event_name,
        condition=alert.condition,
        threshold_n=alert.threshold_n,
        delivered=True,
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)

    assert row.id is not None
    assert row.fired_at is not None
    assert row.error is None
    assert row.condition == AlertCondition.every_n
    assert row.threshold_n == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_alerts.py::test_alert_delivery_model_roundtrip -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.alert_delivery'`

- [ ] **Step 3: Create the model**

Create `app/models/alert_delivery.py`:

```python
"""SQLAlchemy ORM model for the alert_deliveries table.

One row per alert notification the server attempted to send. Columns
snapshot the alert's configuration at fire time so history survives
alert deletion (``alert_id`` becomes NULL via ON DELETE SET NULL).
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.alert import AlertCondition


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("alerts.id", ondelete="SET NULL"),
        nullable=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Reuse the PG enum created in migration 0001; do not create it again.
    condition: Mapped[AlertCondition] = mapped_column(
        sa.Enum(AlertCondition, name="alert_condition", create_type=False),
        nullable=False,
    )
    threshold_n: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    fired_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )
    delivered: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    # Exception class name when the Telegram send failed; NULL on success.
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    __table_args__ = (
        sa.Index(
            "ix_alert_deliveries_project_fired",
            "project_id",
            sa.text("fired_at DESC"),
        ),
    )
```

- [ ] **Step 4: Register the model**

In `app/models/__init__.py`, add after the `Alert` import line:

```python
from app.models.alert_delivery import AlertDelivery
```

and add `"AlertDelivery",` to `__all__` right after `"AlertCondition",`.

- [ ] **Step 5: Write the migration**

Create `alembic/versions/0013_alert_deliveries.py`:

```python
"""Create alert_deliveries table (alert notification history).

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-05 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_deliveries",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "alert_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("alerts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "project_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column(
            "condition",
            postgresql.ENUM(
                "every",
                "every_n",
                "threshold",
                name="alert_condition",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("threshold_n", sa.Integer(), nullable=True),
        sa.Column(
            "fired_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_alert_deliveries_project_fired",
        "alert_deliveries",
        ["project_id", sa.text("fired_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_alert_deliveries_project_fired", table_name="alert_deliveries")
    op.drop_table("alert_deliveries")
```

- [ ] **Step 6: Apply the migration and run the test**

Run:
```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
uv run pytest tests/test_alerts.py::test_alert_delivery_model_roundtrip -v
```
Expected: both alembic directions succeed without error; test PASS.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy app
git add app/models/alert_delivery.py app/models/__init__.py alembic/versions/0013_alert_deliveries.py tests/test_alerts.py
git commit -m "feat(alerts): add alert_deliveries table and model"
```

---

### Task 2: Service functions `record_delivery`, `list_deliveries`, `set_alert_active`

**Files:**
- Modify: `app/services/alerts.py`
- Test: `tests/test_alerts.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_alerts.py`:

```python
async def _seed_project_and_alert(session, owner_id, *, name, event_name="buy"):
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    project, _ = await create_project(
        session, name=name, admin_chat_id=ADMIN_ID, owner_user_id=owner_id
    )
    alert = await create_alert(
        session, project_id=project.id, event_name=event_name, condition=AlertCondition.every
    )
    return project, alert


async def test_record_delivery_snapshots_alert(db_session, singleton_user):
    from app.services.alerts import record_delivery

    project, alert = await _seed_project_and_alert(
        db_session, singleton_user.id, name="rec-deliv.com"
    )
    row = await record_delivery(db_session, alert=alert, delivered=False, error="TimedOut")

    assert row.alert_id == alert.id
    assert row.project_id == project.id
    assert row.event_name == "buy"
    assert row.condition == AlertCondition.every
    assert row.threshold_n is None
    assert row.delivered is False
    assert row.error == "TimedOut"
    assert row.fired_at is not None


async def test_list_deliveries_filters_and_orders(db_session, singleton_user):
    from datetime import UTC, datetime, timedelta

    from app.services.alerts import create_alert, list_deliveries, record_delivery

    project, alert_buy = await _seed_project_and_alert(
        db_session, singleton_user.id, name="list-deliv.com"
    )
    alert_signup = await create_alert(
        db_session, project_id=project.id, event_name="signup", condition=AlertCondition.every
    )
    other_project, other_alert = await _seed_project_and_alert(
        db_session, singleton_user.id, name="other-deliv.com"
    )

    first = await record_delivery(db_session, alert=alert_buy, delivered=True)
    second = await record_delivery(db_session, alert=alert_signup, delivered=True)
    third = await record_delivery(db_session, alert=alert_buy, delivered=True)
    await record_delivery(db_session, alert=other_alert, delivered=True)
    # Force distinct, ordered timestamps regardless of DB clock resolution.
    base = datetime.now(UTC)
    first.fired_at = base - timedelta(minutes=3)
    second.fired_at = base - timedelta(minutes=2)
    third.fired_at = base - timedelta(minutes=1)
    await db_session.flush()

    since = base - timedelta(days=7)
    rows = await list_deliveries(db_session, project.id, since=since, limit=50)
    assert [r.id for r in rows] == [third.id, second.id, first.id]

    rows = await list_deliveries(db_session, project.id, since=since, limit=50, event_name="buy")
    assert [r.id for r in rows] == [third.id, first.id]

    rows = await list_deliveries(db_session, project.id, since=since, limit=2)
    assert [r.id for r in rows] == [third.id, second.id]

    rows = await list_deliveries(
        db_session, project.id, since=base - timedelta(seconds=90), limit=50
    )
    assert [r.id for r in rows] == [third.id]


async def test_set_alert_active_sets_and_does_not_flip(db_session, singleton_user):
    from app.services.alerts import set_alert_active

    project, alert = await _seed_project_and_alert(
        db_session, singleton_user.id, name="set-active.com"
    )
    assert alert.is_active is True

    updated = await set_alert_active(db_session, alert.id, project.id, is_active=False)
    assert updated is not None and updated.is_active is False

    # Calling again with the same value keeps it (no toggle).
    updated = await set_alert_active(db_session, alert.id, project.id, is_active=False)
    assert updated is not None and updated.is_active is False

    updated = await set_alert_active(db_session, alert.id, project.id, is_active=True)
    assert updated is not None and updated.is_active is True

    # Wrong project → None (no cross-project mutation).
    assert await set_alert_active(db_session, alert.id, uuid.uuid4(), is_active=False) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_alerts.py -k "record_delivery or list_deliveries or set_alert_active" -v`
Expected: 3 FAIL with `ImportError: cannot import name 'record_delivery'` (and similar).

- [ ] **Step 3: Implement the services**

In `app/services/alerts.py`, change the model import to:

```python
from app.models.alert import Alert, AlertCondition
from app.models.alert_delivery import AlertDelivery
```

Append at the end of the file:

```python
async def set_alert_active(
    session: AsyncSession,
    alert_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    is_active: bool,
) -> Alert | None:
    """Set ``is_active`` explicitly (not a flip). Returns None if not found."""
    alert = await get_alert(session, alert_id, project_id)
    if alert is None:
        return None
    alert.is_active = is_active
    await session.flush()
    await session.refresh(alert)
    return alert


async def record_delivery(
    session: AsyncSession,
    *,
    alert: Alert,
    delivered: bool,
    error: str | None = None,
) -> AlertDelivery:
    """Insert one ``alert_deliveries`` row snapshotting *alert* at fire time."""
    row = AlertDelivery(
        alert_id=alert.id,
        project_id=alert.project_id,
        event_name=alert.event_name,
        condition=alert.condition,
        threshold_n=alert.threshold_n,
        delivered=delivered,
        error=error,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_deliveries(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    since: datetime,
    limit: int,
    event_name: str | None = None,
) -> list[AlertDelivery]:
    """Return deliveries for *project_id* with ``fired_at >= since``, newest first."""
    query = (
        select(AlertDelivery)
        .where(
            AlertDelivery.project_id == project_id,
            AlertDelivery.fired_at >= since,
        )
        .order_by(AlertDelivery.fired_at.desc())
        .limit(limit)
    )
    if event_name is not None:
        query = query.where(AlertDelivery.event_name == event_name)
    result = await session.execute(query)
    return list(result.scalars().all())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_alerts.py -k "record_delivery or list_deliveries or set_alert_active or delivery_model" -v`
Expected: 4 PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy app
git add app/services/alerts.py tests/test_alerts.py
git commit -m "feat(alerts): record_delivery, list_deliveries, set_alert_active services"
```

---

### Task 3: Record a delivery row from the notification loop

**Files:**
- Modify: `app/api/ingestion.py:219-231` (the `try: await bot.send_message(...)` block inside `_run_alert_evaluation`)
- Test: `tests/test_alerts.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_alerts.py`:

```python
async def test_notification_success_records_delivery(db_session, session_factory, singleton_user):
    from sqlalchemy import select

    from app.api.ingestion import _run_alert_evaluation
    from app.models.alert_delivery import AlertDelivery

    async with session_factory() as session:
        project, alert = await _seed_project_and_alert(
            session, singleton_user.id, name="deliv-ok.com", event_name="ok_event"
        )
        await session.commit()
        pid, aid = project.id, alert.id

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    with (
        patch("app.api.ingestion.get_session_factory", return_value=session_factory),
        patch("app.bot.setup.get_bot", return_value=mock_bot),
    ):
        await _run_alert_evaluation(pid, "ok_event")

    async with session_factory() as session:
        rows = (
            (await session.execute(select(AlertDelivery).where(AlertDelivery.project_id == pid)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].alert_id == aid
    assert rows[0].delivered is True
    assert rows[0].error is None
    assert rows[0].event_name == "ok_event"


async def test_notification_failure_records_undelivered(
    db_session, session_factory, singleton_user
):
    from sqlalchemy import select

    from app.api.ingestion import _run_alert_evaluation
    from app.models.alert_delivery import AlertDelivery

    async with session_factory() as session:
        project, _ = await _seed_project_and_alert(
            session, singleton_user.id, name="deliv-fail.com", event_name="fail_event"
        )
        await session.commit()
        pid = project.id

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    with (
        patch("app.api.ingestion.get_session_factory", return_value=session_factory),
        patch("app.bot.setup.get_bot", return_value=mock_bot),
    ):
        await _run_alert_evaluation(pid, "fail_event")

    async with session_factory() as session:
        rows = (
            (await session.execute(select(AlertDelivery).where(AlertDelivery.project_id == pid)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].delivered is False
    assert rows[0].error == "RuntimeError"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_alerts.py -k "records_delivery or records_undelivered" -v`
Expected: 2 FAIL with `assert 0 == 1`.

- [ ] **Step 3: Record the row in the loop**

In `app/api/ingestion.py`, inside `_run_alert_evaluation`, replace this block:

```python
                try:
                    await bot.send_message(
                        chat_id=project.admin_chat_id,
                        text=msg,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                except Exception:
                    log.exception(
                        "failed to send alert notification: alert=%s project=%s",
                        alert.id,
                        project_id,
                    )
```

with:

```python
delivered = True
send_error: str | None = None
try:
    await bot.send_message(
        chat_id=project.admin_chat_id,
        text=msg,
        parse_mode="HTML",
        reply_markup=keyboard,
    )
except Exception as exc:
    delivered = False
    send_error = type(exc).__name__
    log.exception(
        "failed to send alert notification: alert=%s project=%s",
        alert.id,
        project_id,
    )
# History row lives in the same transaction as the counter
# update, so a fired alert is never recorded twice or lost.
await record_delivery(session, alert=alert, delivered=delivered, error=send_error)
```

Add the import inside the function, next to the existing local imports at the top of `_run_alert_evaluation`:

```python
    from app.services.alerts import record_delivery
```

- [ ] **Step 4: Run the alert suite**

Run: `uv run pytest tests/test_alerts.py tests/test_e2e.py -v`
Expected: all PASS, including the two new tests.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy app
git add app/api/ingestion.py tests/test_alerts.py
git commit -m "feat(alerts): record every alert notification attempt in alert_deliveries"
```

---

### Task 4: Result schemas for the alert tools

**Files:**
- Modify: `app/mcp/tools/_schemas.py` (append at end)

- [ ] **Step 1: Add the models**

Append to `app/mcp/tools/_schemas.py`:

```python
# ─── Alerts ──────────────────────────────────────────────────────────────────


class AlertInfo(BaseModel):
    """One configured alert."""

    id: str
    project_id: str
    event_name: str
    condition: str = Field(..., description="One of 'every', 'every_n', 'threshold'.")
    threshold_n: int | None = None
    counter: int
    is_active: bool
    muted_until: str | None = None
    created_at: str | None = None


class ListAlertsResult(BaseModel):
    """Result of :func:`list_alerts`."""

    alerts: list[AlertInfo]


class AlertDeliveryRow(BaseModel):
    """One alert notification attempt."""

    id: str
    alert_id: str | None = Field(None, description="NULL when the alert was deleted later.")
    event_name: str
    condition: str
    threshold_n: int | None = None
    fired_at: str
    delivered: bool
    error: str | None = Field(None, description="Exception class name if the send failed.")


class AlertHistoryResult(BaseModel):
    """Result of :func:`alert_history`."""

    project_id: str
    period: str
    rows: list[AlertDeliveryRow]


class DeleteAlertResult(BaseModel):
    """Result of :func:`delete_alert`."""

    deleted: bool
    alert_id: str
```

- [ ] **Step 2: Verify import and commit**

Run: `uv run python -c "from app.mcp.tools._schemas import AlertInfo, ListAlertsResult, AlertDeliveryRow, AlertHistoryResult, DeleteAlertResult; print('ok')"`
Expected: `ok`

```bash
uv run ruff check . && uv run ruff format .
git add app/mcp/tools/_schemas.py
git commit -m "feat(mcp): result schemas for alert tools"
```

---

### Task 5: MCP tools `list_alerts` and `alert_history` (read-only)

**Files:**
- Create: `app/mcp/tools/alerts.py`
- Modify: `app/mcp/tools/__init__.py`
- Modify: `tests/mcp/conftest.py:148-155` (`patch_open_session`)
- Create: `tests/mcp/test_alert_tools.py`

- [ ] **Step 1: Patch `patch_open_session` for the new module**

In `tests/mcp/conftest.py`, inside `patch_open_session`, add the import and setattr:

```python
    import app.mcp.tools.alerts as alerts_mod
    import app.mcp.tools.data as data_mod
    import app.mcp.tools.projects as projects_mod
    import app.mcp.tools.setup as setup_mod

    monkeypatch.setattr(projects_mod, "open_session", _fake)
    monkeypatch.setattr(data_mod, "open_session", _fake)
    monkeypatch.setattr(setup_mod, "open_session", _fake)
    monkeypatch.setattr(alerts_mod, "open_session", _fake)
```

- [ ] **Step 2: Write the failing tests**

Create `tests/mcp/test_alert_tools.py`:

```python
"""Tests for the alert MCP tools.

Same boundaries as ``test_projects_tools.py``: no token → error part;
cross-user → error part (ownership check runs before any service);
happy path forwards to ``app.services.alerts`` with the right kwargs.
Services are mocked; ownership is mocked via ``assert_project_owned_by``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent

from app.mcp.auth import ProjectNotOwnedError
from app.models.alert import AlertCondition
from tests.mcp.conftest import _make_token


def _alert_obj(
    aid,
    pid,
    *,
    event_name="purchase",
    condition=AlertCondition.every,
    threshold_n=None,
    is_active=True,
):
    return SimpleNamespace(
        id=aid,
        project_id=pid,
        event_name=event_name,
        condition=condition,
        threshold_n=threshold_n,
        counter=0,
        is_active=is_active,
        muted_until=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _delivery_obj(did, aid, pid, *, delivered=True, error=None):
    return SimpleNamespace(
        id=did,
        alert_id=aid,
        project_id=pid,
        event_name="purchase",
        condition=AlertCondition.every,
        threshold_n=None,
        fired_at=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        delivered=delivered,
        error=error,
    )


@pytest.fixture
def owned(monkeypatch, project_a_id, user_a_id):
    """Make ``assert_project_owned_by`` succeed and return a project stub."""
    project = SimpleNamespace(id=project_a_id, name="myapp", owner_user_id=user_a_id)
    mock = AsyncMock(return_value=project)
    monkeypatch.setattr("app.mcp.tools.alerts.assert_project_owned_by", mock)
    return mock


@pytest.fixture
def not_owned(monkeypatch):
    mock = AsyncMock(side_effect=ProjectNotOwnedError())
    monkeypatch.setattr("app.mcp.tools.alerts.assert_project_owned_by", mock)
    return mock


def _is_error(result) -> bool:
    return (
        isinstance(result, list)
        and isinstance(result[0], TextContent)
        and result[0].isError is True
    )


# ── list_alerts ─────────────────────────────────────────────────────────────


async def test_list_alerts_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(fresh_mcp, "list_alerts", project_id=str(project_a_id))
    assert _is_error(result)
    assert "not authenticated" in result[0].text


async def test_list_alerts_invalid_uuid(fresh_mcp, call_tool, set_auth_token, user_a_id):
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "list_alerts", project_id="nope")
    assert _is_error(result)
    assert "must be a UUID" in result[0].text


async def test_list_alerts_cross_user(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    not_owned,
    monkeypatch,
    project_a_id,
    user_b_id,
):
    svc = AsyncMock(return_value=[])
    monkeypatch.setattr("app.services.alerts.list_alerts", svc)
    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(fresh_mcp, "list_alerts", project_id=str(project_a_id))
    assert _is_error(result)
    svc.assert_not_awaited()


async def test_list_alerts_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
):
    aid = uuid.uuid4()
    svc = AsyncMock(
        return_value=[
            _alert_obj(aid, project_a_id, condition=AlertCondition.every_n, threshold_n=5)
        ]
    )
    monkeypatch.setattr("app.services.alerts.list_alerts", svc)
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "list_alerts", project_id=str(project_a_id))
    assert isinstance(result, dict)
    assert result["alerts"] == [
        {
            "id": str(aid),
            "project_id": str(project_a_id),
            "event_name": "purchase",
            "condition": "every_n",
            "threshold_n": 5,
            "counter": 0,
            "is_active": True,
            "muted_until": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    svc.assert_awaited_once()
    assert svc.await_args.args[1] == project_a_id


# ── alert_history ───────────────────────────────────────────────────────────


async def test_alert_history_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(fresh_mcp, "alert_history", project_id=str(project_a_id))
    assert _is_error(result)


async def test_alert_history_bad_period(
    fresh_mcp, call_tool, set_auth_token, patch_open_session, owned, user_a_id, project_a_id
):
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp, "alert_history", project_id=str(project_a_id), period="2w"
        )
    assert _is_error(result)
    assert "unsupported period" in result[0].text


async def test_alert_history_bad_limit(
    fresh_mcp, call_tool, set_auth_token, patch_open_session, owned, user_a_id, project_a_id
):
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "alert_history", project_id=str(project_a_id), limit=0)
    assert _is_error(result)
    assert "limit" in result[0].text


async def test_alert_history_cross_user(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    not_owned,
    monkeypatch,
    project_a_id,
    user_b_id,
):
    svc = AsyncMock(return_value=[])
    monkeypatch.setattr("app.services.alerts.list_deliveries", svc)
    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(fresh_mcp, "alert_history", project_id=str(project_a_id))
    assert _is_error(result)
    svc.assert_not_awaited()


async def test_alert_history_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
):
    did, aid = uuid.uuid4(), uuid.uuid4()
    svc = AsyncMock(
        return_value=[_delivery_obj(did, aid, project_a_id, delivered=False, error="TimedOut")]
    )
    monkeypatch.setattr("app.services.alerts.list_deliveries", svc)
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "alert_history",
            project_id=str(project_a_id),
            period="30d",
            limit=5,
            event_name="purchase",
        )
    assert result["project_id"] == str(project_a_id)
    assert result["period"] == "30d"
    assert result["rows"] == [
        {
            "id": str(did),
            "alert_id": str(aid),
            "event_name": "purchase",
            "condition": "every",
            "threshold_n": None,
            "fired_at": "2026-09-05T12:00:00+00:00",
            "delivered": False,
            "error": "TimedOut",
        }
    ]
    svc.assert_awaited_once()
    kwargs = svc.await_args.kwargs
    assert svc.await_args.args[1] == project_a_id
    assert kwargs["limit"] == 5
    assert kwargs["event_name"] == "purchase"
    assert (datetime.now(UTC) - kwargs["since"]).days in (29, 30)
```

Check `user_b_id` exists in `tests/mcp/conftest.py` (`grep -n user_b_id tests/mcp/conftest.py`). If it does not, add next to `user_a_id`:

```python
@pytest.fixture
def user_b_id() -> uuid.UUID:
    return uuid.UUID("22222222-2222-2222-2222-222222222222")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/mcp/test_alert_tools.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'app.mcp.tools.alerts'` (from the conftest patch).

- [ ] **Step 4: Create the tools module with the two read-only tools**

Create `app/mcp/tools/alerts.py`:

```python
"""Alert MCP tools.

Five handlers:

- ``list_alerts`` — alerts configured on a project.
- ``alert_history`` — notification attempts recorded in ``alert_deliveries``.
- ``create_alert`` — create an alert directly (no bot approval step).
- ``set_alert_active`` — pause / resume an alert.
- ``delete_alert`` — remove an alert (audited).

Same shape as ``app.mcp.tools.projects``: read the token, parse ids,
``assert_project_owned_by`` before any service call, call
``app.services.alerts``, return a Pydantic result or
``[TextContent(isError=True)]``. Handlers never raise.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ToolAnnotations
from pydantic import ValidationError

from app.mcp.auth import (
    MCPAccessToken,
    ProjectNotOwnedError,
    assert_project_owned_by,
)
from app.mcp.tools._periods import InvalidPeriodError, period_to_window
from app.mcp.tools._schemas import (
    AlertDeliveryRow,
    AlertHistoryResult,
    AlertInfo,
    DeleteAlertResult,
    ListAlertsResult,
)
from app.mcp.tools._session import open_session

logger = logging.getLogger("app.mcp.tools")

_READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
_CREATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
_SET_ACTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_DELETE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)

_MAX_HISTORY_LIMIT = 500


def _error(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text, isError=True)]  # type: ignore[call-arg]


def _not_authenticated() -> list[TextContent]:
    return _error("not authenticated")


def _not_owned_error(project_id: str) -> list[TextContent]:
    return _error(f"project {project_id} not found or you don't have access")


def _parse_uuid(value: str, label: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


def _alert_to_info(alert: Any) -> AlertInfo:
    return AlertInfo(
        id=str(alert.id),
        project_id=str(alert.project_id),
        event_name=alert.event_name,
        condition=str(getattr(alert.condition, "value", alert.condition)),
        threshold_n=alert.threshold_n,
        counter=alert.counter,
        is_active=alert.is_active,
        muted_until=_iso(alert.muted_until),
        created_at=_iso(alert.created_at),
    )


def _delivery_to_row(row: Any) -> AlertDeliveryRow:
    return AlertDeliveryRow(
        id=str(row.id),
        alert_id=str(row.alert_id) if row.alert_id is not None else None,
        event_name=row.event_name,
        condition=str(getattr(row.condition, "value", row.condition)),
        threshold_n=row.threshold_n,
        fired_at=row.fired_at.isoformat(),
        delivered=row.delivered,
        error=row.error,
    )


def register_alert_tools(mcp: FastMCP) -> None:
    """Register the alert tools onto *mcp*."""

    @mcp.tool(title="List alerts", annotations=_READ_ONLY)
    async def list_alerts(project_id: str) -> list[TextContent] | ListAlertsResult:
        """Return every alert configured on *project_id*.

        Each row carries the condition (``every`` / ``every_n`` /
        ``threshold``), ``threshold_n``, whether it is active, and
        ``muted_until`` if it is silenced.
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()
        pid = _parse_uuid(project_id, "project_id")
        if pid is None:
            return _error(f"invalid project_id {project_id!r}; must be a UUID")
        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.services.alerts import list_alerts as svc_list_alerts

        async with open_session() as session:
            try:
                await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)
            alerts = await svc_list_alerts(session, pid)

        return ListAlertsResult(alerts=[_alert_to_info(a) for a in alerts])

    @mcp.tool(title="Alert history", annotations=_READ_ONLY)
    async def alert_history(
        project_id: str,
        period: str = "7d",
        limit: int = 50,
        event_name: str | None = None,
    ) -> list[TextContent] | AlertHistoryResult:
        """Return the alert notifications sent for *project_id*, newest first.

        ``period`` is one of ``7d`` / ``30d`` / ``90d`` / ``1y``. ``limit``
        is 1..500. ``event_name`` filters to one event. ``delivered`` is
        false when the Telegram send failed; ``error`` then holds the
        exception class name.
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()
        pid = _parse_uuid(project_id, "project_id")
        if pid is None:
            return _error(f"invalid project_id {project_id!r}; must be a UUID")
        if not 1 <= limit <= _MAX_HISTORY_LIMIT:
            return _error(f"limit must be between 1 and {_MAX_HISTORY_LIMIT}")
        try:
            since, _end = period_to_window(period)
        except InvalidPeriodError as exc:
            return _error(str(exc))
        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.services.alerts import list_deliveries

        async with open_session() as session:
            try:
                await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)
            rows = await list_deliveries(
                session, pid, since=since, limit=limit, event_name=event_name
            )

        return AlertHistoryResult(
            project_id=str(pid),
            period=period,
            rows=[_delivery_to_row(r) for r in rows],
        )
```

(The write tools are added in Task 6. `ValidationError`, `_CREATE`, `_SET_ACTIVE`, `_DELETE`, `DeleteAlertResult` are imported now so ruff does not flag them later; if ruff flags unused imports at this step, keep them and add `# noqa: F401` temporarily, removed in Task 6.)

- [ ] **Step 5: Register the module**

In `app/mcp/tools/__init__.py`, add the import and call:

```python
from app.mcp.tools.alerts import register_alert_tools
from app.mcp.tools.data import register_data_tools
from app.mcp.tools.projects import register_project_tools
from app.mcp.tools.setup import register_setup_tools


def register_all_tools(mcp: FastMCP) -> None:
    register_project_tools(mcp)
    register_data_tools(mcp)
    register_setup_tools(mcp)
    register_alert_tools(mcp)
```

Also add to the module docstring list:

```
- Alerts:
  ``list_alerts``, ``alert_history``, ``create_alert``,
  ``set_alert_active``, ``delete_alert``.
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/mcp/test_alert_tools.py -v`
Expected: all `list_alerts` and `alert_history` tests PASS.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy app
git add app/mcp/tools/alerts.py app/mcp/tools/__init__.py tests/mcp/conftest.py tests/mcp/test_alert_tools.py
git commit -m "feat(mcp): list_alerts and alert_history tools"
```

---

### Task 6: MCP tools `create_alert`, `set_alert_active`, `delete_alert`

**Files:**
- Modify: `app/mcp/tools/alerts.py`
- Modify: `tests/mcp/test_alert_tools.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/mcp/test_alert_tools.py`:

```python
# ── create_alert ────────────────────────────────────────────────────────────


async def test_create_alert_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(
            fresh_mcp,
            "create_alert",
            project_id=str(project_a_id),
            event_name="x",
            condition="every",
        )
    assert _is_error(result)


async def test_create_alert_cross_user(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    not_owned,
    monkeypatch,
    project_a_id,
    user_b_id,
):
    svc = AsyncMock()
    monkeypatch.setattr("app.services.alerts.create_alert", svc)
    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(
            fresh_mcp,
            "create_alert",
            project_id=str(project_a_id),
            event_name="x",
            condition="every",
        )
    assert _is_error(result)
    svc.assert_not_awaited()


async def test_create_alert_every_n_requires_threshold(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
):
    svc = AsyncMock()
    monkeypatch.setattr("app.services.alerts.create_alert", svc)
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "create_alert",
            project_id=str(project_a_id),
            event_name="signup",
            condition="every_n",
        )
    assert _is_error(result)
    assert "threshold_n is required" in result[0].text
    svc.assert_not_awaited()


async def test_create_alert_bad_condition(
    fresh_mcp, call_tool, set_auth_token, patch_open_session, owned, user_a_id, project_a_id
):
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "create_alert",
            project_id=str(project_a_id),
            event_name="signup",
            condition="sometimes",
        )
    assert _is_error(result)
    assert "condition" in result[0].text


async def test_create_alert_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
    mock_session,
):
    aid = uuid.uuid4()
    svc = AsyncMock(
        return_value=_alert_obj(
            aid, project_a_id, event_name="signup", condition=AlertCondition.every_n, threshold_n=10
        )
    )
    monkeypatch.setattr("app.services.alerts.create_alert", svc)
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "create_alert",
            project_id=str(project_a_id),
            event_name="signup",
            condition="every_n",
            threshold_n=10,
        )
    assert result["id"] == str(aid)
    assert result["condition"] == "every_n"
    assert result["threshold_n"] == 10
    svc.assert_awaited_once()
    kwargs = svc.await_args.kwargs
    assert kwargs["project_id"] == project_a_id
    assert kwargs["event_name"] == "signup"
    assert kwargs["condition"] == AlertCondition.every_n
    assert kwargs["threshold_n"] == 10
    mock_session.commit.assert_awaited_once()


# ── set_alert_active ────────────────────────────────────────────────────────


async def test_set_alert_active_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(
            fresh_mcp,
            "set_alert_active",
            project_id=str(project_a_id),
            alert_id=str(uuid.uuid4()),
            is_active=False,
        )
    assert _is_error(result)


async def test_set_alert_active_bad_alert_uuid(
    fresh_mcp, call_tool, set_auth_token, patch_open_session, owned, user_a_id, project_a_id
):
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "set_alert_active",
            project_id=str(project_a_id),
            alert_id="nope",
            is_active=False,
        )
    assert _is_error(result)
    assert "alert_id" in result[0].text


async def test_set_alert_active_cross_user(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    not_owned,
    monkeypatch,
    project_a_id,
    user_b_id,
):
    svc = AsyncMock()
    monkeypatch.setattr("app.services.alerts.set_alert_active", svc)
    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(
            fresh_mcp,
            "set_alert_active",
            project_id=str(project_a_id),
            alert_id=str(uuid.uuid4()),
            is_active=False,
        )
    assert _is_error(result)
    svc.assert_not_awaited()


async def test_set_alert_active_not_found(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
):
    monkeypatch.setattr("app.services.alerts.set_alert_active", AsyncMock(return_value=None))
    aid = uuid.uuid4()
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "set_alert_active",
            project_id=str(project_a_id),
            alert_id=str(aid),
            is_active=False,
        )
    assert _is_error(result)
    assert f"alert {aid} not found" in result[0].text


async def test_set_alert_active_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
    mock_session,
):
    aid = uuid.uuid4()
    svc = AsyncMock(return_value=_alert_obj(aid, project_a_id, is_active=False))
    monkeypatch.setattr("app.services.alerts.set_alert_active", svc)
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "set_alert_active",
            project_id=str(project_a_id),
            alert_id=str(aid),
            is_active=False,
        )
    assert result["id"] == str(aid)
    assert result["is_active"] is False
    svc.assert_awaited_once()
    assert svc.await_args.args[1:] == (aid, project_a_id)
    assert svc.await_args.kwargs == {"is_active": False}
    mock_session.commit.assert_awaited_once()


# ── delete_alert ────────────────────────────────────────────────────────────


async def test_delete_alert_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(
            fresh_mcp, "delete_alert", project_id=str(project_a_id), alert_id=str(uuid.uuid4())
        )
    assert _is_error(result)


async def test_delete_alert_cross_user(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    not_owned,
    monkeypatch,
    project_a_id,
    user_b_id,
):
    svc = AsyncMock()
    monkeypatch.setattr("app.services.alerts.delete_alert", svc)
    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(
            fresh_mcp, "delete_alert", project_id=str(project_a_id), alert_id=str(uuid.uuid4())
        )
    assert _is_error(result)
    svc.assert_not_awaited()


async def test_delete_alert_not_found(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
):
    monkeypatch.setattr("app.services.alerts.get_alert", AsyncMock(return_value=None))
    audit = AsyncMock()
    monkeypatch.setattr("app.services.audit.write_audit", audit)
    aid = uuid.uuid4()
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp, "delete_alert", project_id=str(project_a_id), alert_id=str(aid)
        )
    assert _is_error(result)
    assert f"alert {aid} not found" in result[0].text
    audit.assert_not_awaited()


async def test_delete_alert_happy_path_writes_audit(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
    mock_session,
):
    aid = uuid.uuid4()
    monkeypatch.setattr(
        "app.services.alerts.get_alert",
        AsyncMock(return_value=_alert_obj(aid, project_a_id, event_name="signup")),
    )
    delete_svc = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.alerts.delete_alert", delete_svc)
    audit = AsyncMock()
    monkeypatch.setattr("app.services.audit.write_audit", audit)

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp, "delete_alert", project_id=str(project_a_id), alert_id=str(aid)
        )

    assert result == {"deleted": True, "alert_id": str(aid)}
    delete_svc.assert_awaited_once()
    assert delete_svc.await_args.args[1:] == (aid, project_a_id)
    audit.assert_awaited_once()
    akw = audit.await_args.kwargs
    assert akw["user_id"] == user_a_id
    assert akw["action"] == "alert.delete"
    assert akw["target_type"] == "alert"
    assert akw["target_id"] == str(aid)
    assert akw["metadata"] == {
        "project_id": str(project_a_id),
        "event_name": "signup",
        "condition": "every",
        "via": "mcp",
    }
    mock_session.commit.assert_awaited_once()
```

The `mock_session` fixture yields a bare `object()`, which has no `commit`. Change it in `tests/mcp/conftest.py` so write tools can be asserted:

```python
@pytest_asyncio.fixture
async def mock_session() -> AsyncIterator[Any]:
    """A session stub — tools call services that are themselves mocked.

    ``commit`` is an ``AsyncMock`` so write tools can assert they committed.
    """
    yield MagicMock(commit=AsyncMock(), rollback=AsyncMock())
```

Keep the same decorator the existing fixture uses (check the line above `async def mock_session`; it is `@pytest_asyncio.fixture` or `@pytest.fixture`). `MagicMock` and `AsyncMock` are already imported in that conftest.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/mcp/test_alert_tools.py -v`
Expected: the new tests FAIL with `KeyError`/`Unknown tool: create_alert` (from `_tool_manager.get_tool`); the Task 5 tests still PASS.

- [ ] **Step 3: Add the three write tools**

Append inside `register_alert_tools` in `app/mcp/tools/alerts.py`, after `alert_history`:

```python
@mcp.tool(title="Create alert", annotations=_CREATE)
async def create_alert(
    project_id: str,
    event_name: str,
    condition: str,
    threshold_n: int | None = None,
) -> list[TextContent] | AlertInfo:
    """Create an alert on *project_id*. The user gets a Telegram message when it fires.

    ``condition``: ``every`` (ping on each event), ``every_n`` (ping
    every N-th event; needs ``threshold_n``), ``threshold`` (ping once
    per day when the daily count reaches N; needs ``threshold_n``).
    Prefer ``every_n`` or ``threshold`` for high-volume events such as
    ``pageview``.
    """
    token = get_access_token()
    if token is None or not isinstance(token, MCPAccessToken):
        return _not_authenticated()
    pid = _parse_uuid(project_id, "project_id")
    if pid is None:
        return _error(f"invalid project_id {project_id!r}; must be a UUID")
    owner_user_id = uuid.UUID(token.extra["user_id"])

    from app.schemas.alert import AlertCreate
    from app.services.alerts import create_alert as svc_create_alert

    try:
        body = AlertCreate(
            project_id=pid,
            event_name=event_name,
            condition=condition,  # type: ignore[arg-type]
            threshold_n=threshold_n,
        )
    except ValidationError as exc:
        msgs = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
        )
        return _error(f"invalid alert: {msgs}")

    async with open_session() as session:
        try:
            await assert_project_owned_by(session, pid, owner_user_id)
        except ProjectNotOwnedError:
            return _not_owned_error(project_id)
        alert = await svc_create_alert(
            session,
            project_id=pid,
            event_name=body.event_name,
            condition=body.condition,
            threshold_n=body.threshold_n,
        )
        await session.commit()

    logger.info("created alert %s on project_id=%s via mcp", alert.id, pid)
    return _alert_to_info(alert)


@mcp.tool(title="Pause or resume alert", annotations=_SET_ACTIVE)
async def set_alert_active(
    project_id: str,
    alert_id: str,
    is_active: bool,
) -> list[TextContent] | AlertInfo:
    """Set an alert active (``true``) or paused (``false``). Idempotent."""
    token = get_access_token()
    if token is None or not isinstance(token, MCPAccessToken):
        return _not_authenticated()
    pid = _parse_uuid(project_id, "project_id")
    if pid is None:
        return _error(f"invalid project_id {project_id!r}; must be a UUID")
    aid = _parse_uuid(alert_id, "alert_id")
    if aid is None:
        return _error(f"invalid alert_id {alert_id!r}; must be a UUID")
    owner_user_id = uuid.UUID(token.extra["user_id"])

    from app.services.alerts import set_alert_active as svc_set_alert_active

    async with open_session() as session:
        try:
            await assert_project_owned_by(session, pid, owner_user_id)
        except ProjectNotOwnedError:
            return _not_owned_error(project_id)
        alert = await svc_set_alert_active(session, aid, pid, is_active=is_active)
        if alert is None:
            return _error(f"alert {alert_id} not found on project {project_id}")
        await session.commit()

    return _alert_to_info(alert)


@mcp.tool(title="Delete alert", annotations=_DELETE)
async def delete_alert(
    project_id: str,
    alert_id: str,
) -> list[TextContent] | DeleteAlertResult:
    """Delete an alert. History rows in ``alert_history`` are kept."""
    token = get_access_token()
    if token is None or not isinstance(token, MCPAccessToken):
        return _not_authenticated()
    pid = _parse_uuid(project_id, "project_id")
    if pid is None:
        return _error(f"invalid project_id {project_id!r}; must be a UUID")
    aid = _parse_uuid(alert_id, "alert_id")
    if aid is None:
        return _error(f"invalid alert_id {alert_id!r}; must be a UUID")
    owner_user_id = uuid.UUID(token.extra["user_id"])

    from app.services.alerts import delete_alert as svc_delete_alert
    from app.services.alerts import get_alert
    from app.services.audit import write_audit

    async with open_session() as session:
        try:
            await assert_project_owned_by(session, pid, owner_user_id)
        except ProjectNotOwnedError:
            return _not_owned_error(project_id)
        alert = await get_alert(session, aid, pid)
        if alert is None:
            return _error(f"alert {alert_id} not found on project {project_id}")
        snapshot = {
            "project_id": str(pid),
            "event_name": alert.event_name,
            "condition": str(getattr(alert.condition, "value", alert.condition)),
            "via": "mcp",
        }
        deleted = await svc_delete_alert(session, aid, pid)
        await write_audit(
            session,
            user_id=owner_user_id,
            action="alert.delete",
            target_type="alert",
            target_id=str(aid),
            metadata=snapshot,
        )
        await session.commit()

    logger.info("deleted alert %s on project_id=%s via mcp", aid, pid)
    return DeleteAlertResult(deleted=bool(deleted), alert_id=str(aid))
```

Remove any temporary `# noqa: F401` added in Task 5.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mcp/test_alert_tools.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy app
git add app/mcp/tools/alerts.py tests/mcp/test_alert_tools.py tests/mcp/conftest.py
git commit -m "feat(mcp): create_alert, set_alert_active, delete_alert tools"
```

---

### Task 7: Register the new tools in the annotation and output-schema contract tests

**Files:**
- Modify: `tests/mcp/test_tool_annotations.py:27-88` (`EXPECTED` dict)
- Modify: `tests/mcp/test_tool_output_schema.py:58-71` (`ALL_TOOLS` list)

- [ ] **Step 1: Add expected annotations**

In `tests/mcp/test_tool_annotations.py`, append to `EXPECTED` before the closing `}`:

```python
    "list_alerts": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "alert_history": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "create_alert": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "set_alert_active": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    # delete_alert is destructive. Clients should prompt the user first.
    "delete_alert": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
```

- [ ] **Step 2: Add tool names to the output-schema list**

In `tests/mcp/test_tool_output_schema.py`, append to `ALL_TOOLS`:

```python
("list_alerts",)
("alert_history",)
("create_alert",)
("set_alert_active",)
("delete_alert",)
```

- [ ] **Step 3: Run the contract tests**

Run: `uv run pytest tests/mcp/test_tool_annotations.py tests/mcp/test_tool_output_schema.py -v`
Expected: all PASS. If a test in `test_tool_annotations.py` asserts the total tool count, update that number by +5.

- [ ] **Step 4: Commit**

```bash
git add tests/mcp/test_tool_annotations.py tests/mcp/test_tool_output_schema.py
git commit -m "test(mcp): cover alert tools in annotation and output-schema contracts"
```

---

### Task 8: Docs, full suite, OpenAPI check, PR

**Files:**
- Modify: `README.md` (MCP section, find with `grep -n "list_projects\|Connect Claude" README.md`; if no per-tool list exists, add a short "Alert tools" bullet list under the MCP section)
- Modify: `app/mcp/server.py:198` (server instructions string, if it enumerates tool groups)

- [ ] **Step 1: Document the tools**

In `README.md`, where the MCP tools are listed, add one line per tool:

```markdown
- `list_alerts(project_id)` — alerts configured on a project.
- `alert_history(project_id, period="7d", limit=50, event_name=None)` — notifications sent, newest first, with delivered/error status.
- `create_alert(project_id, event_name, condition, threshold_n=None)` — create an alert (`every`, `every_n`, `threshold`).
- `set_alert_active(project_id, alert_id, is_active)` — pause or resume an alert.
- `delete_alert(project_id, alert_id)` — delete an alert (audited; history kept).
```

If `app/mcp/server.py` instructions text lists tool groups, add "manage alerts and read alert history".

- [ ] **Step 2: Run everything**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy app
uv run python scripts/export_openapi.py --check
uv run pytest
```
Expected: lint clean, mypy clean, OpenAPI unchanged (MCP tools are not in the FastAPI OpenAPI; if the check fails, run `uv run python scripts/export_openapi.py` and commit `openapi.json`), pytest all PASS with no skips in `tests/test_alerts.py`.

- [ ] **Step 3: Commit and push**

```bash
git add README.md app/mcp/server.py openapi.json
git commit -m "docs(mcp): document alert tools"
git branch --show-current   # must print worktree-mcp-alerts
git push -u origin worktree-mcp-alerts
```

- [ ] **Step 4: Open the PR**

```bash
gh pr create --repo tgram-analytics/server --base main --title "feat(mcp): alert tools + alert delivery history" --body-file - <<'EOF'
## What

- New `alert_deliveries` table (migration 0013): one row per alert notification attempt, with `delivered` and `error`.
- The ingestion notification loop records a row for every fired alert, success or failure.
- Five MCP tools: `list_alerts`, `alert_history`, `create_alert`, `set_alert_active`, `delete_alert`.

## Deploy

Migration runs on start. No env changes. After merge: redeploy `tgram-analytics-cloud` in Coolify.

Spec: docs/superpowers/specs/2026-09-05-mcp-alerts-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
```

Print the PR URL. Wait for CI green (`gh pr checks <n> --watch`).

---

### Task 9: Deploy and verify live

- [ ] **Step 1: Merge** (after CI is green and the user approves the merge)

```bash
gh pr merge <n> --repo tgram-analytics/server --squash --delete-branch
```

- [ ] **Step 2: Redeploy the cloud app**

```bash
curl -s -X GET -H "Authorization: Bearer $COOLIFY_VPS_API_TOKEN" "http://100.81.240.56:8000/api/v1/deploy?uuid=egck4wowko8s4kckw4ogswoc&force=true"
```
Then poll until healthy:
```bash
curl -s -H "Authorization: Bearer $COOLIFY_VPS_API_TOKEN" http://100.81.240.56:8000/api/v1/applications/egck4wowko8s4kckw4ogswoc | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])"
```
Expected: `running:healthy`.

- [ ] **Step 3: Verify by content, not by hash**

From an interactive Claude session with the `tgram` MCP connected (`https://tg-analytics.leorigna.com/mcp`):
1. Call `list_alerts` on a real project. Expected: a list (possibly empty), not "unknown tool".
2. Call `create_alert` with `condition="every_n"`, `threshold_n=1000`, `event_name="mcp_smoke"`.
3. Call `alert_history` with `period="7d"`. Expected: rows list, empty for `mcp_smoke`.
4. Call `delete_alert` on the smoke alert. Expected: `{"deleted": true}`.

If the MCP client caches the tool list, restart the session once.

Report to the user: the PR URL, the deploy status, and the results of the four calls.

"""Audit log: insert, append-only enforcement, and create_project integration."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from app.models.audit import AuditEvent
from app.services.audit import write_audit


@pytest.mark.asyncio
async def test_write_audit_inserts_row(singleton_user, db_session):
    row = await write_audit(
        db_session,
        user_id=singleton_user.id,
        action="test.action",
        target_type="thing",
        target_id="abc123",
        metadata={"k": "v"},
    )
    await db_session.flush()

    fetched = await db_session.execute(sa.select(AuditEvent).where(AuditEvent.id == row.id))
    audit = fetched.scalar_one()
    assert audit.action == "test.action"
    assert audit.target_type == "thing"
    assert audit.target_id == "abc123"
    assert audit.metadata_json == {"k": "v"}
    assert audit.user_id == singleton_user.id


@pytest.mark.asyncio
async def test_update_audit_event_is_blocked(singleton_user, db_session):
    row = await write_audit(
        db_session,
        user_id=singleton_user.id,
        action="test.action",
        target_type="thing",
    )
    await db_session.flush()

    with pytest.raises(DBAPIError, match="audit_events is append-only"):
        await db_session.execute(
            sa.text("UPDATE audit_events SET action = 'tampered' WHERE id = :id"),
            {"id": row.id},
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_delete_audit_event_is_blocked(singleton_user, db_session):
    await write_audit(
        db_session,
        user_id=singleton_user.id,
        action="test.action",
        target_type="thing",
    )
    await db_session.flush()

    with pytest.raises(DBAPIError, match="audit_events is append-only"):
        await db_session.execute(sa.text("DELETE FROM audit_events"))
    await db_session.rollback()


@pytest.mark.asyncio
async def test_create_project_writes_audit_row(singleton_user, db_session):
    from app.services.projects import create_project

    project, _api_key = await create_project(
        db_session,
        name="My Project",
        admin_chat_id=singleton_user.telegram_user_id,
        owner_user_id=singleton_user.id,
    )
    await db_session.flush()

    rows = await db_session.execute(
        sa.select(AuditEvent).where(
            AuditEvent.target_type == "project",
            AuditEvent.target_id == str(project.id),
        )
    )
    audit = rows.scalar_one()
    assert audit.action == "project.create"
    assert audit.user_id == singleton_user.id
    assert audit.metadata_json == {"name": "My Project"}

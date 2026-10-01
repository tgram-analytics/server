"""Events marked as test: stored, excluded from analytics, shown in debug views."""

from __future__ import annotations

ADMIN_ID = 111


async def _project(session, owner_user_id, name: str):
    from app.services.projects import create_project

    project, _ = await create_project(
        session, name=name, admin_chat_id=ADMIN_ID, owner_user_id=owner_user_id
    )
    await session.flush()
    return project


async def test_insert_event_stores_is_test(singleton_user, db_session):
    from app.services.events import insert_event

    project = await _project(db_session, singleton_user.id, "is-test-insert.com")
    real = await insert_event(
        db_session, project_id=project.id, event_name="signup", session_id="s1", properties={}
    )
    test = await insert_event(
        db_session,
        project_id=project.id,
        event_name="signup",
        session_id="s2",
        properties={},
        is_test=True,
    )
    assert real.is_test is False
    assert test.is_test is True

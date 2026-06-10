"""KPI feature tests — CRUD service, digest block, bot handlers.

Follows the patterns of test_alerts.py: service tests run against the
real test DB; handler tests use fake Update/CallbackQuery objects.
"""

ADMIN_ID = 111


async def _make_project(session, singleton_user, name="kpi-test.com"):
    from app.services.projects import create_project

    project, _ = await create_project(
        session,
        name=name,
        admin_chat_id=ADMIN_ID,
        owner_user_id=singleton_user.id,
    )
    return project


# ── KPI CRUD service tests ────────────────────────────────────────────────────


async def test_add_and_list_kpis_in_position_order(db_session, session_factory, singleton_user):
    from app.services.kpis import add_kpi, list_kpis

    async with session_factory() as session:
        project = await _make_project(session, singleton_user)
        await add_kpi(session, project_id=project.id, event_name="signup")
        await add_kpi(session, project_id=project.id, event_name="checkout")
        await session.commit()

        kpis = await list_kpis(session, project_id=project.id)
        assert [k.event_name for k in kpis] == ["signup", "checkout"]
        assert all(not k.is_north_star for k in kpis)


async def test_add_kpi_is_idempotent(db_session, session_factory, singleton_user):
    from app.services.kpis import add_kpi, list_kpis

    async with session_factory() as session:
        project = await _make_project(session, singleton_user)
        first = await add_kpi(session, project_id=project.id, event_name="signup")
        second = await add_kpi(session, project_id=project.id, event_name="signup")
        await session.commit()

        assert first.id == second.id
        assert len(await list_kpis(session, project_id=project.id)) == 1


async def test_set_north_star_switches_flag(db_session, session_factory, singleton_user):
    from app.services.kpis import add_kpi, list_kpis, set_north_star

    async with session_factory() as session:
        project = await _make_project(session, singleton_user)
        await add_kpi(session, project_id=project.id, event_name="signup")
        await add_kpi(session, project_id=project.id, event_name="checkout")
        await set_north_star(session, project_id=project.id, event_name="signup")
        # Switching must clear the previous flag (partial unique index).
        await set_north_star(session, project_id=project.id, event_name="checkout")
        await session.commit()

        kpis = await list_kpis(session, project_id=project.id)
        stars = [k.event_name for k in kpis if k.is_north_star]
        assert stars == ["checkout"]
        # North Star sorts first in list_kpis.
        assert kpis[0].event_name == "checkout"


async def test_set_north_star_pins_unpinned_event(db_session, session_factory, singleton_user):
    from app.services.kpis import list_kpis, set_north_star

    async with session_factory() as session:
        project = await _make_project(session, singleton_user)
        await set_north_star(session, project_id=project.id, event_name="signup")
        await session.commit()

        kpis = await list_kpis(session, project_id=project.id)
        assert len(kpis) == 1
        assert kpis[0].event_name == "signup"
        assert kpis[0].is_north_star


async def test_remove_kpi(db_session, session_factory, singleton_user):
    from app.services.kpis import add_kpi, get_kpi, list_kpis, remove_kpi

    async with session_factory() as session:
        project = await _make_project(session, singleton_user)
        kpi = await add_kpi(session, project_id=project.id, event_name="signup")
        await remove_kpi(session, kpi.id)
        await session.commit()

        assert await list_kpis(session, project_id=project.id) == []
        assert await get_kpi(session, kpi.id) is None

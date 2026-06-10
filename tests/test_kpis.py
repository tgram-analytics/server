"""KPI feature tests — CRUD service, digest block, bot handlers.

Follows the patterns of test_alerts.py: service tests run against the
real test DB; handler tests use fake Update/CallbackQuery objects.
"""

from datetime import UTC, datetime, timedelta

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


# ── Digest KPI block tests ────────────────────────────────────────────────────


async def _seed_event(session, project_id, event_name, *, days_ago, visitor="v1", n=1):
    from app.models.event import Event

    ts = datetime.now(UTC) - timedelta(days=days_ago)
    for i in range(n):
        session.add(
            Event(
                project_id=project_id,
                event_name=event_name,
                session_id=f"s-{visitor}-{days_ago}-{i}",
                visitor_hash=visitor,
                timestamp=ts,
            )
        )
    await session.flush()


async def test_digest_block_ordering_and_dedup(db_session, session_factory, singleton_user):
    """North Star → Visitors → Sessions → Pageviews → pinned KPIs → alerted (deduped)."""
    from app.bot.handlers.digest import _project_digest_lines
    from app.models.alert import AlertCondition
    from app.services.alerts import create_alert
    from app.services.kpis import add_kpi, set_north_star

    async with session_factory() as session:
        project = await _make_project(session, singleton_user, name="digest-kpi.com")
        await set_north_star(session, project_id=project.id, event_name="signup")
        await add_kpi(session, project_id=project.id, event_name="checkout")
        # "signup" is both a KPI and alerted — must appear once (as ⭐).
        await create_alert(
            session, project_id=project.id, event_name="signup", condition=AlertCondition.every
        )
        await create_alert(
            session, project_id=project.id, event_name="invite", condition=AlertCondition.every
        )
        await _seed_event(session, project.id, "signup", days_ago=2, n=3)
        await _seed_event(session, project.id, "checkout", days_ago=2, n=2)
        await _seed_event(session, project.id, "invite", days_ago=2, n=1)
        await _seed_event(session, project.id, "pageview", days_ago=2, n=5)
        await session.commit()

        lines = await _project_digest_lines(session, project, datetime.now(UTC))

    text = "\n".join(lines)
    # Section order.
    assert text.index("⭐ signup") < text.index("👥 Visitors")
    assert text.index("👥 Visitors") < text.index("👤 Sessions")
    assert text.index("👤 Sessions") < text.index("📄 Pageviews")
    assert text.index("📄 Pageviews") < text.index("🎯 checkout")
    assert text.index("🎯 checkout") < text.index("invite")
    # Counts.
    assert "⭐ signup: <b>3</b>" in text
    assert "🎯 checkout: <b>2</b>" in text
    assert "📄 Pageviews: <b>5</b>" in text
    # Dedup: signup appears exactly once.
    assert text.count("signup") == 1


async def test_digest_hides_pageviews_when_zero(db_session, session_factory, singleton_user):
    from app.bot.handlers.digest import _project_digest_lines
    from app.services.kpis import set_north_star

    async with session_factory() as session:
        project = await _make_project(session, singleton_user, name="sdk-only.com")
        await set_north_star(session, project_id=project.id, event_name="signup")
        await _seed_event(session, project.id, "signup", days_ago=2, n=1)
        await session.commit()

        lines = await _project_digest_lines(session, project, datetime.now(UTC))

    text = "\n".join(lines)
    assert "Pageviews" not in text
    assert "👥 Visitors" in text
    assert "👤 Sessions" in text


async def test_digest_hint_when_no_north_star(db_session, session_factory, singleton_user):
    from app.bot.handlers.digest import _project_digest_lines

    async with session_factory() as session:
        project = await _make_project(session, singleton_user, name="no-star.com")
        await _seed_event(session, project.id, "signup", days_ago=2, n=1)
        await session.commit()

        lines = await _project_digest_lines(session, project, datetime.now(UTC))

    text = "\n".join(lines)
    assert "⭐" not in text
    assert "Pin your North Star" in text


async def test_digest_pinned_kpi_with_zero_events_still_renders(
    db_session, session_factory, singleton_user
):
    from app.bot.handlers.digest import _project_digest_lines
    from app.services.kpis import set_north_star

    async with session_factory() as session:
        project = await _make_project(session, singleton_user, name="zero-kpi.com")
        await set_north_star(session, project_id=project.id, event_name="never_fired")
        await session.commit()

        lines = await _project_digest_lines(session, project, datetime.now(UTC))

    text = "\n".join(lines)
    assert "⭐ never_fired: <b>0</b>" in text

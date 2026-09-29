"""Tests for the /heatmap bot flow (app/bot/handlers/heatmap.py).

CallbackQuery objects are mocked with MagicMock/AsyncMock and the handlers
are called directly against the real test DB, as in ``tests/test_funnels.py``
and ``tests/test_export_handler.py``.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from sqlalchemy import text
from telegram import Message

ADMIN_ID = 111


def _callback(data: str, chat_id: int):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = ADMIN_ID
    update.message = None
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = chat_id
    query.message.reply_text = AsyncMock()
    query.message.reply_document = AsyncMock()
    update.callback_query = query
    return update, MagicMock(), query


def _last_text(query) -> str:
    return query.edit_message_text.call_args[0][0]


def _buttons(query) -> dict[str, str]:
    markup = query.edit_message_text.call_args.kwargs["reply_markup"]
    return {b.callback_data: b.text for row in markup.inline_keyboard for b in row}


def _png(size: int = 10) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), (255, 255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


async def _project(session_factory, owner_id: uuid.UUID, name: str) -> uuid.UUID:
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session, name=name, admin_chat_id=ADMIN_ID, owner_user_id=owner_id
        )
        await session.commit()
        return project.id


async def _add_taps(session_factory, pid: uuid.UUID, path: str, device: str, taps) -> None:
    from app.schemas.event import TapPoint
    from app.services.taps import insert_taps

    async with session_factory() as session:
        await insert_taps(
            session,
            project_id=pid,
            path=path,
            device=device,
            vw=390,
            taps=[TapPoint(x=x, y=y, el=el) for x, y, el in taps],
            scroll=0.5,
        )
        await session.commit()


async def _seed_state(session_factory, chat_id: int, payload: dict) -> None:
    from app.bot.states import BotStateService

    async with session_factory() as session:
        await BotStateService(session).save(chat_id, flow="heatmap", step="period", payload=payload)
        await session.commit()


async def _cleanup(session_factory, chat_id: int, *project_ids: uuid.UUID) -> None:
    async with session_factory() as session:
        for pid in project_ids:
            await session.execute(text("DELETE FROM projects WHERE id = :p"), {"p": str(pid)})
        await session.execute(
            text("DELETE FROM bot_conversation_state WHERE chat_id = :c"), {"c": chat_id}
        )
        await session.commit()


@pytest.fixture()
def renderer(monkeypatch):
    """Configure a renderer and capture fetch_screenshot calls."""
    import app.bot.handlers.heatmap as hm

    calls: list[tuple] = []
    result: dict = {"shot": None}

    async def fake_fetch(url, device, median_vw=None, **_kw):
        calls.append((url, device, median_vw))
        return result["shot"]

    monkeypatch.setattr(hm, "get_settings", lambda: SimpleNamespace(screenshot_url="http://r"))
    monkeypatch.setattr(hm, "fetch_screenshot", fake_fetch)
    return SimpleNamespace(calls=calls, result=result)


# ── Tests ───────────────────────────────────────────────────────────────────


async def test_heatmap_command_lists_only_own_projects(session_factory, singleton_user):
    from app.bot.handlers.heatmap import heatmap_command
    from app.models.user import User

    own = await _project(session_factory, singleton_user.id, "hm-own.example.com")
    async with session_factory() as session:
        other = User(telegram_user_id=999_771)
        session.add(other)
        await session.commit()
        other_id = other.id
    foreign = await _project(session_factory, other_id, "hm-foreign.example.com")

    update = MagicMock()
    update.effective_chat.id = ADMIN_ID
    update.effective_user.id = ADMIN_ID
    update.message.reply_text = AsyncMock()
    await heatmap_command(update, MagicMock())

    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"hm:proj:{own}" in data
    assert f"hm:proj:{foreign}" not in data

    await _cleanup(session_factory, ADMIN_ID, own, foreign)
    async with session_factory() as session:
        await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(other_id)})
        await session.commit()


async def test_page_picker_uses_top_pageview_urls_and_state_index(session_factory, singleton_user):
    from app.bot.handlers.heatmap import heatmap_callback
    from app.bot.states import BotStateService
    from app.services.events import insert_event

    chat_id = 4101
    pid = await _project(session_factory, singleton_user.id, "hm-pages.example.com")
    long_path = "/albums/" + "x" * 120 + "?sort=new"
    ts = datetime.now(UTC) - timedelta(hours=2)
    async with session_factory() as session:
        for url, n in (("/", 3), (long_path, 5), ("/about", 1)):
            for i in range(n):
                await insert_event(
                    session,
                    project_id=pid,
                    event_name="pageview",
                    session_id=f"s{i}",
                    properties={"url": url},
                    url=url,
                    timestamp=ts,
                )
        await session.commit()
    await _add_taps(session_factory, pid, long_path, "mobile", [(0.5, 100, "a")])

    update, ctx, query = _callback(f"hm:proj:{pid}", chat_id)
    await heatmap_callback(update, ctx)

    buttons = _buttons(query)
    assert [k for k in buttons if k.startswith("hm:page:")] == [
        "hm:page:0",
        "hm:page:1",
        "hm:page:2",
    ]
    assert all(len(k.encode()) <= 64 for k in buttons)
    async with session_factory() as session:
        state = await BotStateService(session).get(chat_id)
        assert state is not None and state.flow == "heatmap"
        assert state.payload["pages"] == [long_path, "/", "/about"]

    # Index 0 resolves to the long path through the state, not callback_data.
    update, ctx, query = _callback("hm:page:0", chat_id)
    await heatmap_callback(update, ctx)
    buttons = _buttons(query)
    assert "✓" in buttons["hm:dev:mobile"] and "(1)" in buttons["hm:dev:mobile"]
    assert "hm:dev:all" in buttons
    assert "/albums/" in _last_text(query)

    await _cleanup(session_factory, chat_id, pid)


async def test_send_heatmap_sends_text_then_document(session_factory, singleton_user, renderer):
    from app.bot.handlers.heatmap import heatmap_callback
    from app.services.heatmap import Screenshot

    chat_id = 4102
    pid = await _project(session_factory, singleton_user.id, "hm-shot.example.com")
    await _add_taps(
        session_factory,
        pid,
        "/",
        "mobile",
        [(0.5, 2, 'button "Go"'), (0.5, 3, 'button "Go"'), (0.1, 1, None)],
    )
    await _seed_state(
        session_factory,
        chat_id,
        {"project_id": str(pid), "pages": ["/"], "page": 0, "device": "mobile"},
    )
    renderer.result["shot"] = Screenshot(
        png=_png(),
        final_url="https://hm-shot.example.com/",
        login_wall=False,
        doc_height=10,
        width=390,
        dpr=2,
    )

    update, ctx, query = _callback("hm:prd:7d", chat_id)
    order = MagicMock()
    order.attach_mock(query.edit_message_text, "edit")
    order.attach_mock(query.message.reply_document, "document")
    await heatmap_callback(update, ctx)

    names = [c[0] for c in order.mock_calls]
    assert names.index("edit") < names.index("document")
    first_text = order.mock_calls[names.index("edit")].args[0]
    assert "3 taps" in first_text
    assert "button &quot;Go&quot; — 2 (67%)" in first_text
    assert "Scroll depth: median 50%" in first_text

    doc = query.message.reply_document.call_args.kwargs
    assert doc["filename"] == "heatmap-home-mobile-7d.png"
    assert doc["document"].startswith(b"\x89PNG")
    assert renderer.calls == [("https://hm-shot.example.com/", "mobile", 390)]
    # The pending note is gone from the final text.
    assert "📸" not in _last_text(query)

    await _cleanup(session_factory, chat_id, pid)


async def test_send_heatmap_text_only_when_renderer_unconfigured(
    session_factory, singleton_user, monkeypatch
):
    import app.bot.handlers.heatmap as hm

    chat_id = 4103
    fetch = AsyncMock()
    monkeypatch.setattr(hm, "get_settings", lambda: SimpleNamespace(screenshot_url=""))
    monkeypatch.setattr(hm, "fetch_screenshot", fetch)
    pid = await _project(session_factory, singleton_user.id, "hm-norender.example.com")
    await _add_taps(session_factory, pid, "/", "mobile", [(0.5, 10, "a")])
    await _seed_state(
        session_factory,
        chat_id,
        {"project_id": str(pid), "pages": ["/"], "page": 0, "device": "mobile"},
    )

    update, ctx, query = _callback("hm:prd:7d", chat_id)
    await hm.heatmap_callback(update, ctx)

    fetch.assert_not_called()
    query.message.reply_document.assert_not_called()
    assert hm.RENDERER_HINT in _last_text(query)
    assert "hm:prd:30d" in _buttons(query)

    await _cleanup(session_factory, chat_id, pid)


async def test_send_heatmap_text_only_on_login_wall(session_factory, singleton_user, renderer):
    from app.bot.handlers.heatmap import LOGIN_WALL_NOTE, heatmap_callback
    from app.services.heatmap import Screenshot

    chat_id = 4104
    pid = await _project(session_factory, singleton_user.id, "hm-login.example.com")
    await _add_taps(session_factory, pid, "/app", "desktop", [(0.5, 10, "a")])
    await _seed_state(
        session_factory,
        chat_id,
        {"project_id": str(pid), "pages": ["/app"], "page": 0, "device": "desktop"},
    )
    renderer.result["shot"] = Screenshot(
        png=_png(),
        final_url="https://hm-login.example.com/login",
        login_wall=True,
        doc_height=10,
        width=1280,
        dpr=1,
    )

    update, ctx, query = _callback("hm:prd:30d", chat_id)
    await heatmap_callback(update, ctx)

    query.message.reply_document.assert_not_called()
    assert LOGIN_WALL_NOTE in _last_text(query)
    assert "1 tap" in _last_text(query)

    await _cleanup(session_factory, chat_id, pid)


async def test_send_heatmap_all_devices_is_text_only(session_factory, singleton_user, renderer):
    from app.bot.handlers.heatmap import ALL_DEVICES_NOTE, heatmap_callback

    chat_id = 4105
    pid = await _project(session_factory, singleton_user.id, "hm-all.example.com")
    await _add_taps(session_factory, pid, "/", "mobile", [(0.5, 10, "a")])
    await _add_taps(session_factory, pid, "/", "desktop", [(0.5, 10, "a")])
    await _seed_state(
        session_factory,
        chat_id,
        {"project_id": str(pid), "pages": ["/"], "page": 0, "device": "all"},
    )

    update, ctx, query = _callback("hm:prd:7d", chat_id)
    await heatmap_callback(update, ctx)

    assert renderer.calls == []
    query.message.reply_document.assert_not_called()
    assert "2 taps" in _last_text(query)
    assert ALL_DEVICES_NOTE in _last_text(query)

    await _cleanup(session_factory, chat_id, pid)


async def test_send_heatmap_without_taps_explains_sdk_option(
    session_factory, singleton_user, renderer
):
    from app.bot.handlers.heatmap import heatmap_callback

    chat_id = 4106
    pid = await _project(session_factory, singleton_user.id, "hm-empty.example.com")
    await _seed_state(
        session_factory,
        chat_id,
        {"project_id": str(pid), "pages": ["/"], "page": 0, "device": "mobile"},
    )

    update, ctx, query = _callback("hm:prd:7d", chat_id)
    await heatmap_callback(update, ctx)

    assert renderer.calls == []
    query.message.reply_document.assert_not_called()
    assert "heatmaps: true" in _last_text(query)

    await _cleanup(session_factory, chat_id, pid)


def test_ranking_text_escapes_html_in_labels():
    from app.bot.handlers.heatmap import _ranking_text
    from app.models.project import Project

    project = Project(name="<i>site</i>")
    out = _ranking_text(
        project,
        "/<script>",
        "mobile",
        "7d",
        [{"label": "<b>x</b>", "count": 3}, {"label": None, "count": 1}],
        4,
        0.62,
    )
    assert "<b>x</b>" not in out
    assert "&lt;b&gt;x&lt;/b&gt; — 3 (75%)" in out
    assert "&lt;i&gt;site&lt;/i&gt;" in out
    assert "/&lt;script&gt;" in out
    assert "Scroll depth: median 62%" in out


async def test_foreign_project_is_rejected(session_factory, singleton_user):
    from app.bot.handlers.heatmap import heatmap_callback
    from app.bot.states import BotStateService
    from app.models.user import User

    chat_id = 4107
    async with session_factory() as session:
        victim = User(telegram_user_id=999_772)
        session.add(victim)
        await session.commit()
        victim_id = victim.id
    pid = await _project(session_factory, victim_id, "hm-victim.example.com")
    await _add_taps(session_factory, pid, "/", "mobile", [(0.5, 10, "secret label")])

    # Picking the project directly.
    update, ctx, query = _callback(f"hm:proj:{pid}", chat_id)
    await heatmap_callback(update, ctx)
    assert "not found" in _last_text(query).lower()
    async with session_factory() as session:
        assert await BotStateService(session).get(chat_id) is None

    # Crafted state that points at the foreign project.
    await _seed_state(
        session_factory,
        chat_id,
        {"project_id": str(pid), "pages": ["/"], "page": 0, "device": "mobile"},
    )
    update, ctx, query = _callback("hm:prd:7d", chat_id)
    await heatmap_callback(update, ctx)
    assert "not found" in _last_text(query).lower()
    assert "secret label" not in _last_text(query)
    query.message.reply_document.assert_not_called()
    async with session_factory() as session:
        assert await BotStateService(session).get(chat_id) is None

    await _cleanup(session_factory, chat_id, pid)
    async with session_factory() as session:
        await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(victim_id)})
        await session.commit()


def _shot(final_url: str = "https://hm.example.com/"):
    from app.services.heatmap import Screenshot

    return Screenshot(
        png=_png(), final_url=final_url, login_wall=False, doc_height=10, width=390, dpr=2
    )


async def test_pending_screenshot_message_has_no_buttons(
    session_factory, singleton_user, monkeypatch
):
    """While the screenshot is taken the period buttons are gone (no double render)."""
    import app.bot.handlers.heatmap as hm

    chat_id = 4108
    pid = await _project(session_factory, singleton_user.id, "hm-pending.example.com")
    await _add_taps(session_factory, pid, "/", "mobile", [(0.5, 10, "a")])
    await _seed_state(
        session_factory,
        chat_id,
        {"project_id": str(pid), "pages": ["/"], "page": 0, "device": "mobile"},
    )
    update, ctx, query = _callback("hm:prd:7d", chat_id)
    seen: dict = {}

    async def fake_fetch(url, device, median_vw=None, **_kw):
        # The message as the user sees it while the render is pending.
        seen["text"] = query.edit_message_text.call_args[0][0]
        seen["markup"] = query.edit_message_text.call_args.kwargs.get("reply_markup")
        return _shot("https://hm-pending.example.com/")

    monkeypatch.setattr(hm, "get_settings", lambda: SimpleNamespace(screenshot_url="http://r"))
    monkeypatch.setattr(hm, "fetch_screenshot", fake_fetch)
    await hm.heatmap_callback(update, ctx)

    assert hm.SHOT_PENDING_NOTE in seen["text"]
    assert seen["markup"] is None
    # The final edit restores the period toggles and Back.
    assert "hm:prd:30d" in _buttons(query) and "hm:back:dev" in _buttons(query)
    assert hm.SHOT_PENDING_NOTE not in _last_text(query)
    query.message.reply_document.assert_called_once()

    await _cleanup(session_factory, chat_id, pid)


async def test_screenshot_failure_and_render_error_keep_text(
    session_factory, singleton_user, renderer, monkeypatch
):
    """fetch None, render raising or send raising: the ranking still arrives."""
    import app.bot.handlers.heatmap as hm

    chat_id = 4109
    pid = await _project(session_factory, singleton_user.id, "hm-fail.example.com")
    await _add_taps(session_factory, pid, "/", "mobile", [(0.5, 10, "lbl")])
    await _seed_state(
        session_factory,
        chat_id,
        {"project_id": str(pid), "pages": ["/"], "page": 0, "device": "mobile"},
    )

    # The renderer answers nothing usable.
    renderer.result["shot"] = None
    update, ctx, query = _callback("hm:prd:7d", chat_id)
    await hm.heatmap_callback(update, ctx)
    assert hm.SHOT_FAILED_NOTE in _last_text(query) and "1 tap" in _last_text(query)
    query.message.reply_document.assert_not_called()

    # Drawing the heat layer raises.
    renderer.result["shot"] = _shot()

    async def boom(shot, points):
        raise RuntimeError("draw failed")

    monkeypatch.setattr(hm, "render_heatmap", boom)
    update, ctx, query = _callback("hm:prd:7d", chat_id)
    await hm.heatmap_callback(update, ctx)
    assert hm.SHOT_FAILED_NOTE in _last_text(query) and "1 tap" in _last_text(query)
    query.message.reply_document.assert_not_called()

    # Sending the document raises.
    monkeypatch.setattr(hm, "render_heatmap", AsyncMock(return_value=_png()))
    update, ctx, query = _callback("hm:prd:7d", chat_id)
    query.message.reply_document = AsyncMock(side_effect=RuntimeError("send failed"))
    await hm.heatmap_callback(update, ctx)
    assert hm.SHOT_FAILED_NOTE in _last_text(query) and "1 tap" in _last_text(query)

    await _cleanup(session_factory, chat_id, pid)


async def test_unknown_callback_answers_with_note(session_factory, singleton_user):
    from app.bot.handlers.heatmap import UNKNOWN_ACTION_NOTE, heatmap_callback

    for data in ("hm:back:zzz", "hm:zzz:1"):
        update, ctx, query = _callback(data, 4110)
        await heatmap_callback(update, ctx)
        query.answer.assert_awaited_once_with(UNKNOWN_ACTION_NOTE)
        query.edit_message_text.assert_not_called()


def test_header_truncates_long_project_name():
    from app.bot.handlers.heatmap import _ranking_text
    from app.models.project import Project

    out = _ranking_text(Project(name="n" * 3000), "/", "mobile", "7d", [], 0, None)
    assert "n" * 59 + "…" in out
    assert "n" * 61 not in out

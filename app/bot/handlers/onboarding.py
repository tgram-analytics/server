"""Onboarding flow for first-time users.

Triggered from /start (when the caller has zero projects) and from the
post-creation reply of /add (test-event button + SDK snippet buttons).

Callback prefixes routed here:

* ``onb:create``       — "create your first project" tap from /start
* ``onb:how``          — brief explainer
* ``onb:test:<id>``    — fire a test event for project <id> via the bot
* ``onb:sdk:<lang>:<id>`` — show install + init + track snippet for
  the chosen SDK (lang ∈ {js, py, dart})
* ``onb:back:<id>``    — return to the post-create main keyboard
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from app.bot.auth import requires_user
from app.core.config import get_settings
from app.models.user import User
from app.services.events import insert_event
from app.services.projects import get_project

# ── Public helpers (called from /start and /add) ──────────────────────────────


async def send_first_run_welcome(message: Message, user_first_name: str) -> None:
    """Reply to /start when the caller has no projects yet."""
    name = user_first_name or "there"
    await message.reply_text(
        f"👋 <b>Welcome, {name}!</b>\n\n"
        "tgram-analytics tracks events from any app and pings you in "
        "Telegram when something matters. No dashboards, no third "
        "parties — just chat.\n\n"
        "<b>Step 1 of 3 — create your first project.</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🚀 Create my first project", callback_data="onb:create")],
                [InlineKeyboardButton("📖 How it works", callback_data="onb:how")],
            ]
        ),
    )


def post_create_keyboard(project_id: uuid.UUID) -> InlineKeyboardMarkup:
    """Inline keyboard appended to /add's success reply."""
    pid = str(project_id)
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Send test event from bot", callback_data=f"onb:test:{pid}")],
            [
                InlineKeyboardButton("📦 JS", callback_data=f"onb:sdk:js:{pid}"),
                InlineKeyboardButton("🐍 Python", callback_data=f"onb:sdk:py:{pid}"),
                InlineKeyboardButton("📱 Flutter", callback_data=f"onb:sdk:dart:{pid}"),
            ],
        ]
    )


# ── SDK snippets (rendered on demand) ─────────────────────────────────────────


def _js_snippet(server_url: str) -> str:
    return (
        "<b>📦 JavaScript / Web</b>\n\n"
        "<pre>npm install tgram-analytics</pre>\n\n"
        "<pre>"
        "import TGA from &#39;tgram-analytics&#39;;\n\n"
        f"TGA.init(&#39;&lt;YOUR_API_KEY&gt;&#39;, {{ serverUrl: &#39;{server_url}&#39; }});\n"
        "TGA.track(&#39;signup&#39;, {{ plan: &#39;free&#39; }});"
        "</pre>\n\n"
        "Or drop in a script tag:\n"
        f"<pre>&lt;script src=&quot;{server_url}/sdk/tga.min.js&quot;&gt;&lt;/script&gt;</pre>\n\n"
        "💡 Replace <code>&lt;YOUR_API_KEY&gt;</code> with the <code>proj_…</code> "
        "key from the project-creation message above.\n\n"
        "📚 Full docs: github.com/tgram-analytics/tgram-analytics-js"
    )


def _python_snippet(server_url: str) -> str:
    return (
        "<b>🐍 Python (sync + async)</b>\n\n"
        "<pre>pip install tgram-analytics</pre>\n\n"
        "<pre>"
        "from tgram_analytics import TGA\n\n"
        f"tga = TGA(api_key=&#39;&lt;YOUR_API_KEY&gt;&#39;, server_url=&#39;{server_url}&#39;)\n"
        "tga.track(&#39;purchase&#39;, session_id=&#39;user-123&#39;, "
        "properties={{&#39;amount&#39;: 49}})"
        "</pre>\n\n"
        "💡 Replace <code>&lt;YOUR_API_KEY&gt;</code> with the <code>proj_…</code> "
        "key from above.\n\n"
        "📚 Full docs: github.com/tgram-analytics/tgram-analytics-py"
    )


def _dart_snippet(server_url: str) -> str:
    return (
        "<b>📱 Flutter / Dart</b>\n\n"
        "<pre>flutter pub add tgram_analytics</pre>\n\n"
        "<pre>"
        "import &#39;package:tgram_analytics/tgram_analytics.dart&#39;;\n\n"
        f"final tga = TGA(apiKey: &#39;&lt;YOUR_API_KEY&gt;&#39;, serverUrl: &#39;{server_url}&#39;);\n"
        "await tga.track(&#39;app_open&#39;, sessionId: &#39;user-123&#39;);"
        "</pre>\n\n"
        "💡 Replace <code>&lt;YOUR_API_KEY&gt;</code> with the <code>proj_…</code> "
        "key from above.\n\n"
        "📚 Full docs: github.com/tgram-analytics/tgram-analytics-flutter"
    )


_SDK_RENDERERS = {
    "js": _js_snippet,
    "py": _python_snippet,
    "dart": _dart_snippet,
}


# ── Callback dispatcher ───────────────────────────────────────────────────────


@requires_user
async def onboarding_callback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()

    data = query.data or ""
    parts = data.split(":")

    # onb:create — guide user to type /add
    if parts == ["onb", "create"]:
        await query.edit_message_text(
            "✏️ <b>Step 1 of 3</b>\n\n"
            "Send me <code>/add yourproject.com</code> (any name works) "
            "and I'll generate your API key.\n\n"
            "Example: <code>/add myapp.com</code>",
            parse_mode="HTML",
        )
        return

    # onb:how — brief explainer
    if parts == ["onb", "how"]:
        await query.edit_message_text(
            "<b>How it works</b>\n\n"
            "1️⃣ You create a project and get an API key.\n"
            "2️⃣ You drop the SDK (or a curl) into your app and emit events.\n"
            "3️⃣ I tell you when something interesting happens — and "
            "answer questions like /report signup or /events on demand.\n\n"
            "Send /add to create your first project.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🚀 Create my first project", callback_data="onb:create")]]
            ),
        )
        return

    # onb:test:<project_id> — fire a test event
    if len(parts) == 3 and parts[0] == "onb" and parts[1] == "test":
        project_id = _parse_uuid(parts[2])
        if project_id is None:
            await query.edit_message_text("❌ Invalid project id.")
            return
        await _send_test_event(query, session, user, project_id)
        return

    # onb:sdk:<lang>:<project_id>
    if len(parts) == 4 and parts[0] == "onb" and parts[1] == "sdk":
        lang = parts[2]
        project_id = _parse_uuid(parts[3])
        if project_id is None or lang not in _SDK_RENDERERS:
            await query.edit_message_text("❌ Invalid SDK request.")
            return
        await _show_sdk(query, session, user, project_id, lang)
        return

    # Unknown — silently ignore (defensive; pattern is restrictive)


# ── Internal handlers ─────────────────────────────────────────────────────────


async def _send_test_event(query, session: AsyncSession, user: User, project_id: uuid.UUID) -> None:
    project = await get_project(session, project_id, user.id)
    if project is None:
        await query.edit_message_text("❌ Project not found or no longer yours.")
        return

    await insert_event(
        session,
        project_id=project.id,
        event_name="test",
        session_id="bot-onboarding",
        properties={"source": "telegram-bot", "name": project.name},
    )

    await query.edit_message_text(
        f"✅ <b>Test event sent for {project.name}!</b>\n\n"
        f"I just emitted an event named <code>test</code>.\n\n"
        f"Run <code>/events</code> to see it — you'll find <code>test</code> "
        f"in the list with a count of 1.\n\n"
        f"Now hook your real app up using one of the SDKs:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📦 JS", callback_data=f"onb:sdk:js:{project_id}"),
                    InlineKeyboardButton("🐍 Python", callback_data=f"onb:sdk:py:{project_id}"),
                    InlineKeyboardButton("📱 Flutter", callback_data=f"onb:sdk:dart:{project_id}"),
                ],
            ]
        ),
    )


async def _show_sdk(
    query, session: AsyncSession, user: User, project_id: uuid.UUID, lang: str
) -> None:
    project = await get_project(session, project_id, user.id)
    if project is None:
        await query.edit_message_text("❌ Project not found or no longer yours.")
        return

    settings = get_settings()
    server_url = settings.webhook_base_url.rstrip("/") or "https://your-server.com"
    body = _SDK_RENDERERS[lang](server_url)

    other_langs = [
        (code, label)
        for code, label in (("js", "📦 JS"), ("py", "🐍 Python"), ("dart", "📱 Flutter"))
        if code != lang
    ]

    keyboard = [
        [
            InlineKeyboardButton(label, callback_data=f"onb:sdk:{code}:{project_id}")
            for code, label in other_langs
        ],
        [InlineKeyboardButton("🚀 Send a test event", callback_data=f"onb:test:{project_id}")],
    ]

    await query.edit_message_text(
        body,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


def _parse_uuid(s: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(s)
    except (ValueError, TypeError):
        return None

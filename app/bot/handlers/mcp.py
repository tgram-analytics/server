"""/mcp — how to connect an AI agent (MCP client) to this instance.

Prints copy-paste setup instructions over Telegram. Edition-aware: a
default (self-hosted) install authenticates MCP with a static bearer
token minted via /mcp_token, so that flow is shown. When a plugin
registers a custom verifier (the cloud overlay's OAuth), static tokens
don't apply, so the browser sign-in flow is shown instead.

The signal is ``app.extensions.get_mcp_token_verifier()``: ``None`` means
the default StaticTokenVerifier is in use (self-host); anything else is
a plugin-supplied verifier (cloud).
"""

from __future__ import annotations

from html import escape

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.auth import requires_user
from app.models.user import User
from app.services.projects import list_projects


@requires_user
async def mcp_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    assert update.message is not None

    from app.core.config import get_settings

    settings = get_settings()
    if not settings.mcp_enabled:
        await update.message.reply_html(
            "🔌 <b>MCP is disabled on this instance.</b>\n\n"
            "Set <code>MCP_ENABLED=true</code> and redeploy to turn it on."
        )
        return

    server_url = f"{settings.mcp_effective_public_url}/mcp"
    esc = escape(server_url)
    project_count = len(await list_projects(session, user.id))

    from app.extensions import get_mcp_token_verifier

    if get_mcp_token_verifier() is not None:
        # A plugin (cloud overlay) supplies its own verifier → OAuth flow.
        await update.message.reply_html(
            "🔌 <b>Connect an AI agent (MCP)</b>\n\n"
            "Add this server to Claude, Cursor, or any MCP client:\n"
            f"<code>{esc}</code>\n\n"
            "Your client opens a browser to sign in with Telegram the first "
            "time. Once connected, the agent can read your analytics across "
            f"all {project_count} of your projects. It can also ask to create "
            "a project or rotate a project key — both need your approval "
            "first.\n\n"
            'Then ask: <i>"list my tgram-analytics projects"</i>.'
        )
        return

    # Default self-hosted install: static bearer token via /mcp_token.
    await update.message.reply_html(
        "🔌 <b>Connect an AI agent (MCP)</b>\n\n"
        "Give Claude / Cursor / other MCP clients access to your "
        f"analytics — one connection covers all {project_count} of your "
        "projects. Reading is unrestricted; creating a project or rotating a "
        "project key needs your approval first.\n\n"
        "<b>1. Create an access token</b>\n"
        "<code>/mcp_token new my-laptop</code>  (shown once — copy it)\n\n"
        "<b>2. Add the server to your client</b>\n"
        "Claude Code:\n"
        f"<code>claude mcp add --transport http tgram {esc} "
        '--header "Authorization: Bearer YOUR_TOKEN"</code>\n\n'
        "Claude Desktop: Settings → Connectors → <b>Add custom connector</b>, "
        f"URL <code>{esc}</code> — a browser page opens; paste a token from "
        "/mcp_token there.\n"
        "Cursor: add an HTTP MCP server with URL "
        f"<code>{esc}</code> and header "
        "<code>Authorization: Bearer YOUR_TOKEN</code>.\n\n"
        '<b>3. Try it</b> — ask: <i>"list my tgram-analytics projects"</i>.\n\n'
        "Manage or revoke tokens anytime with /mcp_token."
    )

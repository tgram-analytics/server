"""/mcp_token — manage static MCP access tokens.

- ``/mcp_token`` — list tokens with revoke buttons
- ``/mcp_token new [label]`` — create a token; raw value shown ONCE

The ``@requires_user`` decorator opens the session and commits on a
clean return, so these handlers never commit explicitly.
"""

from __future__ import annotations

import uuid
from html import escape

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.bot.auth import requires_user
from app.models.user import User
from app.services import mcp_tokens as svc

_CONNECT_HINT = (
    "\n\nConnect Claude Code:\n"
    "<code>claude mcp add --transport http tgram {base}/mcp "
    '--header "Authorization: Bearer {token}"</code>'
)


@requires_user
async def mcp_token_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    assert update.message is not None
    args = ctx.args or []
    if args and args[0] == "new":
        label = " ".join(args[1:]) or "default"
        raw, _ = await svc.create_token(session, user_id=user.id, label=label)
        from app.core.config import get_settings

        base = get_settings().mcp_effective_public_url
        from app.bot.key_redaction import schedule_redaction, with_hide_button

        text = (
            "🔑 <b>MCP token created</b> — store it now, it's masked in this "
            "message in a few minutes:\n\n"
            f"<code>{raw}</code>" + _CONNECT_HINT.format(base=escape(base), token=raw)
        )
        sent = await update.message.reply_html(text, reply_markup=with_hide_button(None))
        schedule_redaction(ctx.bot, sent.chat_id, sent.message_id, text)
        return

    rows = await svc.list_tokens(session, user_id=user.id)
    active = [r for r in rows if r.revoked_at is None]
    if not active:
        await update.message.reply_html(
            "No MCP tokens yet.\n\n"
            "Create one with <code>/mcp_token new [label]</code> to connect "
            "Claude or another MCP client."
        )
        return

    lines = ["🔑 <b>MCP tokens</b>\n"]
    buttons = []
    for r in active:
        used = r.last_used_at.strftime("%Y-%m-%d %H:%M") if r.last_used_at else "never"
        lines.append(
            f"• <b>{escape(r.label)}</b> — created "
            f"{r.created_at.strftime('%Y-%m-%d')}, last used {used}"
        )
        buttons.append(
            [InlineKeyboardButton(f"🗑 Revoke “{r.label}”", callback_data=f"mcptok:revoke:{r.id}")]
        )
    await update.message.reply_html("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


@requires_user
async def mcp_token_callback(
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
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[1] != "revoke":
        return
    token_id = parts[2]

    revoked = await svc.revoke_token(session, token_id=uuid.UUID(token_id), user_id=user.id)
    await query.edit_message_text(
        "✅ Token revoked." if revoked else "Token not found (already revoked?)."
    )

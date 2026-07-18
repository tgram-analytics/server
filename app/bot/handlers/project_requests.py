"""Inline-keyboard callback for AI-agent project-create requests.

Flow: an MCP client calls the ``create_project`` tool, which files a
``pending`` :class:`~app.models.project_create_request.ProjectCreateRequest`
row and notifies the owner in Telegram with Approve/Reject buttons
(callback_data ``pcr:yes:<uuid>`` / ``pcr:no:<uuid>`` — see
:mod:`app.mcp.notify`). This handler resolves that decision:

* **Approve** — creates the real project via
  :func:`app.services.projects.create_project`, marks the request
  ``approved``, and edits the prompt into the usual post-create message
  (API key in a spoiler block). The MCP client polls the request status
  and sees it resolve.
* **Reject** — marks the request ``rejected``; no project is created.
* **Expired / already resolved / not found** — the message is edited to
  explain why nothing happened.

On a creation error (``ExtensionError``) the request stays ``pending``
so the owner can retry or reject.
"""

from __future__ import annotations

import html
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.auth import requires_user
from app.core.config import get_settings
from app.mcp.notify import approval_keyboard
from app.models.user import User
from app.services.project_requests import claim_request, get_request, is_expired
from app.services.projects import create_project


@requires_user
async def project_request_callback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()

    # Defense-in-depth: the approval prompt lives in the owner's private
    # chat, but callback_query updates carry their own sender id — never
    # let a non-owner resolve a request. Silently ignore (no message
    # edit): a forged callback should get no oracle.
    sender = update.effective_user
    if sender is None or sender.id != user.telegram_user_id:
        return

    # callback_data is "pcr:yes:<uuid>" or "pcr:no:<uuid>"
    data: str = query.data or ""
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "pcr" or parts[1] not in ("yes", "no"):
        await query.edit_message_text("❌ Invalid request.")
        return
    try:
        rid = uuid.UUID(parts[2])
    except ValueError:
        await query.edit_message_text("❌ Invalid request.")
        return
    approve = parts[1] == "yes"

    row = await get_request(session, rid, user.id)
    if row is None:
        await query.edit_message_text("❌ Request not found.")
        return

    if row.status != "pending":
        await query.edit_message_text(f"ℹ️ This request was already {row.status}.")
        return

    if is_expired(row):
        if not await claim_request(session, row, status="expired"):
            await session.rollback()
            await query.edit_message_text("ℹ️ This request was already handled.")
            return
        await session.commit()
        await query.edit_message_text(
            "⌛ This request has expired. Ask the agent to file a new one."
        )
        return

    name = row.name
    if not approve:
        if not await claim_request(session, row, status="rejected"):
            await session.rollback()
            await query.edit_message_text("ℹ️ This request was already handled.")
            return
        await session.commit()
        await query.edit_message_text(
            f"❌ Rejected — project <b>{html.escape(name)}</b> will not be created.",
            parse_mode="HTML",
        )
        return

    from app.extensions import ExtensionError

    try:
        project, api_key = await create_project(
            session,
            name=name,
            admin_chat_id=user.telegram_user_id,
            owner_user_id=user.id,
            domain_allowlist=list(row.domain_allowlist or []),
        )
    except ExtensionError as exc:
        # Plugin-raised, user-facing — render the message and stop. The
        # request stays pending, and the Approve/Reject keyboard is
        # re-attached so the owner can retry or reject.
        await query.edit_message_text(str(exc), reply_markup=approval_keyboard(str(row.id)))
        return

    # Claim AFTER creating the project: both run in the same transaction,
    # so losing the claim race lets us roll back the just-created project.
    if not await claim_request(session, row, status="approved", project_id=project.id):
        await session.rollback()
        await query.edit_message_text("ℹ️ This request was already handled.")
        return
    await session.commit()

    settings = get_settings()
    base = settings.webhook_base_url.rstrip("/") or "https://your-server.com"
    env_block = f"TGA_URL={base}\nTGA_API_KEY={api_key}"

    await query.edit_message_text(
        f"✅ Project <b>{html.escape(name)}</b> created (requested by your AI agent)!\n\n"
        f"⚠️ Save this key — it won't be shown again.\n\n"
        f"<b>Env:</b>\n<tg-spoiler><pre>{env_block}</pre></tg-spoiler>\n\n"
        f"Your agent can now see this project over MCP. If it needs the API key "
        f"itself, it will call rotate_api_key (which replaces the key above).",
        parse_mode="HTML",
    )

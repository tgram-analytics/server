"""Conversation state persistence backed by the bot_conversation_state table.

State is upserted (not replaced) so partial payloads from earlier steps
survive multiple round-trips.  Cleared when a flow completes or /cancel is sent.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bot_conversation_state import BotConversationState


class BotStateService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, chat_id: int) -> BotConversationState | None:
        return await self._session.get(BotConversationState, chat_id)

    async def save(
        self,
        chat_id: int,
        *,
        flow: str,
        step: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        stmt = (
            pg_insert(BotConversationState)
            .values(
                chat_id=chat_id,
                flow=flow,
                step=step,
                payload=payload or {},
            )
            .on_conflict_do_update(
                index_elements=["chat_id"],
                set_={
                    "flow": flow,
                    "step": step,
                    "payload": payload or {},
                    "updated_at": text("now()"),
                },
            )
        )
        await self._session.execute(stmt)

    async def clear(self, chat_id: int) -> None:
        state = await self._session.get(BotConversationState, chat_id)
        if state is not None:
            await self._session.delete(state)

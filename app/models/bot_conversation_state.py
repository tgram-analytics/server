"""SQLAlchemy ORM model for bot_conversation_state table.

Stores multi-step bot conversation flows so state survives server restarts.
One row per Telegram chat ID — upserted on each state transition.
"""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class BotConversationState(Base):
    __tablename__ = "bot_conversation_state"

    chat_id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    # Current flow name, e.g. "add_project", "configure_alert". NULL when idle.
    flow: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Current step within the flow, e.g. "awaiting_name". NULL when idle.
    step: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Accumulated answers from previous steps in the current flow.
    payload: Mapped[Any] = mapped_column(
        JSONB,
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )

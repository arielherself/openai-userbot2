"""The small slice of Telegram this userbot uses, behind an interface.

The bridge sends, edits and deletes messages; keeping that behind one object is
what lets the whole conversation flow be tested without a Telegram account.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class Delivery(Protocol):
    async def send(
        self,
        chat_id: int,
        text: str,
        entities: Sequence | None = None,
        reply_to: int | None = None,
    ) -> int:
        """Send a message and return its id."""

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        """Replace a message's text (status messages are plain text)."""

    async def delete(self, chat_id: int, message_ids: Sequence[int]) -> None:
        """Delete messages, ignoring the ones that are already gone."""


class TelethonDelivery:
    """`Delivery` over a logged-in `TelegramClient` (user account)."""

    def __init__(self, client) -> None:
        self.client = client

    async def send(self, chat_id, text, entities=None, reply_to=None) -> int:
        # `formatting_entities` is always supplied, even when empty, so Telethon
        # never re-parses the text with the client's default Markdown mode.
        # `link_preview=False` keeps a link in a reply from growing a preview —
        # and with it an Instant View button.
        message = await self.client.send_message(
            chat_id,
            text,
            formatting_entities=list(entities) if entities else [],
            reply_to=reply_to,
            parse_mode=None,
            link_preview=False,
        )
        return message.id

    async def edit(self, chat_id, message_id, text) -> None:
        await self.client.edit_message(
            chat_id,
            message_id,
            text,
            formatting_entities=[],
            parse_mode=None,
            link_preview=False,
        )

    async def delete(self, chat_id, message_ids) -> None:
        await self.client.delete_messages(chat_id, list(message_ids))

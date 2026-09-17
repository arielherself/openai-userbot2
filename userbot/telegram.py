"""The slice of Telegram this userbot uses, behind an interface.

The bridge sends, edits and deletes messages; the music tools also listen for one
particular message from another bot, and forward what it returns. Keeping that
behind one object is what lets the whole conversation flow be tested without a
Telegram account.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from telethon import events
from telethon.extensions import markdown
from telethon.tl import types
from telethon.tl.custom import Message as TelegramMessage

# Where a message goes: an id, or a username like the music bot's.
Entity = int | str


@dataclass
class BotMessage:
    """One message from another account, as a tool sees it."""

    id: int
    chat_id: int
    text: str
    # The message as its sender wrote it: a bot's Markdown becomes entities on the
    # way in, so links are only visible once they are put back.
    markdown: str = ""
    # The message this one answers, when it answers one of ours.
    reply_to: int | None = None
    # Whether the message holds a link at all — in its text, or only in the URLs
    # hidden inside formatted links.
    has_link: bool = False
    has_buttons: bool = False
    has_music: bool = False


class Listener(Protocol):
    """The messages one chat sends — and the edits of them — from the moment it
    was opened.

    Watching starts at construction, before the command that provokes an answer is
    even sent, so a bot that answers immediately cannot be missed — and a bot that
    answers nothing at all is just as visible. A message that is later edited is
    handed over again, because that is how a bot adds the keyboard of picks to a
    list it already sent.
    """

    async def next(self, timeout: float) -> BotMessage | None:
        """The next message from that chat, or None if none arrived in time."""

    def close(self) -> None:
        """Stop watching."""


class Delivery(Protocol):
    async def send(
        self,
        chat_id: Entity,
        text: str,
        entities: Sequence | None = None,
        reply_to: int | None = None,
    ) -> int:
        """Send a message and return its id."""

    async def edit(self, chat_id: Entity, message_id: int, text: str) -> None:
        """Replace a message's text (status messages are plain text)."""

    async def delete(self, chat_id: Entity, message_ids: Sequence[int]) -> None:
        """Delete messages, ignoring the ones that are already gone."""

    def listen(self, chat_id: Entity) -> Listener:
        """Start watching one chat, before the message that answers us is sent."""

    async def forward(self, chat_id: Entity, message_id: int, to_chat_id: Entity) -> int:
        """Forward a message into another chat and return the new message's id."""


class _TelethonListener:
    """`Listener` over a `TelegramClient`'s event dispatch."""

    def __init__(self, client, chat_id: Entity) -> None:
        self.client = client
        self.queue: asyncio.Queue = asyncio.Queue()

        async def handler(event) -> None:
            message = bot_message(event.message)
            if message is not None:
                self.queue.put_nowait(message)

        self.handler = handler
        # A bot may build its answer in place — send the list, then edit it to add
        # the keyboard of picks — so an edit counts as a message too.
        for build in (events.NewMessage(chats=chat_id), events.MessageEdited(chats=chat_id)):
            client.add_event_handler(handler, build)

    async def next(self, timeout: float) -> BotMessage | None:
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self.client.remove_event_handler(self.handler)


def bot_message(message) -> BotMessage | None:
    """A message from someone else, or None when there is nothing to hand over."""
    if not isinstance(message, TelegramMessage) or isinstance(message, types.MessageEmpty):
        return None
    text = message.message or ""
    return BotMessage(
        id=message.id,
        chat_id=message.chat_id if message.chat_id is not None else 0,
        text=text,
        markdown=as_markdown(text, message.entities),
        reply_to=message.reply_to_msg_id,
        has_link=has_link(text, message.entities),
        # `message.buttons` needs a client and an input chat to build its objects;
        # what matters here is only whether the message carries a keyboard at all.
        has_buttons=isinstance(
            message.reply_markup, (types.ReplyInlineMarkup, types.ReplyKeyboardMarkup)
        ),
        has_music=message.audio is not None,
    )


def as_markdown(text: str, entities) -> str:
    """The text with its entities written back out, links included."""
    try:
        return markdown.unparse(text, entities)
    except Exception:  # entities that do not line up with the text: keep the text
        return text


def has_link(text: str, entities) -> bool:
    """Whether a message holds a link — bare, or behind formatted link text."""
    for entity in entities or []:
        if isinstance(entity, (types.MessageEntityUrl, types.MessageEntityTextUrl)):
            return True
    return bool(_BARE_URL.search(text))


_BARE_URL = re.compile(r"\bhttps?://\S+", re.IGNORECASE)


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

    def listen(self, chat_id) -> Listener:
        return _TelethonListener(self.client, chat_id)

    async def forward(self, chat_id, message_id, to_chat_id) -> int:
        forwarded = await self.client.forward_messages(to_chat_id, message_id, chat_id)
        return forwarded.id

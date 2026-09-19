"""The slice of Telegram this userbot uses, behind an interface.

The bridge sends, edits and deletes messages; the music tools also listen for one
particular message from another bot, and forward what it returns. Keeping that
behind one object is what lets the whole conversation flow be tested without a
Telegram account.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from telethon import events, utils
from telethon.extensions import markdown
from telethon.tl import types
from telethon.tl.custom import Message as TelegramMessage

from .content import Content, content_of
from .tools import MAX_DOWNLOAD_BYTES

# Where a message goes: an id, or a username like the music bot's.
Entity = int | str

# How much image to hand over. Pictures stay in the transcript — every later
# request carries them again — so they are kept small on purpose.
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_IMAGES_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 4
# How much file to hand over: the one rule every download into a sandbox shares
# (tools.MAX_DOWNLOAD_BYTES), which the harness's own per-call ceiling sits above.
MAX_FILE_BYTES = MAX_DOWNLOAD_BYTES
# Photos come in several sizes; wide enough to read, narrow enough to be small.
USEFUL_WIDTH = 1280


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


@dataclass
class DownloadedFile:
    """A file Telegram handed over: what it is called, and its bytes."""

    name: str
    data: bytes


@dataclass
class Sender:
    """Who sent a message, as far as Telegram will say."""

    name: str
    username: str | None = None
    #: None when Telegram hides the sender — an anonymous admin, say.
    user_id: int | None = None


@dataclass
class ChatMessage:
    """One message of a chat's history, as a tool sees it."""

    id: int
    sender_name: str
    sender_username: str | None
    sender_id: int | None
    content: Content
    date: datetime | None = None
    # The message's images as `data:` URIs, when they were asked for.
    images: list[str] = field(default_factory=list)
    # What it answers: the id it replies to, and that message itself when we could
    # read it — a listing of replies is hard to follow without what they answer.
    reply_to_id: int | None = None
    reply_to: ChatMessage | None = None
    # The part the sender highlighted, when they quoted a selection.
    quote: str = ""


@dataclass
class ChatView:
    """A chat and the messages that were read from it."""

    title: str
    username: str | None
    chat_id: int
    messages: list[ChatMessage]


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

    async def recent_messages(self, chat_id: Entity, limit: int = 50) -> ChatView:
        """A chat and its last `limit` messages, oldest first."""

    async def message(
        self, chat_id: Entity, message_id: int, images: bool = False
    ) -> ChatMessage | None:
        """One message of a chat, or None when there is no such message.

        `images` asks for its pictures to be fetched and carried along too.
        """

    async def download(self, chat_id: Entity, message_id: int) -> DownloadedFile | None:
        """The file a message carries, bytes and name, or None when it carries none.

        None covers a message that is not there as much as a message with no file
        in it. A file too large to hand over, or a chat that cannot be read at
        all, is an error rather than a download.
        """


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

    async def recent_messages(self, chat_id, limit=50) -> ChatView:
        entity = await self.client.get_entity(chat_id)
        messages = await self.client.get_messages(entity, limit=limit)
        read = [
            await chat_message(message)
            for message in messages
            if isinstance(message, TelegramMessage)
        ]
        read.reverse()  # Telegram hands them over newest first
        await with_replies(self.client, entity, read)
        return ChatView(
            title=utils.get_display_name(entity) or "unnamed",
            username=getattr(entity, "username", None),
            chat_id=utils.get_peer_id(entity),
            messages=read,
        )

    async def message(self, chat_id, message_id, images=False) -> ChatMessage | None:
        found = await self.client.get_messages(chat_id, ids=message_id)
        if not isinstance(found, TelegramMessage):
            return None
        read = await chat_message(found)
        if images:
            read.images = await images_of(self.client, found)
        await with_replies(self.client, chat_id, [read])
        return read

    async def download(self, chat_id, message_id) -> DownloadedFile | None:
        found = await self.client.get_messages(chat_id, ids=message_id)
        if not isinstance(found, TelegramMessage) or found.file is None:
            return None
        declared = found.file.size or 0
        if declared > MAX_FILE_BYTES:
            raise ValueError(
                f"the file is {declared} bytes; the most that can be handed over "
                f"is {MAX_FILE_BYTES}"
            )
        handle = await self.client.download_media(found, file=io.BytesIO())
        raw = handle.getvalue() if handle is not None else b""
        if not raw:
            raise ValueError("Telegram handed over nothing")
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError(
                f"the file is {len(raw)} bytes; the most that can be handed over "
                f"is {MAX_FILE_BYTES}"
            )
        return DownloadedFile(name=found.file.name or "", data=raw)


async def images_of(client, message, budget: int = MAX_IMAGES_BYTES) -> list[str]:
    """A message's pictures, as `data:` URIs — at most a few, and none oversized.

    A photo is its own picture; a document is one when it is an image, and
    otherwise contributes its thumbnail, which is how a video, a video note, an
    animation or a video sticker gives up a frame. A link the message carries is
    read by the same rules and its media travels too — the preview's own picture
    or video first, then an Instant View page's when Telegram cached one — a
    video in either giving up its thumbnail as well. Anything else has no
    picture, and nothing here is worth failing a turn over: a download that goes
    wrong simply leaves a placeholder in the prompt.
    """
    wanted = [(message, *image_source(getattr(message, "media", None)))]
    wanted += [(media, *image_source(media)) for media in link_media(message)]
    images: list[str] = []
    left = budget
    for target, kind, thumb, mime in wanted:
        if len(images) >= MAX_IMAGES:
            break
        if kind == "none":
            continue
        image = await _fetched_image(client, target, kind, thumb, mime, left)
        if image:
            images.append(image)
            left -= len(image)
    return images


def link_media(message) -> list:
    """The media a message's link holds: the preview's own, and its page's.

    Telegram keeps a preview's picture or video on the webpage itself, and beside
    a cached Instant View page the article's photos and videos — plain `Photo`
    and `Document` objects either way, the same media a message carries, so the
    same rules decide which of them become pictures. The preview comes first: it
    is what the message itself shows. A link Telegram neither previewed nor
    cached a page for contributes nothing.
    """
    webpage = getattr(message, "web_preview", None)
    if webpage is None:
        return []
    media = [getattr(webpage, "photo", None), getattr(webpage, "document", None)]
    page = getattr(webpage, "cached_page", None)
    if page is not None:
        media += [*(page.photos or []), *(page.documents or [])]
    return [entry for entry in media if entry is not None]


async def _fetched_image(
    client, target, kind: str, thumb: int | None, mime: str, budget: int
) -> str:
    """One picture as a `data:` URI, or "" when it cannot be had."""
    try:
        handle = await client.download_media(
            target, file=io.BytesIO(), thumb=thumb if kind == "thumb" else None
        )
    except Exception:
        return ""
    raw = handle.getvalue() if handle is not None else b""
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        return ""
    encoded = base64.b64encode(raw).decode("ascii")
    uri = f"data:{mime};base64,{encoded}"
    if len(uri) > budget:
        return ""
    return uri


def image_source(media) -> tuple[str, int | None, str]:
    """What picture a piece of media holds: `("file"|"thumb"|"none", size, mime)`.

    A message's media and an Instant View page's media are judged alike: a photo
    is fetched at a size worth reading, an image file whole, and anything else
    gives up the widest thumbnail it has.
    """
    if isinstance(media, types.MessageMediaPhoto):
        media = media.photo
    if isinstance(media, types.Photo):
        return "thumb", best_size(media.sizes), "image/jpeg"
    if isinstance(media, types.MessageMediaDocument):
        media = media.document
    if not isinstance(media, types.Document):
        return "none", None, ""
    mime = media.mime_type or ""
    if mime.startswith("image/"):
        return "file", None, mime
    thumb = best_size(media.thumbs)
    if thumb is None:
        return "none", None, ""
    return "thumb", thumb, "image/jpeg"


def best_size(sizes) -> int | None:
    """Which of a media's sizes to fetch: the widest usable one, never the raw file."""
    usable = [
        (index, size)
        for index, size in enumerate(sizes or [])
        if isinstance(size, (types.PhotoSize, types.PhotoSizeProgressive, types.PhotoCachedSize))
    ]
    if not usable:
        return None
    modest = [pair for pair in usable if pair[1].w <= USEFUL_WIDTH] or usable
    return max(modest, key=lambda pair: pair[1].w * pair[1].h)[0]


async def sender_of(message) -> Sender:
    """Who sent a message — or, when Telegram hides it, as much as it will say.

    A post by an anonymous admin has no sender id at all: the message is the
    group's, and all that may be left is a signature (`post_author`).
    """
    sender = await message.get_sender()
    identifier = message.sender_id
    if identifier:
        return Sender(
            utils.get_display_name(sender) or "unknown",
            getattr(sender, "username", None),
            identifier,
        )
    signature = (getattr(message, "post_author", None) or "").strip()
    return Sender(signature or "anonymous admin")


async def chat_message(message) -> ChatMessage:
    """One message of a history, with its sender resolved."""
    sender = await sender_of(message)
    header = message.reply_to
    return ChatMessage(
        id=message.id,
        sender_name=sender.name,
        sender_username=sender.username,
        sender_id=sender.user_id,
        content=content_of(message),
        date=message.date,
        reply_to_id=message.reply_to_msg_id,
        quote=(getattr(header, "quote_text", None) or "").strip(),
    )


async def with_replies(client, chat_id, messages: list[ChatMessage]) -> None:
    """Fill in what each message answers, for the ones we do not already hold.

    The missing ids are asked for in one go, so a listing full of replies costs a
    single extra request rather than one per message, and a message we already
    have is reused as it is.
    """
    known = {message.id: message for message in messages}
    wanted = sorted(
        {
            message.reply_to_id
            for message in messages
            if message.reply_to_id and message.reply_to_id not in known
        }
    )
    found: dict[int, ChatMessage] = {}
    if wanted:
        answers = await client.get_messages(chat_id, ids=wanted)
        for answer in answers if isinstance(answers, list) else [answers]:
            if isinstance(answer, TelegramMessage):
                found[answer.id] = await chat_message(answer)
    for message in messages:
        quoted = known.get(message.reply_to_id) or found.get(message.reply_to_id)
        if quoted is None:
            continue
        # one level is enough: the quoted message is described on its own
        message.reply_to = replace(quoted, reply_to_id=None, reply_to=None, quote="")

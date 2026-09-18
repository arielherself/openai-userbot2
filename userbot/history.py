"""The tools that let the agent look at messages.

Two of them read a chat's history — this chat, and any public chat by its
`@username` — and two work on a single message of this chat: reading it, and
forwarding it. Every message is described the same way: its id, when it was sent,
who sent it, and its full content, with the same placeholders for media the
prompts use.
"""

from __future__ import annotations

from datetime import datetime

from .telegram import ChatView, Delivery
from .tools import Answer

# How many messages to read, and how much of an answered message to show.
LIMIT = 50
QUOTE_CHARS = 200

VIEW_CURRENT_TOOL = {
    "name": "tg_view_current_chat",
    "description": (
        "Read this chat's recent messages: the last 50 of them, oldest first, each "
        "with its message id, the time it was sent, who sent it (display name, "
        "@username, user id) and its full content — text, and a placeholder for "
        "anything that is not text. A message that answers another one shows what "
        "it answers, so a run of replies reads as a conversation.\n"
        "Use it to see what was being discussed before you were asked, or what "
        "people said after a message you are looking at.\n"
        "Takes no parameters."
    ),
    "params": [],
}

VIEW_PUBLIC_TOOL = {
    "name": "tg_view_public_chat",
    "description": (
        "Read the recent messages of another group or channel: its last 50, oldest "
        "first, each with its message id, the time it was sent, who sent it, its "
        "full content, and what it answers when it is a reply.\n"
        "`username`: the chat's public name, starting with @ — like `@telegram`.\n"
        "Only public chats can be read this way, and only ones this account can "
        "see; a private chat has no username to give."
    ),
    "params": [
        {
            "name": "username",
            "type": "string",
            "description": "a public chat name, starting with @",
        },
    ],
}

READ_TOOL = {
    "name": "tg_read_message",
    "description": (
        "Read one message of this chat by its id: its sender, the time it was sent "
        "and its full content — text, and a placeholder for anything that is not "
        "text.\n"
        "`message_id`: the id of a message — the numbers tg_view_current_chat and "
        "tg_view_public_chat print in brackets.\n"
        "Its pictures come back with it, so you can look at a photo, a picture "
        "sent as a file, or a frame of a video it holds — of a link it carries "
        "too, preview and Instant View page alike.\n"
        "`from_chat`: where that message is, when it is not this chat: a public "
        "name like `@telegram`, or the chat id the view tools printed. Leave it "
        "out for this chat."
    ),
    "params": [
        {"name": "message_id", "type": "integer", "description": "the message id to read"},
        {
            "name": "from_chat",
            "type": "string",
            "description": "the chat the message is in: `@name` or a chat id; omit for this chat",
        },
    ],
}

FORWARD_TOOL = {
    "name": "tg_forward_message",
    "description": (
        "Forward one message of this chat into this chat, so the people here see it "
        "again — an older message worth bringing back to the end, for instance.\n"
        "`message_id`: the id of a message — the numbers tg_view_current_chat and "
        "tg_view_public_chat print in brackets.\n"
        "`from_chat`: where that message is, when it is not this chat: a public "
        "name like `@telegram`, or the chat id the view tools printed. Leave it "
        "out for this chat.\n"
        "Returns a confirmation once it has been forwarded."
    ),
    "params": [
        {"name": "message_id", "type": "integer", "description": "the message id to forward"},
        {
            "name": "from_chat",
            "type": "string",
            "description": "the chat the message is in: `@name` or a chat id; omit for this chat",
        },
    ],
    # The forwarded copy really was posted, so a failed turn takes it back.
    "rollback": True,
    "external_effects": True,
}

TOOLS = [VIEW_CURRENT_TOOL, VIEW_PUBLIC_TOOL, READ_TOOL, FORWARD_TOOL]


async def current_chat(delivery: Delivery, chat_id: int, limit: int = LIMIT) -> Answer:
    """The messages of the chat this conversation is happening in."""
    return await _read(delivery, chat_id, limit, "this chat")


async def public_chat(delivery: Delivery, username: str, limit: int = LIMIT) -> Answer:
    """The messages of a public chat, named the way Telegram names it."""
    if not username.startswith("@"):
        return Answer(error="`username` must start with @, like `@telegram`")
    return await _read(delivery, username, limit, "that chat")


async def _read(delivery: Delivery, chat, limit: int, what: str) -> Answer:
    try:
        view = await delivery.recent_messages(chat, limit)
    except Exception as error:
        return Answer(error=f"could not read {what}: {error}")
    return Answer(result=render(view))


async def read_message(delivery: Delivery, chat_id: int, message_id: int, source=None) -> Answer:
    """What one message says — of this chat, or of one the caller names.

    Its pictures come with it: the model sees a photo, a picture sent as a file,
    or a frame of a video, not just the placeholder.
    """
    message, complaint = await _one(delivery, chat_id, message_id, source, images=True)
    if message is None:
        return Answer(error=complaint)
    return Answer(result=entry(message), images=message.images)


async def forward_message(delivery: Delivery, chat_id: int, message_id: int, source=None) -> Answer:
    """Put one message back at the end of this chat, wherever it came from."""
    message, complaint = await _one(delivery, chat_id, message_id, source)
    if message is None:
        return Answer(error=complaint)
    try:
        forwarded = await delivery.forward(source or chat_id, message_id, chat_id)
    except Exception as error:
        return Answer(error=f"could not forward that message: {error}")
    return Answer(result="the message was forwarded into this chat", forwarded=forwarded)


async def _one(
    delivery: Delivery, chat_id: int, message_id: int, source=None, images: bool = False
) -> tuple:
    """The message, or why it cannot be reached."""
    where = "this chat" if source is None else f"the chat {source}"
    try:
        message = await delivery.message(source or chat_id, message_id, images=images)
    except Exception as error:
        return None, f"could not read message {message_id} from {where}: {error}"
    if message is None:
        return None, f"there is no message {message_id} in {where}"
    return message, ""


def sender_line(message) -> str:
    """`[id] time name (@username, user id N):` — the head of every entry.

    A message whose sender Telegram hides — an anonymous admin — simply has no
    id and no username to print, and keeps the name it does have.
    """
    who = [f"user id {message.sender_id}"] if message.sender_id else []
    if message.sender_username:
        who.insert(0, f"@{message.sender_username}")
    named = f"{message.sender_name} ({', '.join(who)})" if who else message.sender_name
    return f"[{message.id}] {stamp(message.date)}{named}:"


def entry(message) -> str:
    """One message: who sent it, when, what it answers, and what it said."""
    lines = [sender_line(message)]
    if message.reply_to is not None or message.reply_to_id:
        lines.append(quoted_line(message))
    lines.append(message.content.render())
    return "\n".join(lines)


def quoted_line(message) -> str:
    """`↩ in reply to [id] who: what it said` — one line, clipped, never recursive.

    The quoted message is what makes a run of replies readable; when it is longer
    than a line it is cut, and its id is right there to read in full.
    """
    quoted = message.reply_to
    if quoted is None:
        return f"↩ in reply to [{message.reply_to_id}] a message that is gone"
    who = quoted.sender_name
    if quoted.sender_username:
        who += f" (@{quoted.sender_username})"
    said = " ".join(quoted.content.render().split())
    if len(said) > QUOTE_CHARS:
        said = said[: QUOTE_CHARS - 1] + "…"
    line = f"↩ in reply to [{quoted.id}] {who}: {said}"
    if message.quote:
        line += f"\n↩ they highlighted: {message.quote}"
    return line


def stamp(date: datetime | None) -> str:
    """A message's time, in the reader's own timezone, with the offset spelled out."""
    if date is None:
        return ""
    return date.astimezone().strftime("%Y-%m-%d %H:%M %z ")


def render(view: ChatView, limit: int = LIMIT) -> str:
    """The reading, as the model sees it."""
    where = view.title
    if view.username:
        where += f" (@{view.username})"
    where += f", {view.chat_id}"
    if not view.messages:
        return f"{where}: no messages"
    lines = [f"the last {len(view.messages)} message(s) in {where}, oldest first:"]
    for index, message in enumerate(view.messages[:limit], start=1):
        lines.append(f"{index}. {entry(message)}")
    return "\n".join(lines)

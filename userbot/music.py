"""The two tools that reach the music bot.

`tg_search_music` asks @Music163DownBot for tracks and hands the agent back what
the bot prints — a numbered list of songs, each with a link. `tg_send_music`
hands the bot one of those links, waits for the audio it returns, and forwards
that into the chat the request came from.

Both talk to the bot by sending it a command and listening for the message that
answers it — listening starts before the command goes out, so a bot that answers
instantly cannot be missed. A bot that says nothing at all is given a short grace
period, because a working bot acknowledges a command almost immediately; after
that it may take its time to produce the answer itself. Either way, a failure is
reported as an error, never as a silent success.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .relay import Answer, ask
from .telegram import BotMessage, Delivery

# @Music163DownBot — the account that fetches the tracks.
MUSIC_BOT = "Music163DownBot"

# What the agent calls a platform, and what the bot calls it.
PLATFORMS = {
    "NetEase": "163",
    "AppleMusic": "am",
    "QQMusic": "qq",
    "Soda": "qs",
}

# How long the bot may stay completely silent after a command before we give up
# on it, and how long it may take to produce the answer once it has spoken.
FIRST_REPLY_TIMEOUT = 30.0
SEARCH_TIMEOUT = 300.0
MUSIC_TIMEOUT = 300.0

# The pause between one music call and the next. The bot is a shared service, so
# the whole userbot makes one call at a time and waits this long after each.
MUSIC_GAP = 5.0

# What the harness is told to wait for an answer from us. It has to outlast the
# waits above, or a slow fetch is reported to the model as a tool timeout.
LOCAL_TIMEOUT = 900.0

SEARCH_TOOL = {
    "name": "tg_search_music",
    "description": (
        "Search for a track through the music bot and get back what it prints: a "
        "numbered song per line, each with its link, as the bot wrote it — the "
        "titles and links come as markdown, `[title](link)`.\n"
        "`platform`: which service to search — NetEase, AppleMusic, QQMusic or "
        "Soda. Try them in that order: search NetEase first and fall back to the "
        "next one when a search comes back empty, fails, or has nothing matching.\n"
        "Read the links out of the result and pass one of them to tg_send_music."
    ),
    "params": [
        {
            "name": "keyword",
            "type": "string",
            "description": "what to search for: the title, the artist, or both",
        },
        {
            "name": "platform",
            "type": "string",
            "description": "NetEase, AppleMusic, QQMusic or Soda — in that order of preference",
        },
    ],
}

SEND_TOOL = {
    "name": "tg_send_music",
    "description": (
        "Send a track into the chat this conversation is in: the music bot fetches "
        "it and the audio file it returns is forwarded here.\n"
        "`url`: a song link — one of the links from a tg_search_music result, or "
        "any other link the music bot understands.\n"
        "`platform`: the service that link belongs to — NetEase, AppleMusic, "
        "QQMusic or Soda — and it must be the one the link came from.\n"
        "Waits for the bot to finish fetching, which can take a few minutes. "
        "Returns the bot's own message when it refuses, and a confirmation once "
        "the file has been sent; an error when the bot cannot be reached or never "
        "answers."
    ),
    "params": [
        {"name": "url", "type": "string", "description": "the song link to fetch and send"},
        {
            "name": "platform",
            "type": "string",
            "description": "NetEase, AppleMusic, QQMusic or Soda — the service the link is from",
        },
    ],
    # The file really was posted, so a turn that fails afterwards takes it back.
    "rollback": True,
    "external_effects": True,
}

TOOLS = [SEARCH_TOOL, SEND_TOOL]


class Busy(Exception):
    """The rate limit left no time for this call."""


class Gate:
    """The userbot's one-at-a-time rule for the music bot.

    However many conversations are running, only one of them talks to the bot at
    a time, and the next one waits out a pause measured from the end of the last
    call — not from its start, so a slow fetch does not eat into the pause. The
    wait for a turn comes out of the caller's own budget: a call that would spend
    its whole allowance queueing gives up instead of waiting in line.
    """

    def __init__(self, gap: float = MUSIC_GAP) -> None:
        self.gap = gap
        self._lock = asyncio.Lock()
        self._free_at = 0.0

    @asynccontextmanager
    async def slot(self, deadline: float, budget: float) -> AsyncIterator[None]:
        """Take the bot for the duration of one call, or raise `Busy`."""
        async with self._lock:
            loop = asyncio.get_running_loop()
            wait = self._free_at - loop.time()
            left = deadline - loop.time()
            if wait > left:
                raise Busy(
                    f"the music bot is busy: a call is already queued {wait:.0f}s ahead, "
                    f"and only {max(left, 0):.0f}s of this call's {budget:.0f}s are left"
                )
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                yield
            finally:
                self._free_at = asyncio.get_running_loop().time() + self.gap


@asynccontextmanager
async def _slot(gate: Gate | None, deadline: float, budget: float) -> AsyncIterator[None]:
    """The rate limit, when one was given."""
    if gate is None:
        yield
        return
    async with gate.slot(deadline, budget):
        yield


def resolve_platform(name) -> str | None:
    """The canonical platform name, or None when it is not one of ours."""
    if not isinstance(name, str):
        return None
    wanted = name.strip().lower()
    for platform in PLATFORMS:
        if platform.lower() == wanted:
            return platform
    return None


def platform_names() -> str:
    return ", ".join(PLATFORMS)


def _listing(message: BotMessage, _sent: int) -> bool:
    """The search bot answers with a list under a keyboard of numbered picks."""
    return message.has_buttons and bool(message.text.strip())


def _track(message: BotMessage, _sent: int) -> bool:
    """Either the audio itself, or the bot saying it could not fetch it."""
    return message.has_music or "fail" in message.text.lower()


async def search(
    delivery: Delivery,
    keyword: str,
    platform: str,
    bot: str = MUSIC_BOT,
    timeout: float = SEARCH_TIMEOUT,
    first_timeout: float = FIRST_REPLY_TIMEOUT,
    gate: Gate | None = None,
) -> Answer:
    """Ask the bot to search, and hand back what it prints.

    `timeout` is the whole call: the wait for a turn at the rate limit, the bot's
    grace period, and the answer itself.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        async with _slot(gate, deadline, timeout):
            message, complaint = await ask(
                delivery,
                bot,
                f"/search {keyword} {PLATFORMS[platform]}",
                _listing,
                "music bot",
                first_timeout,
                deadline,
                timeout,
            )
    except Busy as error:
        return Answer(error=str(error))
    if message is None:
        return Answer(error=complaint)
    # the listing as the bot wrote it: plain text has had its links stripped out
    listing = message.markdown or message.text
    return Answer(result=listing.strip() or "(the music bot sent an empty message)")


async def send(
    delivery: Delivery,
    url: str,
    platform: str,
    to_chat: int,
    bot: str = MUSIC_BOT,
    timeout: float = MUSIC_TIMEOUT,
    first_timeout: float = FIRST_REPLY_TIMEOUT,
    gate: Gate | None = None,
) -> Answer:
    """Have the bot fetch a track, and put the file it returns into `to_chat`.

    `timeout` is the whole call, the wait for the rate limit included.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        async with _slot(gate, deadline, timeout):
            message, complaint = await ask(
                delivery,
                bot,
                f"/music {url} {PLATFORMS[platform]}",
                _track,
                "music bot",
                first_timeout,
                deadline,
                timeout,
            )
            if message is None:
                return Answer(error=complaint)
            if not message.has_music:
                return Answer(
                    result=message.text.strip() or "(the music bot sent an empty message)"
                )
            try:
                forwarded = await delivery.forward(bot, message.id, to_chat)
            except Exception as error:
                return Answer(error=f"the track arrived but forwarding it failed: {error}")
    except Busy as error:
        return Answer(error=str(error))
    return Answer(result="the track was sent to the chat", forwarded=forwarded)

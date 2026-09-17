"""Talking to another bot on the userbot's behalf.

The music bot and the parse bot pose the same problem: send a command to an
account, wait for what it says back, and put that answer into the chat. What
differs per bot — the command, what counts as an answer, what to do with one —
stays with the tool that owns it; this is only the part that is the same.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from .telegram import BotMessage, Delivery, Listener


@dataclass
class Answer:
    """What such a tool tells the model: a result, or why there is none."""

    result: str = ""
    error: str = ""
    # What a rollback would have to delete: what the tool put into the chat.
    forwarded: int | None = None


# Given a message and the id of the command we sent, is this the answer?
Accept = Callable[[BotMessage, int], bool]


async def wait_for_answer(
    listener: Listener,
    accept: Accept,
    sent: int,
    name: str,
    first_timeout: float,
    deadline: float,
    budget: float,
) -> tuple[BotMessage | None, str]:
    """The message that answers us, or a complaint about why there is none.

    The bot has `first_timeout` to show any sign of life, and the rest of the
    call's budget to produce the answer itself.
    """
    loop = asyncio.get_running_loop()
    window = min(first_timeout, max(deadline - loop.time(), 0.0))
    message = await listener.next(window)
    if message is None:
        return None, f"the {name} said nothing within {window:g}s of the command"
    while True:
        if accept(message, sent):
            return message, ""
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        message = await listener.next(remaining)
        if message is None:
            break
    return None, f"the {name} did not answer within {budget:g}s"


async def ask(
    delivery: Delivery,
    bot: str,
    command: str,
    accept: Accept,
    name: str,
    first_timeout: float,
    deadline: float,
    budget: float,
) -> tuple[BotMessage | None, str]:
    """Send a bot a command and wait for the message that answers it.

    Watching starts before the command goes out, so a bot that answers instantly
    cannot be missed. `accept(message, sent)` decides what the answer is; the id
    of our own message is there for bots that answer by replying to it.
    """
    try:
        listener = delivery.listen(bot)
    except Exception as error:
        return None, f"could not watch the {name}: {error}"
    try:
        try:
            sent = await delivery.send(bot, command)
        except Exception as error:
            return None, f"could not reach the {name}: {error}"
        return await wait_for_answer(listener, accept, sent, name, first_timeout, deadline, budget)
    finally:
        listener.close()

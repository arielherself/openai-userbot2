"""Scheduled tasks: the three tools, and the clock that runs them.

A task is set with a local time, a piece of text, and the message the turn was
answering — that is where its answer goes. When the time comes the task fires:
an agent turn starts with that text as its prompt, and the reply is drafted into
the same chat, as a reply to that message. A task fires once and is gone.

The clock is one long-lived asyncio task of the userbot's own: it asks the store
what is due, hands each task to the bridge to run on its own task, and sleeps
until the next one is due. A task that came due while the userbot was away fires
when it starts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .store import MappingStore, Schedule
from .tools import Answer, text_argument

if TYPE_CHECKING:
    from .bridge import Bridge

log = logging.getLogger(__name__)

# The most tasks that may be waiting at once, across every chat.
MAX_TASKS = 20
# How far ahead a task may be set: 24 hours, not a moment more.
MAX_AHEAD = 24 * 60 * 60
# A time a little behind the clock is still taken: the model writes minutes, so
# a "now" rounded down to the minute is a rounding, not a mistake.
GRACE = 60.0
# The longest the clock sleeps when nothing is due sooner.
IDLE_SECONDS = 5.0
# The shortest, so a task that is due this moment does not spin the loop.
NUDGE_SECONDS = 0.05

#: The shapes an `at` may be written in; a missing seconds part means zero.
TIME_SHAPES = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")

ADD_TOOL = {
    "name": "tg_add_schedule",
    "description": (
        "Schedule a task for later: at `at`, an agent turn starts in this "
        "conversation with `content` as its task, and the answer it drafts comes "
        "back to this chat as a reply to the message you are answering now.\n"
        "`at` is the local time of the machine the userbot runs on, written "
        "`YYYY-MM-DD HH:MM` (seconds optional). It must be in the future and at "
        "most 24 hours ahead; anything further out is refused. Call "
        "get_current_time first if you are not sure what time it is now.\n"
        "`content` is the whole prompt that future turn gets: say what to do and "
        "what to report, in the language the answer should be written in.\n"
        "At most 20 tasks may be waiting at once; when the list is full this tool "
        "answers with an error. tg_view_schedule lists what is waiting, and "
        "tg_remove_schedule frees a slot.\n"
        "Say in your reply what you scheduled and when it fires."
    ),
    "params": [
        {
            "name": "content",
            "type": "string",
            "description": (
                "the task the future turn carries out, in the language the answer should be in"
            ),
        },
        {
            "name": "at",
            "type": "string",
            "description": (
                "local time to fire, `YYYY-MM-DD HH:MM`; in the future, at most 24 hours ahead"
            ),
        },
    ],
    # The task can be taken back: a turn that fails cancels it again.
    "rollback": True,
    "external_effects": True,
}

VIEW_TOOL = {
    "name": "tg_view_schedule",
    "description": (
        "Every scheduled task that is waiting, soonest first — across all chats, "
        "each with its id, when it fires in local time, and the content it will "
        "run. A task that has fired is no longer listed. Use the id with "
        "tg_remove_schedule to cancel one."
    ),
    "params": [],
}

REMOVE_TOOL = {
    "name": "tg_remove_schedule",
    "description": (
        "Cancel one scheduled task, by the id tg_view_schedule printed. It will "
        "not fire, and its slot is free for another."
    ),
    "params": [
        {
            "name": "id",
            "type": "string",
            "description": "the id the task was listed under, like `sch-1a2b3c4d5e6f`",
        },
    ],
}

TOOLS = [ADD_TOOL, VIEW_TOOL, REMOVE_TOOL]


def new_task_id() -> str:
    """A task id, short enough to read back and unlikely to repeat."""
    return f"sch-{os.urandom(6).hex()}"


def parse_time(text: str) -> float:
    """A local time as the model writes it, as epoch seconds."""
    for shape in TIME_SHAPES:
        try:
            # `astimezone` reads a naive time as this machine's own clock, the
            # one a task is set by.
            return datetime.strptime(text.strip(), shape).astimezone().timestamp()
        except ValueError:
            pass
    raise ValueError(text)


def format_time(when: float) -> str:
    """A time in the reader's own timezone, with the offset spelled out."""
    return datetime.fromtimestamp(when, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %z")


def schedule_arguments(arguments: dict) -> tuple[str, float, str | None]:
    """What a new task says and when it fires, or what is wrong with them."""
    content, complaint = text_argument(arguments, "content")
    if complaint is not None:
        return "", 0.0, complaint
    at = arguments.get("at")
    if not isinstance(at, str) or not at.strip():
        return "", 0.0, "`at` must be a local time like `2026-09-19 14:30`"
    try:
        when = parse_time(at)
    except ValueError:
        return "", 0.0, f"`at` is not a local time like `2026-09-19 14:30`: {at.strip()}"
    now = time.time()
    if when > now + MAX_AHEAD:
        return "", 0.0, f"`at` must be within 24 hours: {format_time(when)} is too far ahead"
    if when < now - GRACE:
        return "", 0.0, f"`at` has already passed: {format_time(when)}"
    return content, when, None


def add_task(
    store: MappingStore,
    content: str,
    due_at: float,
    chat_id: int,
    message_id: int,
    agent_id: str,
) -> Answer:
    """Put a task on the list, unless the list is already full."""
    if store.count_schedules() >= MAX_TASKS:
        return Answer(
            error=(
                f"there are already {MAX_TASKS} scheduled tasks; "
                "cancel one with tg_remove_schedule before adding another"
            )
        )
    task = Schedule(
        id=new_task_id(),
        content=content,
        due_at=due_at,
        chat_id=chat_id,
        message_id=message_id,
        agent_id=agent_id,
    )
    store.add_schedule(task)
    log.info("scheduled %s for chat %s at %s", task.id, chat_id, format_time(due_at))
    return Answer(
        result=(
            f"scheduled as {task.id}: it fires at {format_time(due_at)}, and the "
            "answer goes to this chat as a reply to this message"
        ),
        scheduled=task.id,
    )


def view_tasks(store: MappingStore) -> Answer:
    """Every task that is waiting, soonest first."""
    tasks = store.schedules()
    if not tasks:
        return Answer(result="no scheduled tasks are waiting")
    now = time.time()
    lines = [f"{len(tasks)} scheduled task(s), soonest first:"]
    for index, task in enumerate(tasks, start=1):
        lines.append(
            f"{index}. [{task.id}] {format_time(task.due_at)} ({_away(task.due_at - now)}), "
            f"chat {task.chat_id}, replying to message {task.message_id}:"
        )
        lines.append(task.content)
    return Answer(result="\n".join(lines))


def remove_task(store: MappingStore, task_id: str) -> Answer:
    """Take one task off the list, so it never fires."""
    if not store.remove_schedule(task_id):
        return Answer(error=f"there is no scheduled task {task_id}")
    log.info("cancelled the scheduled task %s", task_id)
    return Answer(result=f"cancelled {task_id}: it will not fire")


async def run_scheduler(bridge: Bridge, store: MappingStore, tasks: set) -> None:
    """Fire each task when its time comes, until the process stops.

    Every due task runs as its own asyncio task, handed to `tasks` — the set the
    process gathers before it shuts down — so a slow one never holds up the
    others. The store is asked afresh every time the clock wakes: a task fires
    once, and is gone from it before its turn even starts.
    """
    while True:
        try:
            for task in store.due_schedules(time.time()):
                store.remove_schedule(task.id)
                log.info("task %s is due: it fires in chat %s", task.id, task.chat_id)
                fired = asyncio.create_task(bridge.fire(task))
                tasks.add(fired)
                fired.add_done_callback(tasks.discard)
            wait = _wait(store.next_due(), time.time())
        except Exception:  # a stumble must not stop the clock for good
            log.exception("the schedule clock stumbled")
            wait = IDLE_SECONDS
        await asyncio.sleep(wait)


def _wait(next_due: float | None, now: float) -> float:
    """How long the clock may sleep: until the next task, or the idle bound."""
    if next_due is None:
        return IDLE_SECONDS
    return max(NUDGE_SECONDS, min(IDLE_SECONDS, next_due - now))


def _away(seconds: float) -> str:
    """How far off a time is, rounded, for the line that names it."""
    minutes = max(0, int(seconds + 30) // 60)
    if minutes < 1:
        return "in under a minute"
    if minutes < 60:
        return f"in {minutes} minute(s)"
    hours, rest = divmod(minutes, 60)
    return f"in {hours} hour(s)" + (f" {rest} minute(s)" if rest else "")

"""The bridge: a Telegram message in, an agent turn in the harness, messages out.

Only two things start a turn. A message that mentions us opens a fresh
conversation — a fork from the chat's root block. A message that replies to one we
sent continues that conversation: the reply's message id is looked up in the
mapping store, and the turn forks from the very block that produced it. A reply to
a message we never mapped is ignored, silently, because there is nothing to
continue.

Everything the agent says reaches the user through `tg_draft_response`. While the
turn runs, one status message follows it, edited in place; when the reply goes out
that message is deleted, and if the turn fails instead, the failure is reported
back to the sender.

A message that replies to somebody else is quoted into the prompt, so the agent
can see what is being talked about — text, and a placeholder for anything in it
that is not text (a file's name, a poll's options, a link's Instant View).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .content import Content, Quoted
from .fetch import TOOLS as FETCH_TOOLS
from .fetch import (
    CommandTransfer,
    Transfer,
    clone_arguments,
    clone_to_sandbox,
    curl_to_sandbox,
    fetch_arguments,
)
from .harness import CONNECTION_LOST, TG_DRAFT_TOOL, HarnessError, new_agent_id
from .history import TOOLS as VIEW_TOOLS
from .history import current_chat as view_current_chat
from .history import forward_message as forward_one_message
from .history import public_chat as view_public_chat
from .history import read_message as read_one_message
from .music import LOCAL_TIMEOUT, SEARCH_TOOL, SEND_TOOL, Gate, platform_names, resolve_platform
from .music import search as search_music
from .music import send as send_music
from .parse import TOOL as PARSE_TOOL
from .parse import send as send_parsed
from .render import MAX_UNITS, build_reply_parts, clamp_units
from .sandbox import (
    SEND_FILE_TOOL,
    SEND_FROM_SANDBOX_TOOL,
    download_to_sandbox,
    export_arguments,
    send_arguments,
    send_from_sandbox,
)
from .sandbox import TOOL as DOWNLOAD_TOOL
from .sandbox import read_arguments as download_arguments
from .sandbox import send_file as send_file_to_chat
from .schedule import TOOLS as SCHEDULE_TOOLS
from .schedule import add_task, remove_task, schedule_arguments, view_tasks
from .status import StatusMessage, TurnStatus
from .store import Schedule
from .telegram import MAX_IMAGES
from .tools import chat_argument, message_id_argument, text_argument

log = logging.getLogger(__name__)

TOOL_NAME = TG_DRAFT_TOOL["name"]
SEARCH_TOOL_NAME = SEARCH_TOOL["name"]
SEND_TOOL_NAME = SEND_TOOL["name"]
PARSE_TOOL_NAME = PARSE_TOOL["name"]
VIEW_CURRENT_TOOL_NAME = VIEW_TOOLS[0]["name"]
VIEW_PUBLIC_TOOL_NAME = VIEW_TOOLS[1]["name"]
READ_TOOL_NAME = VIEW_TOOLS[2]["name"]
FORWARD_TOOL_NAME = VIEW_TOOLS[3]["name"]
DOWNLOAD_TOOL_NAME = DOWNLOAD_TOOL["name"]
SEND_FROM_SANDBOX_TOOL_NAME = SEND_FROM_SANDBOX_TOOL["name"]
SEND_FILE_TOOL_NAME = SEND_FILE_TOOL["name"]
CLONE_TOOL_NAME = FETCH_TOOLS[0]["name"]
CURL_TOOL_NAME = FETCH_TOOLS[1]["name"]
ADD_SCHEDULE_TOOL_NAME = SCHEDULE_TOOLS[0]["name"]
VIEW_SCHEDULE_TOOL_NAME = SCHEDULE_TOOLS[1]["name"]
REMOVE_SCHEDULE_TOOL_NAME = SCHEDULE_TOOLS[2]["name"]

# The protocol that carries images on a fork and on a tool result.
IMAGE_PROTOCOL = 3
# The protocol that carries a tool pipe: a local tool answering with the tool to
# run next, which is how a file reaches a sandbox without passing through the model.
PIPE_PROTOCOL = 4

# Everything the agent can ask this userbot to do.
LOCAL_TOOLS = [
    TG_DRAFT_TOOL,
    SEARCH_TOOL,
    SEND_TOOL,
    PARSE_TOOL,
    DOWNLOAD_TOOL,
    SEND_FROM_SANDBOX_TOOL,
    SEND_FILE_TOOL,
    *FETCH_TOOLS,
    *VIEW_TOOLS,
    *SCHEDULE_TOOLS,
]

# The tools that declare a rollback: a turn that failed undoes their work.
UNDOABLE = (
    TOOL_NAME,
    SEARCH_TOOL_NAME,
    SEND_TOOL_NAME,
    PARSE_TOOL_NAME,
    FORWARD_TOOL_NAME,
    ADD_SCHEDULE_TOOL_NAME,
    SEND_FILE_TOOL_NAME,
)


@dataclass
class Incoming:
    """One Telegram message, normalised so the bridge needs no Telethon."""

    chat_id: int
    message_id: int
    content: Content
    sender_id: int | None
    sender_name: str
    sender_username: str | None = None
    chat_title: str | None = None
    chat_username: str | None = None
    is_group: bool = False
    mentioned: bool = False
    reply_to_message_id: int | None = None
    quoted: Quoted | None = None
    # The message's pictures, as `data:` URIs, ready to ride along with the fork.
    images: list[str] = field(default_factory=list)
    # A task that came due rather than a message from anyone: there is no sender
    # to name, and the prompt is built as the task it is.
    scheduled: bool = False


@dataclass
class Identity:
    """The userbot account itself — whose voice the agent is writing in."""

    name: str = ""
    username: str | None = None
    user_id: int = 0


def _who(name: str, username: str | None, user_id: int | None) -> str:
    """`name (@username, user id N)` — the parts Telegram would tell us.

    A message whose sender is hidden — an anonymous admin — has no id and often
    no username, and is described by whatever is left.
    """
    bits = []
    if username:
        bits.append(f"@{username}")
    if user_id:
        bits.append(f"user id {user_id}")
    named = name or "unknown"
    return f"{named} ({', '.join(bits)})" if bits else named


def build_prompt(message: Incoming, identity: Identity | None = None) -> str:
    """The envelope the agent sees: who it writes as, who wrote what, and how to answer."""
    header = ["[telegram]"]
    if identity is not None:
        header.append(
            "you are replying on behalf of the userbot "
            f"{_who(identity.name, identity.username, identity.user_id)}"
        )
    if message.scheduled:
        # No sender to name: the text is what a turn asked for itself, earlier,
        # and the answer goes on where that turn happened.
        header.append(
            "a scheduled task you set earlier is due now, and what you write goes "
            "to the chat it was set in, as a reply to the message it was set for"
        )
    else:
        header.append(
            f"from: {_who(message.sender_name, message.sender_username, message.sender_id)}"
        )
        if message.is_group:
            group = [f"group id {message.chat_id}"]
            if message.chat_username:
                group.insert(0, f"@{message.chat_username}")
            header.append(f"group: {message.chat_title or 'unnamed group'} ({', '.join(group)})")
        if message.quoted is not None:
            quoted = message.quoted
            header.append(
                f"replying to {_who(quoted.sender_name, quoted.sender_username, quoted.sender_id)}:"
            )
            if quoted.excerpt:
                header.append(f"the sender highlighted: {quoted.excerpt}")
            header.append("--- quoted message ---")
            header.append(quoted.content.render())
            header.append("--- end of quoted message ---")
        elif message.reply_to_message_id is not None:
            header.append("this message replies to one you sent earlier")
    label = "task" if message.scheduled else "message"
    subject = "the task" if message.scheduled else "the sender's message"
    return (
        "\n".join(header)
        + f"\n{label}:\n"
        + message.content.render()
        + "\n[/telegram]\n"
        + f"Answer in the language of {subject} above — in English if that "
        + "language cannot be told — and deliver it by calling "
        + f"{TOOL_NAME} (a short reply in `summary`, the full answer in `details`, "
        + "which the sender reads too) as the last thing you do."
    )


def _short(text, limit: int = 200) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def is_inspect(message: Incoming) -> bool:
    """Whether this is `/inspect` aimed at one of our messages."""
    if message.reply_to_message_id is None:
        return False
    return message.content.text.strip().lower().startswith("/inspect")


def _music_arguments(arguments: dict, field: str) -> tuple[str, str, str | None]:
    """The platform and the text a music tool was given, or what is wrong with them."""
    platform = resolve_platform(arguments.get("platform"))
    if platform is None:
        return "", "", f"`platform` must be one of {platform_names()}"
    value, complaint = text_argument(arguments, field)
    if complaint is not None:
        return "", "", complaint
    return platform, value, None


def _one_message_arguments(event: dict) -> tuple[int, object | None, str | None]:
    """The message id and the chat a single-message tool was pointed at."""
    arguments = event.get("arguments") or {}
    message_id, complaint = message_id_argument(arguments)
    if complaint is not None:
        return 0, None, complaint
    source, complaint = chat_argument(arguments)
    return message_id, source, complaint


def _failure_reason(event: dict) -> str:
    kind, error = event.get("error_type"), event.get("error")
    for candidate in (error, kind):
        if candidate:
            return _short(candidate, 800)
    return "the agent failed"


@dataclass
class _Turn:
    """What one running turn needs to keep track of."""

    message: Incoming
    block: str
    parent: str
    status: TurnStatus
    tracker: StatusMessage
    started: float = 0.0
    sent: list[int] = field(default_factory=list)
    delivered: bool = False
    # What a rollback would have to delete, per local call: the messages a call
    # put into the chat, keyed by that call's id.
    effects: dict[str, list[int]] = field(default_factory=dict)
    # What a rollback would have to cancel, per local call: the tasks a call
    # scheduled, keyed by that call's id.
    scheduled: dict[str, str] = field(default_factory=dict)


class Bridge:
    def __init__(
        self,
        harness,
        store,
        delivery,
        status_interval: float = 2.0,
        turn_timeout: float = 3600.0,
        model: str | None = None,
        identity: Identity | None = None,
        music_gate: Gate | None = None,
        transfer: Transfer | None = None,
    ) -> None:
        self.hh = harness
        self.store = store
        self.delivery = delivery
        self.status_interval = status_interval
        self.turn_timeout = turn_timeout
        self.model = model
        self.identity = identity
        # one gate for the whole userbot: the music bot is shared between chats
        self.music_gate = music_gate if music_gate is not None else Gate()
        # where a clone or a URL is fetched, on this side of the wire
        self.transfer = transfer if transfer is not None else CommandTransfer()
        # the turns running right now, by block, for `/inspect` to look at
        self._turns: dict[str, _Turn] = {}

    # --- entry point --------------------------------------------------------
    async def handle(self, message: Incoming) -> None:
        """Answer one message, if it is addressed to us. Never raises."""
        if is_inspect(message):
            await self._inspect(message)
            return
        try:
            parent = await self._parent(message)
        except Exception as error:
            log.exception("could not open a conversation in chat %s", message.chat_id)
            await self._failure(message, None, f"could not reach the agent harness: {error}")
            return
        if parent is None:
            log.debug(
                "message %s in chat %s is not addressed to us", message.message_id, message.chat_id
            )
            return
        block = None
        try:
            block, forked_from = await self._fork(message, parent)
            await self._turn(message, block, forked_from)
        except HarnessError as error:
            log.warning("the harness refused the turn for %s: %s", message.message_id, error)
            await self._failure(message, block, str(error))
        except Exception as error:
            log.exception("the turn for %s broke", message.message_id)
            await self._failure(message, block, f"internal error: {error}")

    async def fire(self, task: Schedule) -> None:
        """Run one task that has come due: its text goes to the agent, the answer here."""
        message = Incoming(
            chat_id=task.chat_id,
            message_id=task.message_id,
            content=Content(text=task.content),
            sender_id=None,
            sender_name="a scheduled task",
            scheduled=True,
        )
        block = None
        try:
            block, forked_from = await self._fork(message, task.agent_id)
            await self._turn(message, block, forked_from)
        except HarnessError as error:
            log.warning("the harness refused the scheduled task %s: %s", task.id, error)
            await self._failure(message, block, str(error))
        except Exception as error:
            log.exception("the scheduled task %s broke", task.id)
            await self._failure(message, block, f"internal error: {error}")

    async def _inspect(self, message: Incoming) -> None:
        """Answer `/inspect`: what the turn behind the replied-to message is doing."""
        block = self.store.lookup(message.chat_id, message.reply_to_message_id)
        if block is None:
            return  # a message we never sent: not ours to explain
        log.info("inspecting %s for chat %s", block, message.chat_id)
        sent = await self.delivery.send(
            message.chat_id, self._inspection(block), reply_to=message.message_id
        )
        # the answer is a message of ours like any other: it can be asked about too
        self.store.record(message.chat_id, sent, block)

    def _inspection(self, block: str) -> str:
        """Everything known about a block's turn, with nothing clipped."""
        turn = self._turns.get(block)
        if turn is None:
            return f"🔍 {block}\n\nno turn of mine is running for that block any more"
        elapsed = time.monotonic() - turn.started
        state = [
            f"chat {turn.message.chat_id}",
            f"asked in message {turn.message.message_id}",
            f"forked from {turn.parent}",
            f"reply sent as {len(turn.sent)} message(s)"
            if turn.delivered
            else "reply not sent yet",
            f"{elapsed:.0f}s in",
        ]
        return "\n".join([f"🔍 {block}", "", turn.status.detail(), "", " · ".join(state)])

    # --- choosing where the turn forks from ----------------------------------
    async def _parent(self, message: Incoming) -> str | None:
        if message.reply_to_message_id is not None:
            block = self.store.lookup(message.chat_id, message.reply_to_message_id)
            if block is not None:
                return block
            if not message.mentioned:
                return None  # a reply to something we did not answer: stay quiet
        if message.mentioned:
            return await self._chat_root(message.chat_id)
        return None

    async def _chat_root(self, chat_id: int, refresh: bool = False) -> str:
        """The chat's root block — where a fresh conversation forks from."""
        if refresh:
            self.store.forget_root(chat_id)
        root = self.store.get_root(chat_id)
        if root is not None:
            return root
        root = new_agent_id("root")
        fields = {
            "command": "create_agent",
            "id": root,
            "local_tools": LOCAL_TOOLS,
            # A local tool may have to wait on the music bot, so the harness has to
            # be willing to wait at least as long for our answer.
            "local_timeout": LOCAL_TIMEOUT,
        }
        if self.model:
            fields["model"] = self.model
        await self.hh.command(**fields)
        self.store.set_root(chat_id, root)
        log.info("chat %s now starts conversations at %s", chat_id, root)
        return root

    async def _fork(self, message: Incoming, parent: str) -> tuple[str, str]:
        block = new_agent_id("tg")
        prompt = build_prompt(message, self.identity)
        fields = {
            "command": "fork",
            "id": parent,
            "prompt": prompt,
            "new_id": block,
            # The pictures of what the agent is being asked about — the message
            # itself and whatever it quotes — so it can look at them.
            "images": self._images(message),
            # Every fork carries the tools afresh, not just the root: a conversation
            # continuing an older chain still gets the definitions this bot offers
            # today, descriptions included.
            "local_tools": LOCAL_TOOLS,
            "local_timeout": LOCAL_TIMEOUT,
        }
        if not self._takes_images():
            # a server that predates them would ignore the field, so do not send it
            del fields["images"]
        try:
            await self.hh.command(**fields)
            return block, parent
        except HarnessError as error:
            if error.code != "unknown_agent":
                raise
            # The chain is gone — evicted, or the harness was pointed at another
            # database. The conversation cannot be continued, so start a fresh one
            # rather than leave the message unanswered.
            log.warning("the harness no longer knows %s; opening a new conversation", parent)
            fields["id"] = await self._chat_root(message.chat_id, refresh=True)
            await self.hh.command(**fields)
            return block, fields["id"]

    # --- running the turn ----------------------------------------------------
    async def _turn(self, message: Incoming, block: str, parent: str) -> None:
        turn = _Turn(
            message=message,
            block=block,
            parent=parent,
            status=TurnStatus(),
            tracker=StatusMessage(
                self.delivery,
                message.chat_id,
                message.message_id,
                self.status_interval,
                store=self.store,
                block=block,
            ),
            started=time.monotonic(),
        )
        status = turn.status
        failure: str | None = None
        self._turns[block] = turn
        subscription = self.hh.subscribe(f"agent:{block}")
        try:
            rid = self.hh.next_rid()
            subscription.key(f"rid:{rid}")
            await self.hh.send(command="run", rid=rid, id=block)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.turn_timeout
            while True:
                try:
                    event = await subscription.get(max(1.0, deadline - loop.time()))
                except asyncio.TimeoutError:
                    failure = f"the agent did not finish within {self.turn_timeout:.0f}s"
                    break
                name = event.get("event")

                if name == CONNECTION_LOST:
                    failure = "the connection to the agent harness was lost"
                    break
                if name in ("run_accepted", "turn_started"):
                    status.phase = "thinking"
                    await turn.tracker.update(status)
                elif name == "reasoning_delta":
                    status.reasoning += event.get("chars") or len(event.get("text") or "")
                    await turn.tracker.update(status)
                elif name == "content_delta":
                    status.content += event.get("chars") or len(event.get("text") or "")
                    await turn.tracker.update(status)
                elif name in ("tool_call_requested", "tool_call_started"):
                    status.tool(event.get("name", "?")).state = (
                        "queued" if name == "tool_call_requested" else "running"
                    )
                    await turn.tracker.update(status)
                elif name == "tool_call_finished":
                    tool = status.tool(event.get("name", "?"))
                    tool.state = "ok" if event.get("ok") else "failed"
                    tool.detail = event.get("error") or ""
                    await turn.tracker.update(status)
                elif name == "local_tool_called":
                    tool = status.tool(event.get("name", "?"))
                    tool.state, tool.detail = "running", "sending"
                    await turn.tracker.update(status, force=True)
                    await self._local_call(turn, event)
                elif name == "local_tool_rollback":
                    await self._rollback(turn, event)
                elif name == "turn_finished":
                    status.phase = "done"
                    await turn.tracker.update(status)
                elif name == "turn_failed":
                    status.phase = "failed"
                    failure = _failure_reason(event)
                    status.error = _short(failure)
                    await turn.tracker.update(status, force=True)
                elif name == "turn_cancelled":
                    status.phase = "cancelled"
                elif name == "error":
                    status.phase = "failed"
                    failure = _short(f"{event.get('code')}: {event.get('message')}", 800)
                    status.error = _short(failure)
                elif name == "command_finished" and event.get("rid") == rid:
                    outcome = event.get("status")
                    if outcome == "cancelled":
                        status.phase = "cancelled"
                    elif outcome != "ok" and failure is None:
                        failure = _short(
                            event.get("error") or f"the agent ended with status {outcome!r}", 800
                        )
                    break
        finally:
            subscription.close()
            self._turns.pop(block, None)

        if failure is not None:
            status.phase = "failed"
            status.error = status.error or _short(failure)
        elif status.phase == "done" and not turn.delivered:
            status.note = "no reply was drafted"
        await turn.tracker.update(status, force=True)

        if failure is not None:
            await self._failure(message, block, failure)

    # --- the local tools -----------------------------------------------------
    async def _local_call(self, turn: _Turn, event: dict) -> None:
        """A parked local tool call — this one is ours to run."""
        agent_id = event.get("agent_id") or turn.block
        call_id = event.get("call_id")
        name = event.get("name")

        if name == TOOL_NAME:
            await self._draft(turn, event, agent_id, call_id)
        elif name == SEARCH_TOOL_NAME:
            await self._search_music(turn, event, agent_id, call_id)
        elif name == SEND_TOOL_NAME:
            await self._send_music(turn, event, agent_id, call_id)
        elif name == PARSE_TOOL_NAME:
            await self._send_parsed(turn, event, agent_id, call_id)
        elif name == DOWNLOAD_TOOL_NAME:
            await self._download_to_sandbox(event, agent_id, call_id)
        elif name == SEND_FROM_SANDBOX_TOOL_NAME:
            await self._send_from_sandbox(event, agent_id, call_id)
        elif name == SEND_FILE_TOOL_NAME:
            await self._send_file(turn, event, agent_id, call_id)
        elif name == CLONE_TOOL_NAME:
            await self._clone_to_sandbox(event, agent_id, call_id)
        elif name == CURL_TOOL_NAME:
            await self._curl_to_sandbox(event, agent_id, call_id)
        elif name == VIEW_CURRENT_TOOL_NAME:
            await self._read_current(turn, agent_id, call_id)
        elif name == VIEW_PUBLIC_TOOL_NAME:
            await self._read_public(event, agent_id, call_id)
        elif name == READ_TOOL_NAME:
            await self._read_one(turn, event, agent_id, call_id)
        elif name == FORWARD_TOOL_NAME:
            await self._forward_one(turn, event, agent_id, call_id)
        elif name == ADD_SCHEDULE_TOOL_NAME:
            await self._add_schedule(turn, event, agent_id, call_id)
        elif name == VIEW_SCHEDULE_TOOL_NAME:
            await self._view_schedules(agent_id, call_id)
        elif name == REMOVE_SCHEDULE_TOOL_NAME:
            await self._remove_schedule(event, agent_id, call_id)
        else:
            await self.hh.resolve_tool(agent_id, call_id, error=f"unknown local tool: {name}")

    async def _draft(self, turn: _Turn, event: dict, agent_id: str, call_id: str) -> None:
        if turn.delivered:
            await self.hh.resolve_tool(
                agent_id,
                call_id,
                error="the reply was already delivered; call this tool only once",
            )
            return

        arguments = event.get("arguments") or {}
        summary, details = arguments.get("summary"), arguments.get("details")
        if not isinstance(details, str) or not details.strip():
            await self.hh.resolve_tool(
                agent_id,
                call_id,
                error="tg_draft_response needs `summary` and `details` as non-empty strings",
            )
            return

        parts = build_reply_parts(summary if isinstance(summary, str) else "", details)
        try:
            for index, part in enumerate(parts):
                turn.sent.append(
                    await self.delivery.send(
                        turn.message.chat_id,
                        part.text,
                        entities=part.entities,
                        reply_to=turn.message.message_id if index == 0 else None,
                    )
                )
        except Exception as error:
            log.warning("Telegram refused the reply for %s: %s", turn.message.message_id, error)
            self._remember(turn)
            turn.effects[call_id] = list(turn.sent)
            await self.hh.resolve_tool(
                agent_id, call_id, error=f"Telegram refused the messages: {error}"
            )
            return

        self._remember(turn)
        turn.delivered = True
        turn.effects[call_id] = list(turn.sent)
        tool = turn.status.tool(TOOL_NAME)
        tool.state, tool.detail = "ok", f"{len(turn.sent)} sent"
        await turn.tracker.delete()
        await self.hh.resolve_tool(
            agent_id,
            call_id,
            result=f"delivered: the reply went out as {len(turn.sent)} Telegram message(s)",
        )

    async def _search_music(self, turn: _Turn, event: dict, agent_id: str, call_id: str) -> None:
        platform, keyword, complaint = _music_arguments(event.get("arguments") or {}, "keyword")
        if complaint is not None:
            await self.hh.resolve_tool(agent_id, call_id, error=complaint)
            return
        log.info("searching %s on %s for chat %s", keyword, platform, turn.message.chat_id)
        answer = await search_music(self.delivery, keyword, platform, gate=self.music_gate)
        await self._answer(agent_id, call_id, answer)

    async def _send_music(self, turn: _Turn, event: dict, agent_id: str, call_id: str) -> None:
        platform, url, complaint = _music_arguments(event.get("arguments") or {}, "url")
        if complaint is not None:
            await self.hh.resolve_tool(agent_id, call_id, error=complaint)
            return
        log.info("sending %s (%s) to chat %s", url, platform, turn.message.chat_id)
        answer = await send_music(
            self.delivery, url, platform, turn.message.chat_id, gate=self.music_gate
        )
        self._forwarded(turn, call_id, answer)
        await self._answer(agent_id, call_id, answer)

    async def _send_parsed(self, turn: _Turn, event: dict, agent_id: str, call_id: str) -> None:
        url, complaint = text_argument(event.get("arguments") or {}, "url")
        if complaint is not None:
            await self.hh.resolve_tool(agent_id, call_id, error=complaint)
            return
        log.info("parsing %s for chat %s", url, turn.message.chat_id)
        answer = await send_parsed(self.delivery, url, turn.message.chat_id)
        self._forwarded(turn, call_id, answer)
        await self._answer(agent_id, call_id, answer)

    async def _download_to_sandbox(self, event: dict, agent_id: str, call_id: str) -> None:
        """A message's file, fetched here and piped into the sandbox as bytes."""
        if not self._takes_pipes():
            await self.hh.resolve_tool(agent_id, call_id, error=self._pipe_refusal())
            return
        arguments, complaint = download_arguments(event.get("arguments") or {})
        if complaint is not None:
            await self.hh.resolve_tool(agent_id, call_id, error=complaint)
            return
        log.info(
            "downloading message %s of chat %s into sandbox %s at %s",
            arguments["message_id"],
            arguments["chat_id"],
            arguments["sandbox_id"],
            arguments["path"],
        )
        await self._answer(agent_id, call_id, await download_to_sandbox(self.delivery, **arguments))

    async def _send_from_sandbox(self, event: dict, agent_id: str, call_id: str) -> None:
        """A sandbox file, handed to nix_cat_file so it can reach the chat."""
        if not self._takes_pipes():
            await self.hh.resolve_tool(
                agent_id, call_id, error=self._pipe_refusal("a file out of a sandbox")
            )
            return
        arguments, complaint = export_arguments(event.get("arguments") or {})
        if complaint is not None:
            await self.hh.resolve_tool(agent_id, call_id, error=complaint)
            return
        log.info("sending %s out of sandbox %s", arguments["path"], arguments["sandbox_id"])
        await self._answer(agent_id, call_id, send_from_sandbox(**arguments))

    async def _send_file(self, turn: _Turn, event: dict, agent_id: str, call_id: str) -> None:
        """A file's bytes, posted into this chat under the name its path ends with."""
        arguments, complaint = send_arguments(event.get("arguments") or {})
        if complaint is not None:
            await self.hh.resolve_tool(agent_id, call_id, error=complaint)
            return
        log.info("sending %s into chat %s", arguments["path"], turn.message.chat_id)
        answer = await send_file_to_chat(self.delivery, turn.message.chat_id, **arguments)
        self._forwarded(turn, call_id, answer)
        await self._answer(agent_id, call_id, answer)

    async def _clone_to_sandbox(self, event: dict, agent_id: str, call_id: str) -> None:
        """A repository, cloned here and piped into the sandbox as one archive."""
        if not self._takes_pipes():
            await self.hh.resolve_tool(agent_id, call_id, error=self._pipe_refusal())
            return
        arguments, complaint = clone_arguments(event.get("arguments") or {})
        if complaint is not None:
            await self.hh.resolve_tool(agent_id, call_id, error=complaint)
            return
        log.info(
            "cloning %s into sandbox %s at %s (%s commits deep)",
            arguments["url"],
            arguments["sandbox_id"],
            arguments["path"],
            arguments["depth"] or "all",
        )
        await self._answer(agent_id, call_id, await clone_to_sandbox(self.transfer, **arguments))

    async def _curl_to_sandbox(self, event: dict, agent_id: str, call_id: str) -> None:
        """A URL, fetched here and piped into the sandbox as its own file."""
        if not self._takes_pipes():
            await self.hh.resolve_tool(agent_id, call_id, error=self._pipe_refusal())
            return
        arguments, complaint = fetch_arguments(event.get("arguments") or {})
        if complaint is not None:
            await self.hh.resolve_tool(agent_id, call_id, error=complaint)
            return
        log.info(
            "fetching %s into sandbox %s at %s",
            arguments["url"],
            arguments["sandbox_id"],
            arguments["path"],
        )
        await self._answer(agent_id, call_id, await curl_to_sandbox(self.transfer, **arguments))

    def _forwarded(self, turn: _Turn, call_id: str, answer) -> None:
        """A message a tool put into the chat is the userbot's, like any other."""
        if answer.forwarded is None:
            return
        turn.effects[call_id] = [answer.forwarded]
        # replying to what a bot sent continues this very conversation
        self.store.record(turn.message.chat_id, answer.forwarded, turn.block)

    async def _read_current(self, turn: _Turn, agent_id: str, call_id: str) -> None:
        """The last messages of the chat this conversation is happening in."""
        answer = await view_current_chat(self.delivery, turn.message.chat_id)
        await self._answer(agent_id, call_id, answer)

    async def _read_public(self, event: dict, agent_id: str, call_id: str) -> None:
        """The last messages of a public chat, named by its username."""
        username, complaint = text_argument(event.get("arguments") or {}, "username")
        if complaint is not None:
            await self.hh.resolve_tool(agent_id, call_id, error=complaint)
            return
        answer = await view_public_chat(self.delivery, username)
        await self._answer(agent_id, call_id, answer)

    async def _read_one(self, turn: _Turn, event: dict, agent_id: str, call_id: str) -> None:
        """One message, by its id — of this chat or of one the model named."""
        message_id, source, complaint = _one_message_arguments(event)
        if complaint is not None:
            await self.hh.resolve_tool(agent_id, call_id, error=complaint)
            return
        answer = await read_one_message(self.delivery, turn.message.chat_id, message_id, source)
        await self._answer(agent_id, call_id, answer)

    async def _forward_one(self, turn: _Turn, event: dict, agent_id: str, call_id: str) -> None:
        """One message, put back at the end of this chat."""
        message_id, source, complaint = _one_message_arguments(event)
        if complaint is not None:
            await self.hh.resolve_tool(agent_id, call_id, error=complaint)
            return
        answer = await forward_one_message(self.delivery, turn.message.chat_id, message_id, source)
        self._forwarded(turn, call_id, answer)
        await self._answer(agent_id, call_id, answer)

    async def _add_schedule(self, turn: _Turn, event: dict, agent_id: str, call_id: str) -> None:
        """A task for this chat, to run later and answer here."""
        content, due_at, complaint = schedule_arguments(event.get("arguments") or {})
        if complaint is not None:
            await self.hh.resolve_tool(agent_id, call_id, error=complaint)
            return
        answer = add_task(
            self.store,
            content,
            due_at,
            turn.message.chat_id,
            turn.message.message_id,
            turn.block,
        )
        if answer.scheduled is not None:
            turn.scheduled[call_id] = answer.scheduled
        await self._answer(agent_id, call_id, answer)

    async def _view_schedules(self, agent_id: str, call_id: str) -> None:
        """Everything that is waiting for its time, soonest first."""
        await self._answer(agent_id, call_id, view_tasks(self.store))

    async def _remove_schedule(self, event: dict, agent_id: str, call_id: str) -> None:
        """One task, taken off the list by the id the view printed."""
        task_id, complaint = text_argument(event.get("arguments") or {}, "id")
        if complaint is not None:
            await self.hh.resolve_tool(agent_id, call_id, error=complaint)
            return
        await self._answer(agent_id, call_id, remove_task(self.store, task_id))

    async def _undo_schedule(self, turn: _Turn, agent_id: str, call_id: str, original: str) -> None:
        """A failed turn takes its task back: it will not fire."""
        task_id = turn.scheduled.pop(original, None)
        if task_id is None or not self.store.remove_schedule(task_id):
            await self.hh.resolve_tool(agent_id, call_id, result="nothing to undo")
            return
        log.info("the failed turn cancelled the scheduled task %s", task_id)
        await self.hh.resolve_tool(agent_id, call_id, result=f"cancelled the task {task_id}")

    def _takes_images(self) -> bool:
        """Whether the harness we are talking to knows about images at all."""
        protocol = (self.hh.hello or {}).get("protocol")
        return isinstance(protocol, int) and protocol >= IMAGE_PROTOCOL

    def _takes_pipes(self) -> bool:
        """Whether the harness we are talking to can run a tool our answer names."""
        protocol = (self.hh.hello or {}).get("protocol")
        return isinstance(protocol, int) and protocol >= PIPE_PROTOCOL

    def _pipe_refusal(self, carrying: str = "a file to a sandbox") -> str:
        """What to tell the model when the harness is too old to carry a pipe."""
        return (
            f"this harness cannot hand {carrying}: tool pipes arrived in "
            f"protocol 4, and it speaks {(self.hh.hello or {}).get('protocol')}"
        )

    def _images(self, message: Incoming) -> list[str]:
        """The pictures to send with a fork: what this is about, and what it quotes."""
        images = list(message.images)
        if message.quoted is not None:
            images = list(message.quoted.images) + images
        if len(images) > MAX_IMAGES:
            log.info("sending only the first %d pictures", MAX_IMAGES)
            images = images[:MAX_IMAGES]
        return images

    async def _answer(self, agent_id: str, call_id: str, answer) -> None:
        """Tell the model what a tool did, or why it could not.

        An answer that names the tool to run next does not reach the model at
        all: the harness runs that one, and what the model reads is its output.
        """
        if answer.error:
            log.warning("%s failed: %s", call_id, answer.error)
            await self.hh.resolve_tool(agent_id, call_id, error=answer.error)
        elif answer.call is not None:
            await self.hh.resolve_tool(agent_id, call_id, result=answer.result, call=answer.call)
        else:
            await self.hh.resolve_tool(
                agent_id, call_id, result=answer.result, images=answer.images
            )

    async def _rollback(self, turn: _Turn, event: dict) -> None:
        """The harness is undoing the turn — take what it sent back with it."""
        agent_id = event.get("agent_id") or turn.block
        call_id = event.get("call_id")
        name = event.get("name")
        original = event.get("rollback_of") or call_id

        if name not in UNDOABLE:
            await self.hh.resolve_tool(agent_id, call_id, error=f"nothing to undo for {name}")
            return
        if name == ADD_SCHEDULE_TOOL_NAME:
            await self._undo_schedule(turn, agent_id, call_id, original)
            return
        messages = turn.effects.pop(original, [])
        if name == TOOL_NAME:
            turn.delivered = False
            turn.sent = []
        if not messages:
            await self.hh.resolve_tool(agent_id, call_id, result="nothing to undo")
            return
        try:
            await self.delivery.delete(turn.message.chat_id, messages)
        except Exception as error:
            log.warning("could not delete the rolled-back messages: %s", error)
            await self.hh.resolve_tool(agent_id, call_id, error=f"could not delete: {error}")
            return
        self.store.forget(turn.message.chat_id, messages)
        await self.hh.resolve_tool(agent_id, call_id, result=f"deleted {len(messages)} message(s)")

    # --- messages of our own -------------------------------------------------
    def _remember(self, turn: _Turn) -> None:
        """Every message we sent points back at the block that produced it."""
        self.store.record_many(
            [(turn.message.chat_id, message_id, turn.block) for message_id in turn.sent]
        )

    async def _failure(self, message: Incoming, block: str | None, text: str) -> None:
        body = clamp_units(f"❌ agent failed\n\n{text}", MAX_UNITS)
        try:
            message_id = await self.delivery.send(
                message.chat_id, body, reply_to=message.message_id
            )
        except Exception as error:
            log.warning("could not report the failure in chat %s: %s", message.chat_id, error)
            return
        if block is not None:
            self.store.record(message.chat_id, message_id, block)

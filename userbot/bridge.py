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
from dataclasses import dataclass, field

from .content import Content, Quoted
from .harness import CONNECTION_LOST, TG_DRAFT_TOOL, HarnessError, new_agent_id
from .render import MAX_UNITS, build_reply_parts, clamp_units
from .status import StatusMessage, TurnStatus

log = logging.getLogger(__name__)

TOOL_NAME = TG_DRAFT_TOOL["name"]


@dataclass
class Incoming:
    """One Telegram message, normalised so the bridge needs no Telethon."""

    chat_id: int
    message_id: int
    content: Content
    sender_id: int
    sender_name: str
    sender_username: str | None = None
    chat_title: str | None = None
    chat_username: str | None = None
    is_group: bool = False
    mentioned: bool = False
    reply_to_message_id: int | None = None
    quoted: Quoted | None = None


@dataclass
class Identity:
    """The userbot account itself — whose voice the agent is writing in."""

    name: str = ""
    username: str | None = None
    user_id: int = 0


def _who(name: str, username: str | None, user_id: int) -> str:
    bits = [f"user id {user_id}"]
    if username:
        bits.insert(0, f"@{username}")
    return f"{name or 'unknown'} ({', '.join(bits)})"


def build_prompt(message: Incoming, identity: Identity | None = None) -> str:
    """The envelope the agent sees: who it writes as, who wrote what, and how to answer."""
    header = ["[telegram]"]
    if identity is not None:
        header.append(
            "you are replying on behalf of the userbot "
            f"{_who(identity.name, identity.username, identity.user_id)}"
        )
    header.append(f"from: {_who(message.sender_name, message.sender_username, message.sender_id)}")
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
    return (
        "\n".join(header)
        + "\nmessage:\n"
        + message.content.render()
        + "\n[/telegram]\n"
        + "Answer in the language of the sender's message above — in English if that "
        + "language cannot be told — and deliver it by calling "
        + f"{TOOL_NAME} (a short reply in `summary`, the full answer in `details`, "
        + "which the sender reads too) as the last thing you do."
    )


def _short(text, limit: int = 200) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


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
    status: TurnStatus
    tracker: StatusMessage
    sent: list[int] = field(default_factory=list)
    delivered: bool = False


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
    ) -> None:
        self.hh = harness
        self.store = store
        self.delivery = delivery
        self.status_interval = status_interval
        self.turn_timeout = turn_timeout
        self.model = model
        self.identity = identity

    # --- entry point --------------------------------------------------------
    async def handle(self, message: Incoming) -> None:
        """Answer one message, if it is addressed to us. Never raises."""
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
            block = await self._fork(message, parent)
            await self._turn(message, block)
        except HarnessError as error:
            log.warning("the harness refused the turn for %s: %s", message.message_id, error)
            await self._failure(message, block, str(error))
        except Exception as error:
            log.exception("the turn for %s broke", message.message_id)
            await self._failure(message, block, f"internal error: {error}")

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
        fields = {"command": "create_agent", "id": root, "local_tools": [TG_DRAFT_TOOL]}
        if self.model:
            fields["model"] = self.model
        await self.hh.command(**fields)
        self.store.set_root(chat_id, root)
        log.info("chat %s now starts conversations at %s", chat_id, root)
        return root

    async def _fork(self, message: Incoming, parent: str) -> str:
        block = new_agent_id("tg")
        prompt = build_prompt(message, self.identity)
        try:
            await self.hh.command(command="fork", id=parent, prompt=prompt, new_id=block)
            return block
        except HarnessError as error:
            if error.code != "unknown_agent":
                raise
            # The chain is gone — evicted, or the harness was pointed at another
            # database. The conversation cannot be continued, so start a fresh one
            # rather than leave the message unanswered.
            log.warning("the harness no longer knows %s; opening a new conversation", parent)
            parent = await self._chat_root(message.chat_id, refresh=True)
            await self.hh.command(command="fork", id=parent, prompt=prompt, new_id=block)
            return block

    # --- running the turn ----------------------------------------------------
    async def _turn(self, message: Incoming, block: str) -> None:
        turn = _Turn(
            message=message,
            block=block,
            status=TurnStatus(),
            tracker=StatusMessage(
                self.delivery, message.chat_id, message.message_id, self.status_interval
            ),
        )
        status = turn.status
        failure: str | None = None
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

        if failure is not None:
            status.phase = "failed"
            status.error = status.error or _short(failure)
        elif status.phase == "done" and not turn.delivered:
            status.note = "no reply was drafted"
        await turn.tracker.update(status, force=True)

        if failure is not None:
            await self._failure(message, block, failure)

    # --- the local tool ------------------------------------------------------
    async def _local_call(self, turn: _Turn, event: dict) -> None:
        """A parked local tool call — this one is ours to run."""
        agent_id = event.get("agent_id") or turn.block
        call_id = event.get("call_id")
        name = event.get("name")

        if name != TOOL_NAME:
            await self.hh.resolve_tool(agent_id, call_id, error=f"unknown local tool: {name}")
            return
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
            await self.hh.resolve_tool(
                agent_id, call_id, error=f"Telegram refused the messages: {error}"
            )
            return

        self._remember(turn)
        turn.delivered = True
        tool = turn.status.tool(TOOL_NAME)
        tool.state, tool.detail = "ok", f"{len(turn.sent)} sent"
        await turn.tracker.delete()
        await self.hh.resolve_tool(
            agent_id,
            call_id,
            result=f"delivered: the reply went out as {len(turn.sent)} Telegram message(s)",
        )

    async def _rollback(self, turn: _Turn, event: dict) -> None:
        """The harness is undoing the turn — take the reply back with it."""
        agent_id = event.get("agent_id") or turn.block
        call_id = event.get("call_id")
        name = event.get("name")

        if name != TOOL_NAME:
            await self.hh.resolve_tool(agent_id, call_id, error=f"nothing to undo for {name}")
            return
        if not turn.sent:
            await self.hh.resolve_tool(agent_id, call_id, result="no reply had been sent")
            return
        sent, turn.sent = turn.sent, []
        turn.delivered = False
        try:
            await self.delivery.delete(turn.message.chat_id, sent)
        except Exception as error:
            log.warning("could not delete the rolled-back reply: %s", error)
            await self.hh.resolve_tool(agent_id, call_id, error=f"could not delete: {error}")
            return
        self.store.forget(turn.message.chat_id, sent)
        await self.hh.resolve_tool(agent_id, call_id, result=f"deleted {len(sent)} message(s)")

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

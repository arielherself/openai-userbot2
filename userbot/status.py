"""The one Telegram message that follows a turn while it runs.

The user asked something, so they get an answer to their message straight away:
a status message that is edited in place as the agent thinks, calls tools and
finally finishes. Editing is rate-limited — Telegram counts edits like sends,
and a stream of deltas would otherwise turn into a flood — so an update that
arrives too soon after the last is dropped, and the end of the turn is always
published.

What it says is deliberately bare — `🧠 thinking`, `🔧 calling web_search`,
`✅ done` — rather than a sentence addressed to the reader: it is a trace of the
turn, not a reply. The thinking itself is never repeated here, only how much of
it there has been; a tool call is named, never its arguments or its result.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

THINKING = "🧠 thinking"

HEADLINES = {
    "starting": THINKING,
    "thinking": THINKING,
    "done": "✅ done",
    "failed": "❌ failed",
    "cancelled": "⏹️ cancelled",
}

# How many tool lines to keep in view.
TOOL_LINES = 6


@dataclass
class ToolProgress:
    name: str
    state: str = "queued"
    detail: str = ""


def tool_line(tool: ToolProgress) -> str:
    """The tool being called right now, or how the last call of it went."""
    if tool.state not in ("ok", "failed"):
        return f"🔧 calling {tool.name}"
    if tool.state == "ok":
        return f"✅ {tool.name}"
    error = " ".join((tool.detail or "").split())
    if len(error) > 80:
        error = error[:79] + "…"
    return f"❌ {tool.name} failed" + (f" ({error})" if error else "")


@dataclass
class TurnStatus:
    """What the status message shows, accumulated from the turn's events."""

    phase: str = "starting"
    reasoning: int = 0
    content: int = 0
    error: str = ""
    note: str = ""
    tools: list[ToolProgress] = field(default_factory=list)

    def tool(self, name: str) -> ToolProgress:
        for tool in self.tools:
            if tool.name == name:
                return tool
        tool = ToolProgress(name)
        self.tools.append(tool)
        return tool

    @property
    def active(self) -> bool:
        """Whether the agent has produced anything yet.

        Nothing is shown before this is true: a turn that has only been accepted,
        or is still waiting on the provider, has nothing to report.
        """
        return bool(self.reasoning or self.content or self.tools)

    def detail(self) -> str:
        """The same facts as `render`, with nothing clipped — for `/inspect`."""
        lines = [self.render()]
        for tool in self.tools:
            if tool.state == "failed" and tool.detail:
                lines.append(f"❌ {tool.name}: {tool.detail}")
        return "\n".join(lines)

    def render(self) -> str:
        headline = HEADLINES.get(self.phase, THINKING)
        body = []
        if self.reasoning:
            # While the agent is thinking, its count is what the headline is about,
            # so it goes on that line; once the turn has an outcome of its own, the
            # count becomes a line of its own instead.
            thinking = f"{THINKING} · {self.reasoning:,} chars"
            if headline == THINKING:
                headline = thinking
            else:
                body.append(thinking)
        body.extend(tool_line(tool) for tool in self.tools[-TOOL_LINES:])
        if self.content:
            body.append(f"✍️ drafting · {self.content:,} chars")
        if self.phase == "failed" and self.error:
            body.append(f"⚠️ error · {self.error}")
        if self.note and self.phase in ("done", "failed", "cancelled"):
            headline += f" ({self.note})"
        return "\n".join([headline, *body])


class StatusMessage:
    """Posts the status once the first thing worth showing happens, then edits it.

    While it stands, the message is recorded against the block it is tracking, so
    that a reply to it can ask what the turn is doing (`/inspect`). That mapping
    is temporary like the message: deleting the message drops it.
    """

    def __init__(
        self,
        delivery,
        chat_id: int,
        reply_to: int,
        min_interval: float = 2.0,
        store=None,
        block: str | None = None,
    ) -> None:
        self.delivery = delivery
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.min_interval = min_interval
        self.store = store
        self.block = block
        self.message_id: int | None = None
        self.deleted = False
        self._text: str | None = None
        self._at = 0.0

    @property
    def posted(self) -> bool:
        return self.message_id is not None and not self.deleted

    async def update(self, status: TurnStatus, force: bool = False) -> None:
        if self.deleted:
            return
        if self.message_id is None and not status.active:
            return  # the agent has not produced anything yet: nothing to show
        text = status.render()
        if text == self._text:
            return
        if self.message_id is None:
            try:
                self.message_id = await self.delivery.send(
                    self.chat_id, text, reply_to=self.reply_to
                )
            except Exception as error:
                log.warning("could not post the status message: %s", error)
                self.deleted = True  # do not keep retrying into a failing chat
                return
            self._remember()
            self._text, self._at = text, time.monotonic()
            return
        now = time.monotonic()
        if not force and now - self._at < self.min_interval:
            return
        try:
            await self.delivery.edit(self.chat_id, self.message_id, text)
        except Exception as error:
            log.warning("could not update the status message: %s", error)
            self.deleted = True
            return
        self._text, self._at = text, now

    def _remember(self) -> None:
        """Point the tracking message at the block whose turn it is tracking."""
        if self.store is not None and self.block and self.message_id is not None:
            self.store.record(self.chat_id, self.message_id, self.block)

    async def delete(self) -> None:
        """Take the status message down — the reply itself is the answer now."""
        message_id, self.message_id = self.message_id, None
        self.deleted = True
        if message_id is None:
            return
        if self.store is not None:
            self.store.forget(self.chat_id, [message_id])
        try:
            await self.delivery.delete(self.chat_id, [message_id])
        except Exception as error:
            log.warning("could not delete the status message: %s", error)

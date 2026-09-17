"""Pieces the tests share: a scripted harness server and a fake Telegram."""

from __future__ import annotations

import asyncio
import json

from userbot.telegram import BotMessage

DEFAULTS = {"endpoint": "https://provider.invalid/v1", "model": "fake-model", "has_key": True}


class Conn:
    """One client connection to the fake server, and how to write to it."""

    def __init__(self, writer) -> None:
        self.writer = writer
        self._seq = 0

    async def emit(self, **event) -> None:
        self._seq += 1
        event.setdefault("seq", self._seq)
        event.setdefault("ts", 0.0)
        self.writer.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.writer.drain()


class FakeHarness:
    """Speaks the harness protocol well enough to drive the real client.

    `script(harness, conn, command)` is called for every command and writes
    whatever that command's events should be.
    """

    def __init__(self, script, defaults=None) -> None:
        self.script = script
        self.defaults = dict(defaults or DEFAULTS)
        self.commands: list[dict] = []
        self.connections: list[Conn] = []
        self.host = "127.0.0.1"
        self.port = 0
        self._server = None
        self._writers: list = []

    async def start(self) -> FakeHarness:
        self._server = await asyncio.start_server(self._accept, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        # Close the connections first: since 3.12 `wait_closed` waits for the
        # handlers too, and a handler is parked on a read until we hang up.
        for writer in self._writers:
            writer.close()
        self._writers.clear()
        self._server.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0)

    async def _accept(self, reader, writer) -> None:
        conn = Conn(writer)
        self.connections.append(conn)
        self._writers.append(writer)
        await conn.emit(
            event="session_hello",
            protocol=2,
            server="fake",
            peer="",
            started_at=0,
            defaults=self.defaults,
            commands=[],
            tools=[],
            store=None,
        )
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                command = json.loads(line.decode("utf-8"))
                self.commands.append(command)
                await self.script(self, conn, command)
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    # --- assertions helpers ------------------------------------------------
    def commands_named(self, name: str) -> list[dict]:
        return [command for command in self.commands if command.get("command") == name]

    async def wait_for_command(self, name: str, timeout: float = 2.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            found = self.commands_named(name)
            if found:
                return found[-1]
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"no {name} command arrived")
            await asyncio.sleep(0.01)


class FakeTelegram:
    """A `Delivery` that remembers every call instead of sending anything."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.deleted: list[tuple[int, tuple[int, ...]]] = []
        self.forwarded: list[dict] = []
        self.fail_send = False
        self.fail_forward = False
        # What the "other side" sends back: a chat the tools listen to gets these.
        self.inbox: list[BotMessage] = []
        # Called with (chat_id, text) after a send, so a test can script the reply
        # the way a real bot would produce it.
        self.on_send = None
        self._next_id = 1000
        self._listeners: list[tuple] = []

    async def send(self, chat_id, text, entities=None, reply_to=None) -> int:
        if self.fail_send:
            raise RuntimeError("telegram said no")
        self._next_id += 1
        self.sent.append(
            {
                "id": self._next_id,
                "chat_id": chat_id,
                "text": text,
                "entities": list(entities or []),
                "reply_to": reply_to,
            }
        )
        if self.on_send is not None:
            self.on_send(chat_id, text)
        return self._next_id

    async def edit(self, chat_id, message_id, text) -> None:
        self.edits.append({"chat_id": chat_id, "id": message_id, "text": text})

    async def delete(self, chat_id, message_ids) -> None:
        self.deleted.append((chat_id, tuple(message_ids)))

    def listen(self, chat_id) -> _FakeListener:
        # Like the real one: watching starts before the command goes out, and it
        # hands over everything the chat says from then on.
        self._listeners.append(chat_id)
        return _FakeListener(self, chat_id)

    async def forward(self, chat_id, message_id, to_chat_id) -> int:
        if self.fail_forward:
            raise RuntimeError("telegram said no")
        self._next_id += 1
        self.forwarded.append(
            {"id": self._next_id, "from": chat_id, "message_id": message_id, "to": to_chat_id}
        )
        return self._next_id

    # --- questions the tests ask --------------------------------------------
    def live(self) -> list[dict]:
        """Messages that have not been deleted, in the order they went out."""
        gone = {message_id for _, ids in self.deleted for message_id in ids}
        return [message for message in self.sent if message["id"] not in gone]

    def of(self, message_id: int) -> dict:
        return next(message for message in self.sent if message["id"] == message_id)


class _FakeListener:
    """The fake's `listen`: the chat's messages, oldest first."""

    def __init__(self, delivery, chat_id) -> None:
        self.delivery = delivery
        self.chat_id = chat_id
        self.closed = False

    async def next(self, timeout: float):
        for index, message in enumerate(self.delivery.inbox):
            if message.chat_id == self.chat_id:
                del self.delivery.inbox[index]
                return message
        return None

    def close(self) -> None:
        self.closed = True


def bot_message(
    id=1,
    chat_id=-500,
    text="",
    markdown="",
    reply_to=None,
    has_link=False,
    has_buttons=False,
    has_music=False,
) -> BotMessage:
    return BotMessage(
        id=id,
        chat_id=chat_id,
        text=text,
        markdown=markdown or text,  # what a bot writes is markdown; plain is the fallback
        reply_to=reply_to,
        has_link=has_link,
        has_buttons=has_buttons,
        has_music=has_music,
    )


def text_of(sent) -> str:
    """The blockquoted body of a rendered part — or of a sent message."""
    from telethon.tl.types import MessageEntityBlockquote

    text, entities = _text_and_entities(sent)
    quote = next(e for e in entities if isinstance(e, MessageEntityBlockquote))
    return _slice(text, quote.offset, quote.length)


def bold_of(sent) -> list[str]:
    from telethon.tl.types import MessageEntityBold

    text, entities = _text_and_entities(sent)
    return [
        _slice(text, entity.offset, entity.length)
        for entity in entities
        if isinstance(entity, MessageEntityBold)
    ]


def quotes_in(sent) -> list:
    from telethon.tl.types import MessageEntityBlockquote

    return [e for e in _text_and_entities(sent)[1] if isinstance(e, MessageEntityBlockquote)]


def _text_and_entities(sent):
    if isinstance(sent, dict):
        return sent["text"], sent["entities"]
    return sent.text, sent.entities


def _slice(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2 : (offset + length) * 2].decode("utf-16-le", "ignore")

"""Pieces the tests share: a scripted harness server, a fake Telegram, a server to fetch."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from userbot.content import Content
from userbot.harness import LINE_LIMIT
from userbot.telegram import BotMessage, ChatMessage, ChatView, DownloadedFile

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

    def __init__(self, script, defaults=None, protocol=4) -> None:
        self.script = script
        self.defaults = dict(defaults or DEFAULTS)
        self.protocol = protocol  # 4 is the version that carries images and pipes
        self.commands: list[dict] = []
        self.connections: list[Conn] = []
        self.host = "127.0.0.1"
        self.port = 0
        self._server = None
        self._writers: list = []

    async def start(self) -> FakeHarness:
        # a piped payload is one line, and it can be a file's worth of base64 —
        # the real server reads lines that long, so this one has to as well
        self._server = await asyncio.start_server(self._accept, self.host, 0, limit=LINE_LIMIT)
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
            protocol=self.protocol,
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
        # Documents, as they went out: what the file was called, and its bytes.
        self.files_sent: list[dict] = []
        self.fail_send = False
        self.fail_forward = False
        # What the "other side" sends back: a chat the tools listen to gets these.
        self.inbox: list[BotMessage] = []
        # Called with (chat_id, text) after a send, so a test can script the reply
        # the way a real bot would produce it.
        self.on_send = None
        # When set, reading history waits on it — a way to hold a turn open.
        self.hold: asyncio.Event | None = None
        # What a chat holds, for the tools that read history: id or @username -> view.
        self.chats: dict = {}
        self.fail_history = False
        # What a message carries, for the download tool: (chat_id, message_id) -> bytes.
        self.files: dict[tuple, bytes] = {}
        self.fail_download = False
        self.downloaded: list[tuple] = []
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

    async def send_file(self, chat_id, name, data) -> int:
        if self.fail_send:
            raise RuntimeError("telegram said no")
        self._next_id += 1
        self.files_sent.append(
            {"id": self._next_id, "chat_id": chat_id, "name": name, "data": data}
        )
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

    async def recent_messages(self, chat_id, limit=50) -> ChatView:
        if self.hold is not None:
            await self.hold.wait()
        if self.fail_history:
            raise RuntimeError("telegram said no")
        try:
            view = self.chats[chat_id]
        except KeyError:
            raise ValueError(f"no such chat: {chat_id}") from None
        messages = list(view.messages[-limit:])  # the newest, as Telegram gives them
        return ChatView(
            title=view.title,
            username=view.username,
            chat_id=view.chat_id,
            messages=[await self._with_reply(one, view) for one in messages],
        )

    async def _with_reply(self, message, view):
        """What a message answers, looked up the way the real one looks it up."""
        if message.reply_to is not None or not message.reply_to_id:
            return message
        quoted = next((one for one in view.messages if one.id == message.reply_to_id), None)
        if quoted is None:
            return message
        return replace(message, reply_to=replace(quoted, reply_to=None, reply_to_id=None, quote=""))

    async def message(self, chat_id, message_id, images=False):
        if self.fail_history:
            raise RuntimeError("telegram said no")
        view = self.chats.get(chat_id)
        if view is None:
            raise ValueError(f"no such chat: {chat_id}")
        found = next((one for one in view.messages if one.id == message_id), None)
        if found is None:
            return None
        # the same as the real one: a message brings what it answers
        found = await self._with_reply(found, view)
        if images:
            return found
        return replace(found, images=[])  # pictures only when they were asked for

    async def download(self, chat_id, message_id):
        """The file a message carries, as the real one would hand it over."""
        self.downloaded.append((chat_id, message_id))
        if self.fail_download:
            raise RuntimeError("telegram said no")
        data = self.files.get((chat_id, message_id))
        if data is None:
            return None
        return DownloadedFile(name=f"file-{message_id}.bin", data=data)

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


#: What a message in a test is stamped with, unless the test says otherwise.
SENT_AT = datetime(2026, 9, 18, 4, 12, tzinfo=timezone.utc)


def chat_message(
    id=1,
    name="小明",
    username="ming",
    user_id=7,
    text="",
    media="",
    date=SENT_AT,
    images=None,
    reply_to_id=None,
    reply_to=None,
    quote="",
) -> ChatMessage:
    return ChatMessage(
        id=id,
        sender_name=name,
        sender_username=username,
        sender_id=user_id,
        content=Content(text=text, media=media),
        date=date,
        images=list(images or []),
        reply_to_id=reply_to_id,
        reply_to=reply_to,
        quote=quote,
    )


def chat_view(title="测试群", username="testgroup", chat_id=-100, messages=None) -> ChatView:
    return ChatView(title=title, username=username, chat_id=chat_id, messages=list(messages or []))


class FakeFiles:
    """The bit of a Telethon client that hands over a file's bytes."""

    #: The smallest thing that still looks like a JPEG to a sniffer.
    JPEG = b"\xff\xd8\xff\xe0" + b"image bytes"

    def __init__(self, content: bytes | None = None) -> None:
        self.content = self.JPEG if content is None else content
        self.asked: list[dict] = []

    async def download_media(self, message, file=None, thumb=None):
        self.asked.append({"message": message, "thumb": thumb})
        file.write(self.content)
        return file


class FakeTransfer:
    """A `Transfer` that answers with bytes instead of reaching the network.

    It leaves behind what the real one leaves behind — a small checkout, or a
    file, in the scratch directory it is handed — and remembers what it was
    asked for, so a test can see that a clone or a fetch really happened.
    """

    def __init__(self, file: bytes = b"", fail: str = "") -> None:
        self.file = file
        self.fail = fail
        self.cloned: list[tuple] = []
        self.fetched: list[str] = []

    async def clone(self, url, branch, depth, into):
        self.cloned.append((url, branch, depth))
        if self.fail:
            raise RuntimeError(self.fail)
        checkout = into / "repo"
        (checkout / ".git").mkdir(parents=True)
        (checkout / "README.md").write_bytes(self.file)
        return checkout

    async def fetch(self, url, into):
        self.fetched.append(url)
        if self.fail:
            raise RuntimeError(self.fail)
        target = into / "download"
        target.write_bytes(self.file)
        return target


@contextmanager
def serving(
    body: bytes, chunks: int = 1, delay: float = 0.0, stall: float = 0.0, declare: bool = True
):
    """A local HTTP server for the real curl to fetch from.

    `chunks` pieces are written with `delay` seconds between them, and no
    `Content-Length` unless `declare` says the reply announces its own size — so
    a test can choose between a size that is refused up front and one that only
    a watch can stop. A `stall` answers nothing at all for that long.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if stall:
                    time.sleep(stall)
                self.send_response(200)
                if declare:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                piece = max(1, len(body) // chunks)
                for index in range(chunks):
                    self.wfile.write(body[index * piece : (index + 1) * piece])
                    self.wfile.flush()
                    if delay and index < chunks - 1:
                        time.sleep(delay)
            except OSError:
                pass  # the other end stopped listening: that is the point of a test

        def log_message(self, *arguments):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/payload.bin"
    finally:
        server.shutdown()
        server.server_close()

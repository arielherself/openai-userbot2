"""The client side of the headless-harness wire protocol.

One connection carries every conversation: a command is tagged with a `rid` that
comes back on its events, and every block's events carry its `agent_id`, so a
single reader routes what it reads to the conversation that asked for it. A
conversation subscribes to its block id *and* to the rid of its `run`, and reads
the turn's events — deltas, tool calls, the outcome — off one queue.

Local tools are how a turn reaches back: the server parks the turn, we send
`resolve_tool`, and the model continues with whatever we answered.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import socket
import subprocess
import sys
import time

log = logging.getLogger(__name__)

# The server rejects a line over 8 MiB, so the reader has to accept them too.
LINE_LIMIT = 8 * 1024 * 1024 + 4096

CONNECTION_LOST = "connection_lost"

# What the agent uses to answer the user. It carries no implementation: the
# server only needs the schema, and every call is answered from here.
TG_DRAFT_TOOL = {
    "name": "tg_draft_response",
    "description": (
        "Send your reply to the Telegram user, on behalf of the userbot account "
        "you are writing as — its name, username and id are in the message.\n"
        "This is the only way the user receives anything, and it must be the LAST "
        "thing you do: call it once, after every other tool call, when you are "
        "finished thinking.\n"
        "Write BOTH `summary` and `details` in the language of the user's message. "
        "If that language cannot be told (a bare link, an emoji, a number), write "
        "in English.\n"
        "When you write a link, write it as the bare URL with a space on each side "
        "of it, so it is not glued to the words around it — never as "
        "`[text](url)`.\n"
        "`summary`: your short reply to the user — two or three sentences "
        "addressed to them, not a description of the answer, and able to stand on "
        "its own as a reply, because it is what they read first.\n"
        "`details`: the full answer, as Markdown using **bold** only — no "
        "headings, no bullet or numbered list markup, no code fences, no italics, "
        "no `[text](url)` links. It may be longer than the summary; if the answer "
        "is short it may repeat it.\n"
        "The user sees the summary as a normal message, and the details inside a "
        "collapsed quote right below it. Assume they read both: the two are one "
        "reply, so the details carry it further instead of being an appendix the "
        "summary points at.\n"
        "Nothing you write outside this tool is delivered."
    ),
    "params": [
        {
            "name": "summary",
            "type": "string",
            "description": (
                "your short reply to the user, in the language of their message: "
                "two or three sentences addressed to them"
            ),
        },
        {
            "name": "details",
            "type": "string",
            "description": (
                "the full answer in Markdown, bold only, in the language of their "
                "message — the user reads this too; links bare, a space on each side"
            ),
        },
    ],
    # The messages really were sent, and they can be taken back: a turn that
    # fails asks us to delete them again.
    "rollback": True,
    "external_effects": True,
}


class HarnessError(Exception):
    """A command the server refused, or a connection that went away."""

    def __init__(self, code: str, message: str = "", detail=None) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.detail = detail


def new_agent_id(prefix: str) -> str:
    """A locally minted block id, so a fork need not wait for its reply."""
    return f"{prefix}-{os.urandom(6).hex()}"


class Subscription:
    """One conversation's share of the event stream."""

    def __init__(self, client: HHClient) -> None:
        self._client = client
        self._keys: set[str] = set()
        self._queue: asyncio.Queue = asyncio.Queue()

    def key(self, key: str) -> Subscription:
        self._keys.add(key)
        self._client.routes[key] = self
        return self

    def close(self) -> None:
        for key in self._keys:
            if self._client.routes.get(key) is self:
                del self._client.routes[key]
        self._keys.clear()

    def deliver(self, event: dict) -> None:
        self._queue.put_nowait(event)

    def fail(self, message: str) -> None:
        self._queue.put_nowait({"event": CONNECTION_LOST, "error": message})

    async def get(self, timeout: float | None = None) -> dict:
        if timeout is None:
            return await self._queue.get()
        return await asyncio.wait_for(self._queue.get(), timeout)


class HHClient:
    def __init__(self, host: str, port: int, name: str = "userbot") -> None:
        self.host = host
        self.port = port
        self.name = name
        self.routes: dict[str, Subscription] = {}
        self.hello: dict | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._counter = itertools.count(1)
        self._write_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._hello = asyncio.Event()
        self._closed = False

    # --- lifecycle ---------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    def next_rid(self) -> str:
        return f"{self.name}-{next(self._counter)}"

    async def connect(self, timeout: float = 10.0) -> None:
        """Open the connection and wait for the greeting. Safe to call twice."""
        async with self._connect_lock:
            if self.connected:
                return
            if self._closed:
                raise HarnessError("closed", "the client was closed")
            self._hello = asyncio.Event()
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port, limit=LINE_LIMIT),
                    timeout=timeout,
                )
            except (OSError, asyncio.TimeoutError) as error:
                raise HarnessError(
                    "unreachable", f"no harness at {self.host}:{self.port} ({error})"
                ) from error
            self._reader, self._writer = reader, writer
            self._reader_task = asyncio.create_task(self._read_loop(reader))
            try:
                await asyncio.wait_for(self._hello.wait(), timeout)
            except asyncio.TimeoutError as error:
                await self._teardown()
                raise HarnessError("unreachable", "the harness did not say hello") from error

    async def close(self) -> None:
        self._closed = True
        await self._teardown()

    async def _teardown(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except Exception as error:  # the task is gone either way
                log.debug("reader stopped with %s", error)
            self._reader_task = None
        self._reader = None
        self._lost(HarnessError("closed", "the connection is gone"))

    # --- reading and routing ------------------------------------------------
    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    raise ConnectionResetError("the harness closed the connection")
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    log.warning("ignoring a line that is not JSON: %.120r", line)
                    continue
                self._route(event)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # the connection is going away either way
            self._lost(error)

    def _route(self, event: dict) -> None:
        name = event.get("event")
        if name == "session_hello":
            self.hello = event
            self._hello.set()
            log.info(
                "harness %s:%s — protocol %s, model %s%s",
                self.host,
                self.port,
                event.get("protocol"),
                (event.get("defaults") or {}).get("model"),
                "" if (event.get("defaults") or {}).get("has_key") else " (no key configured)",
            )
            return
        if name == "session_closing":
            return
        rid = event.get("rid")
        agent_id = event.get("agent_id")
        for key in (
            f"rid:{rid}" if rid is not None else None,
            f"agent:{agent_id}" if agent_id else None,
        ):
            if key is None:
                continue
            subscription = self.routes.get(key)
            if subscription is not None:
                subscription.deliver(event)
                return
        log.debug("no route for %s", name)

    def _lost(self, error: Exception) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except OSError:
                pass
        self._writer = None
        self.hello = None
        self._hello.clear()
        for subscription in set(self.routes.values()):
            subscription.fail(str(error))
        if self._closed:
            log.debug("the harness connection is closed: %s", error)
        else:
            log.warning("harness connection lost: %s", error)

    # --- speaking -----------------------------------------------------------
    async def send(self, **command) -> str:
        """Write one command; returns the rid it went out with."""
        rid = command.setdefault("rid", self.next_rid())
        data = (json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            if not self.connected:
                raise HarnessError("not_connected", f"no harness at {self.host}:{self.port}")
            self._writer.write(data)
            await self._writer.drain()
        return rid

    def subscribe(self, *keys: str) -> Subscription:
        subscription = Subscription(self)
        for key in keys:
            subscription.key(key)
        return subscription

    async def command(self, timeout: float = 60.0, **fields) -> dict[str, dict]:
        """Send a command and collect its events until it finishes."""
        subscription = Subscription(self)
        try:
            rid = await self.send(**fields)
            subscription.key(f"rid:{rid}")
            found: dict[str, dict] = {}
            while True:
                try:
                    event = await subscription.get(timeout)
                except asyncio.TimeoutError as error:
                    raise HarnessError(
                        "timeout",
                        f"{fields.get('command')} did not finish within {timeout:g}s",
                    ) from error
                name = event["event"]
                if name == CONNECTION_LOST:
                    raise HarnessError("connection_lost", event.get("error", ""))
                if name == "error":
                    raise HarnessError(
                        event.get("code", "error"),
                        event.get("message", ""),
                        event.get("detail"),
                    )
                found[name] = event
                if name == "command_finished":
                    return found
        finally:
            subscription.close()

    async def resolve_tool(
        self,
        agent_id: str,
        call_id: str,
        result: str | None = None,
        error: str | None = None,
        images: list[str] | None = None,
    ) -> bool:
        """Answer a parked local tool call (or the undo of one).

        `images`, when given, ride along with the result as `data:` URIs, so the
        model sees them the way it sees a fork's images. An answer that carries an
        error drops them: a failed call is text.
        """
        fields: dict = {"command": "resolve_tool", "id": agent_id, "call_id": call_id}
        if error is not None:
            fields["error"] = error
        else:
            fields["result"] = "" if result is None else result
            if images:
                fields["images"] = list(images)
        try:
            await self.command(**fields)
            return True
        except HarnessError as failure:
            log.warning("could not answer %s for %s: %s", call_id, agent_id, failure)
            return False


# --- running the server -----------------------------------------------------
def is_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def spawn_server(
    host: str,
    port: int,
    project: str,
    python: str | None = None,
    db: str | None = None,
    wait: float = 10.0,
) -> subprocess.Popen:
    """Start the harness server from `project` and wait for its port."""
    if python is None:
        candidate = os.path.join(project, ".venv", "bin", "python")
        python = candidate if os.path.exists(candidate) else sys.executable
    command = [
        python,
        os.path.join(project, "src", "server.py"),
        "--host",
        host,
        "--port",
        str(port),
    ]
    if db:
        command += ["--db", db]
    log.info("starting the harness: %s", " ".join(command))
    process = subprocess.Popen(command, cwd=project)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if is_listening(host, port):
            return process
        if process.poll() is not None:
            raise HarnessError("spawn_failed", f"the harness exited with {process.returncode}")
        time.sleep(0.1)
    process.terminate()
    raise HarnessError("spawn_failed", f"the harness never listened on {host}:{port}")

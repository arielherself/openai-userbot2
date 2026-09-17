"""Running the userbot: connect the harness, log in, and serve.

Incoming Telegram messages are turned into `Incoming` here — that is the only
place that knows about Telethon — and handed to the bridge on their own task, so
one conversation never delays another.
"""

from __future__ import annotations

import asyncio
import logging
import re
import signal
import time

from telethon import TelegramClient, events, utils
from telethon.tl import types
from telethon.tl.custom import Message as TelegramMessage

from .bridge import Bridge, Identity, Incoming
from .config import Config
from .content import Quoted, content_of
from .harness import HHClient, is_listening, spawn_server
from .store import MappingStore
from .telegram import TelethonDelivery, images_of, sender_of

log = logging.getLogger("userbot")

# Message dates come from Telegram's clock, `since` from ours: a little slack keeps
# a message sent right after startup from looking like offline backlog.
CLOCK_SLACK = 60.0


def mentions_us(message, me) -> bool:
    """True when the message names us — an @username, or a text mention."""
    username = (me.username or "").lower()
    try:
        entities = message.get_entities_text()
    except Exception:  # a message whose entities do not line up with its text
        entities = []
    for entity, inner in entities:
        if isinstance(entity, types.MessageEntityMentionName) and entity.user_id == me.id:
            return True
        if isinstance(entity, types.MessageEntityMention) and inner.lstrip("@").lower() == username:
            return True
    if not username:
        return False
    return bool(
        re.search(rf"(?<!\w)@{re.escape(username)}(?!\w)", message.message or "", re.IGNORECASE)
    )


async def quoted_from(message, me, client) -> Quoted | None:
    """The message this one replies to — unless it is one of ours.

    A reply to our own message needs no quote: the turn forks from the block that
    wrote it, so that message is already in the agent's context.
    """
    header = message.reply_to
    if not isinstance(header, types.MessageReplyHeader):
        return None
    peer = header.reply_to_peer_id
    if peer is not None and utils.get_peer_id(peer) == me.id:
        return None
    reply = await message.get_reply_message()
    if not isinstance(reply, TelegramMessage) or reply.out or reply.sender_id == me.id:
        return None
    sender = await sender_of(reply)
    return Quoted(
        content=content_of(reply),
        sender_name=sender.name,
        sender_id=sender.user_id,
        sender_username=sender.username,
        excerpt=(getattr(header, "quote_text", None) or "").strip(),
        # what the sender is pointing at, pictures included
        images=await images_of(client, reply),
    )


async def incoming_from(event, me, since: float) -> Incoming | None:
    """The message, or None when it is not one we ever answer."""
    message = event.message
    if message.out or (message.sender_id is not None and message.sender_id == me.id):
        return None  # our own message
    if message.date.timestamp() < since - CLOCK_SLACK:
        return None  # the backlog of a userbot that was offline
    chat = await event.get_chat()
    sender = await sender_of(message)
    if getattr(await event.get_sender(), "bot", False):
        return None  # another bot talking to us: never answered, never logged
    return Incoming(
        chat_id=utils.get_peer_id(chat),
        message_id=message.id,
        content=content_of(message),
        sender_id=sender.user_id,
        sender_name=sender.name,
        sender_username=sender.username,
        chat_title=utils.get_display_name(chat) or None,
        chat_username=getattr(chat, "username", None),
        is_group=bool(event.is_group),
        mentioned=mentions_us(message, me),
        reply_to_message_id=message.reply_to_msg_id,
        quoted=await quoted_from(message, me, event.client),
        images=await images_of(event.client, message),
    )


async def serve(config: Config) -> None:
    store = MappingStore(config.db_path, config.max_db_bytes)
    log.info("mappings in %s (%.1f MiB budget)", config.db_path, config.max_db_bytes / (1 << 20))
    harness = HHClient(config.harness_host, config.harness_port)
    spawned = None
    client = None
    tasks: set[asyncio.Task] = set()
    try:
        if not is_listening(config.harness_host, config.harness_port):
            if not config.harness_spawn:
                raise SystemExit(
                    f"nothing is listening on {config.harness_host}:{config.harness_port}; "
                    f"start it with: python src/server.py (in {config.project_dir})"
                )
            spawned = spawn_server(
                config.harness_host,
                config.harness_port,
                config.project_dir,
                config.harness_python,
                config.harness_db,
            )
        await harness.connect()

        client = TelegramClient(config.session, config.api_id, config.api_hash)
        client.parse_mode = None  # we format messages ourselves
        await client.start()
        me = await client.get_me()
        log.info("logged in as %s (id %s)", utils.get_display_name(me) or me.username, me.id)

        bridge = Bridge(
            harness,
            store,
            TelethonDelivery(client),
            status_interval=config.status_interval,
            turn_timeout=config.turn_timeout,
            model=config.model,
            identity=Identity(
                name=utils.get_display_name(me) or "",
                username=me.username,
                user_id=me.id,
            ),
        )
        started_at = time.time()

        @client.on(events.NewMessage(incoming=True))
        async def on_message(event):
            incoming = await incoming_from(event, me, started_at)
            if incoming is None:
                return
            log.info(
                "chat %s message %s from %s%s",
                incoming.chat_id,
                incoming.message_id,
                incoming.sender_name,
                " (reply to us)"
                if incoming.reply_to_message_id
                else " (mention)"
                if incoming.mentioned
                else "",
            )
            task = asyncio.create_task(bridge.handle(incoming))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in ("SIGINT", "SIGTERM"):
            try:
                loop.add_signal_handler(getattr(signal, name), stop.set)
            except (NotImplementedError, AttributeError):  # not a POSIX loop
                pass
        log.info("ready — mention me in a group, or reply to a message of mine")
        await stop.wait()
        log.info("stopping")
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if client is not None:
            await client.disconnect()
        await harness.close()
        store.close()
        if spawned is not None:
            spawned.terminate()
            log.info("stopped the harness this process started")


def main(argv=None) -> None:
    config = Config.from_args(argv)
    config.require_credentials()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        pass

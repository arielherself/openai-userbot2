"""The real delivery: what it asks of Telethon, driven through a stub client."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from telethon import events
from telethon.tl import types
from telethon.tl.custom import Message as TgMessage

from userbot.telegram import TelethonDelivery, as_markdown, bot_message


class StubClient:
    """Just enough `TelegramClient` to record what the delivery does with it."""

    def __init__(self) -> None:
        self.handlers: list[tuple] = []
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.deleted: list[dict] = []
        self.forwarded: list[dict] = []

    async def send_message(self, chat_id, text, **fields):
        self.sent.append({"chat_id": chat_id, "text": text, **fields})
        return message(id=11, text=text)

    async def edit_message(self, chat_id, message_id, text, **fields):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, **fields})

    async def delete_messages(self, chat_id, message_ids):
        self.deleted.append({"chat_id": chat_id, "message_ids": list(message_ids)})

    def add_event_handler(self, handler, event=None):
        self.handlers.append((handler, event))

    def remove_event_handler(self, handler, event=None):
        self.handlers = [pair for pair in self.handlers if pair[0] is not handler]

    async def forward_messages(self, entity, message_id, from_peer=None):
        self.forwarded.append({"to": entity, "message_id": message_id, "from": from_peer})
        return message(id=12, text="")


def message(id=5, text="", media=None, reply_markup=None, entities=None) -> TgMessage:
    """A Telethon message, as an update would hand one over."""
    return TgMessage(
        id=id,
        peer_id=types.PeerUser(user_id=9),
        date=datetime.now(timezone.utc),
        message=text,
        out=False,
        media=media,
        reply_markup=reply_markup,
        entities=entities,
    )


def audio_document() -> types.MessageMediaDocument:
    return types.MessageMediaDocument(
        document=types.Document(
            id=1,
            access_hash=1,
            file_reference=b"",
            date=None,
            mime_type="audio/mpeg",
            size=1024,
            dc_id=2,
            attributes=[types.DocumentAttributeAudio(duration=3, title="歌")],
        )
    )


def keyboard() -> types.ReplyInlineMarkup:
    return types.ReplyInlineMarkup(
        rows=[
            types.KeyboardButtonRow(
                buttons=[
                    types.KeyboardButton(text="1", type=types.InlineButtonTypeCallback(data=b"1"))
                ]
            )
        ]
    )


class FakeUpdate:
    def __init__(self, inner) -> None:
        self.message = inner


def test_sending_leaves_the_text_alone_and_never_previews_it():
    async def scenario():
        client = StubClient()
        delivery = TelethonDelivery(client)
        assert await delivery.send(-100, "hello") == 11
        sent = client.sent[0]
        assert sent["chat_id"] == -100 and sent["text"] == "hello"
        # no markdown parsing of our own text, and no link preview or IV button
        assert sent["formatting_entities"] == []
        assert sent["parse_mode"] is None
        assert sent["link_preview"] is False

    asyncio.run(scenario())


def test_editing_speaks_the_same_language_as_sending():
    async def scenario():
        client = StubClient()
        await TelethonDelivery(client).edit(-100, 7, "thinking")
        edit = client.edits[0]
        assert (edit["chat_id"], edit["message_id"], edit["text"]) == (-100, 7, "thinking")
        assert edit["formatting_entities"] == []
        assert edit["link_preview"] is False

    asyncio.run(scenario())


def test_deleting_hands_over_a_list():
    async def scenario():
        client = StubClient()
        await TelethonDelivery(client).delete(-100, (7, 8))
        assert client.deleted == [{"chat_id": -100, "message_ids": [7, 8]}]

    asyncio.run(scenario())


def test_forwarding_returns_the_id_of_the_new_message():
    async def scenario():
        client = StubClient()
        forwarded = await TelethonDelivery(client).forward("Music163DownBot", 77, -100)
        assert forwarded == 12
        assert client.forwarded == [{"to": -100, "message_id": 77, "from": "Music163DownBot"}]

    asyncio.run(scenario())


def test_listening_hands_over_every_message_until_it_is_closed():
    async def scenario():
        client = StubClient()
        listener = TelethonDelivery(client).listen("Music163DownBot")
        assert len(client.handlers) == 2  # new messages, and edits of them
        handler, built = client.handlers[0]
        assert isinstance(built, events.NewMessage)

        await handler(FakeUpdate(message(id=1, text="fetching…")))
        await handler(FakeUpdate(message(id=2, text="", media=audio_document())))
        first = await listener.next(0.05)
        second = await listener.next(0.05)
        assert (first.id, first.text) == (1, "fetching…")
        assert second.id == 2 and second.has_music is True
        assert await listener.next(0.05) is None  # nothing else arrived

        listener.close()
        assert client.handlers == []

    asyncio.run(scenario())


def test_a_message_is_described_for_the_tools():
    assert bot_message(message(id=3, text="hi")) == bot_message(message(id=3, text="hi"))
    assert bot_message(message(id=3, text="hi")).id == 3
    assert bot_message(message(id=3, text="hi")).has_buttons is False

    listing = bot_message(message(id=4, text="🎶 results", reply_markup=keyboard()))
    assert listing.has_buttons is True and listing.text == "🎶 results"

    track = bot_message(message(id=5, media=audio_document()))
    assert track.has_music is True and track.has_buttons is False

    assert bot_message(None) is None
    assert bot_message(types.MessageEmpty(id=1, peer_id=types.PeerUser(user_id=9))) is None


def test_listening_hears_a_message_that_is_edited_to_add_its_buttons():
    async def scenario():
        client = StubClient()
        listener = TelethonDelivery(client).listen("Music163DownBot")
        built = [pair[1] for pair in client.handlers]
        assert len(built) == 2
        assert isinstance(built[0], events.NewMessage)
        assert isinstance(built[1], events.MessageEdited)

        handler = client.handlers[0][0]
        await handler(FakeUpdate(message(id=1, text="🎶 results")))  # the list, no keyboard yet
        listed = await listener.next(0.05)
        assert listed.id == 1 and listed.has_buttons is False

        # the bot edits that very message to add the keyboard of picks
        await handler(FakeUpdate(message(id=1, text="🎶 results", reply_markup=keyboard())))
        edited = await listener.next(0.05)
        assert edited.id == 1 and edited.has_buttons is True

        listener.close()
        assert client.handlers == []  # both registrations go together

    asyncio.run(scenario())


def test_a_bots_markdown_survives_the_round_trip():
    """The wire carries text plus entities; the tools get the links back."""
    url = "https://y.qq.com/n/ryqq_v2/songDetail/002OrhQA0bNYFg"
    listing = message(
        id=7,
        text="1. 「明天，你好」 - 牛奶咖啡",
        entities=[types.MessageEntityTextUrl(offset=4, length=5, url=url)],
    )
    described = bot_message(listing)
    assert described.text == "1. 「明天，你好」 - 牛奶咖啡"  # links stripped, as sent
    assert described.markdown == f"1. 「[明天，你好]({url})」 - 牛奶咖啡"

    plain = bot_message(message(id=8, text="no entities here"))
    assert plain.markdown == "no entities here"


def test_plain_text_is_left_alone_and_odd_entities_do_not_break_it():
    assert as_markdown("nothing marked up", None) == "nothing marked up"
    # an entity claiming more text than there is still renders, it just clamps
    assert as_markdown("short", [types.MessageEntityBold(offset=0, length=99)]) == "**short**"

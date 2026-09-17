"""The real delivery: what it asks of Telethon, driven through a stub client."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone

from telethon import events
from telethon.tl import types
from telethon.tl.custom import Message as TgMessage

from userbot.telegram import (
    MAX_IMAGE_BYTES,
    TelethonDelivery,
    as_markdown,
    best_size,
    bot_message,
    image_source,
    images_of,
)


class StubClient:
    """Just enough `TelegramClient` to record what the delivery does with it."""

    def __init__(self, payload: bytes | None = None, fail: bool = False) -> None:
        self.payload = payload
        self.fail_download = fail
        self.handlers: list[tuple] = []
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.deleted: list[dict] = []
        self.forwarded: list[dict] = []
        self.downloaded: list[dict] = []

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

    #: What a download hands back.
    BYTES = b"\xff\xd8\xff\xe0image bytes"

    async def download_media(self, message, file=None, thumb=None):
        self.downloaded.append({"thumb": thumb})
        if self.fail_download:
            raise RuntimeError("telegram said no")
        file.write(self.payload if self.payload is not None else self.BYTES)
        return file

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


def photo_sizes():
    return [
        types.PhotoStrippedSize(type="i", bytes=b"tiny"),
        types.PhotoSize(type="m", w=320, h=240, size=100),
        types.PhotoSize(type="x", w=800, h=600, size=200),
        types.PhotoSize(type="y", w=1280, h=960, size=300),
        types.PhotoSize(type="w", w=2560, h=1920, size=400),
    ]


def test_a_photo_is_fetched_at_a_size_worth_reading():
    # the widest that still fits what a model can read, not the 2560 one
    assert best_size(photo_sizes()) == 3
    assert best_size([types.PhotoStrippedSize(type="i", bytes=b"x")]) is None
    assert best_size(None) is None
    # nothing modest on offer: the largest there is
    big = [types.PhotoSize(type="w", w=4000, h=3000, size=9)]
    assert best_size(big) == 0


def test_what_a_message_offers_as_a_picture():
    photo = types.MessageMediaPhoto(
        photo=types.Photo(
            id=1, access_hash=1, file_reference=b"", date=None, dc_id=2, sizes=photo_sizes()
        )
    )
    assert image_source(message(media=photo)) == ("thumb", 3, "image/jpeg")

    picture = types.MessageMediaDocument(
        document=types.Document(
            id=1,
            access_hash=1,
            file_reference=b"",
            date=None,
            dc_id=2,
            mime_type="image/png",
            size=10,
            attributes=[types.DocumentAttributeFilename(file_name="cat.png")],
        )
    )
    assert image_source(message(media=picture)) == ("file", None, "image/png")

    video = types.MessageMediaDocument(
        document=types.Document(
            id=2,
            access_hash=1,
            file_reference=b"",
            date=None,
            dc_id=2,
            mime_type="video/mp4",
            size=10,
            attributes=[types.DocumentAttributeVideo(duration=9, w=1920, h=1080)],
            thumbs=[types.PhotoSize(type="m", w=320, h=180, size=50)],
        )
    )
    assert image_source(message(media=video)) == ("thumb", 0, "image/jpeg")
    assert image_source(message(id=6, text="just words")) == ("none", None, "")


def test_a_message_with_a_video_but_no_thumbnail_has_no_picture():
    video = types.MessageMediaDocument(
        document=types.Document(
            id=2,
            access_hash=1,
            file_reference=b"",
            date=None,
            dc_id=2,
            mime_type="video/mp4",
            size=10,
            attributes=[types.DocumentAttributeVideo(duration=9, w=1920, h=1080)],
        )
    )
    assert image_source(message(media=video))[0] == "none"


def test_the_bytes_are_encoded_as_a_data_uri():
    async def scenario():
        client = StubClient()
        picture = types.MessageMediaDocument(
            document=types.Document(
                id=1,
                access_hash=1,
                file_reference=b"",
                date=None,
                dc_id=2,
                mime_type="image/png",
                size=10,
                attributes=[],
            )
        )
        images = await images_of(client, message(media=picture))
        assert len(images) == 1
        assert images[0].startswith("data:image/png;base64,")
        assert base64.b64decode(images[0].split(",", 1)[1]) == StubClient.BYTES
        # an image document is fetched whole; a photo's thumb is picked by index
        assert client.downloaded == [{"thumb": None}]

    asyncio.run(scenario())


def test_an_image_that_is_too_big_or_unreachable_is_left_out():
    async def scenario():
        picture = types.MessageMediaDocument(
            document=types.Document(
                id=1,
                access_hash=1,
                file_reference=b"",
                date=None,
                dc_id=2,
                mime_type="image/png",
                size=10,
                attributes=[],
            )
        )
        huge = StubClient(payload=b"x" * (MAX_IMAGE_BYTES + 1))
        assert await images_of(huge, message(media=picture)) == []

        tight = StubClient(payload=b"x" * 100)
        assert await images_of(tight, message(media=picture), budget=10) == []

        broken = StubClient(fail=True)
        assert await images_of(broken, message(media=picture)) == []

    asyncio.run(scenario())

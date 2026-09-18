"""The Telethon edge: what becomes an Incoming, and what never does."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from support import FakeFiles
from telethon.tl import types
from telethon.tl.custom import Message as TgMessage

from userbot.bridge import build_prompt
from userbot.main import incoming_from, mentions_us, quoted_from, sender_of
from userbot.telegram import Sender, images_of

ME = types.User(id=4242, first_name="小助手", username="MyBot", access_hash=1)


def person(id=7, name="小明", username="ming", bot=False):
    return types.User(id=id, first_name=name, username=username, bot=bot or None, access_hash=1)


def group(id=99, title="测试群", username="testgroup"):
    return types.Channel(
        id=id,
        title=title,
        photo=types.ChatPhotoEmpty(),
        date=None,
        megagroup=True,
        username=username,
        access_hash=1,
    )


def photo(width=1280, height=720):
    return types.Photo(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=None,
        dc_id=2,
        sizes=[types.PhotoSize(type="m", w=width, h=height, size=10)],
    )


def tg_message(
    id=999, text="", peer=9, out=False, reply_to=None, sender=None, media=None, post_author=None
):
    """A Telethon message, as `get_reply_message` would hand one over.

    `peer=None` builds one with no sender at all, the way a post whose author
    Telegram hides arrives.
    """
    message = TgMessage(
        id=id,
        peer_id=types.PeerUser(user_id=peer) if peer else None,
        date=datetime.now(timezone.utc),
        message=text,
        out=out,
        reply_to=reply_to,
        media=media,
        post_author=post_author,
    )
    message._sender = sender  # what the entity cache would have filled in
    return message


class FakeMessage:
    def __init__(
        self,
        id=1,
        text="",
        out=False,
        sender_id=7,
        date=None,
        media=None,
        webpage=None,
        reply_to=None,
        reply=None,
        sender=None,
        entities=(),
        post_author=None,
    ) -> None:
        self.id = id
        self.message = text
        self.raw_text = text
        self.out = out
        self.sender_id = sender_id
        self.date = date or datetime.now(timezone.utc)
        self.media = media
        self.web_preview = webpage
        self.reply_to = reply_to
        self.post_author = post_author
        self.sender = sender if sender is not None else person()
        self._reply = reply
        self._entities = list(entities)

    @property
    def reply_to_msg_id(self):
        if isinstance(self.reply_to, types.MessageReplyHeader):
            return self.reply_to.reply_to_msg_id
        return None

    async def get_reply_message(self):
        return self._reply

    async def get_sender(self):
        return self.sender

    def get_entities_text(self, cls=None):
        return list(self._entities)


class FakeEvent:
    def __init__(self, message, chat=None, sender=None, is_group=True, client=None) -> None:
        self.message = message
        self.chat = chat if chat is not None else group()
        self.sender = sender if sender is not None else person()
        self.is_group = is_group
        self.client = client if client is not None else FakeFiles()

    async def get_chat(self):
        return self.chat

    async def get_sender(self):
        return self.sender


def build(event, since=0.0):
    return asyncio.run(incoming_from(event, ME, since))


def test_a_message_from_another_bot_is_ignored_silently():
    event = FakeEvent(
        FakeMessage(text="@MyBot 在吗"),
        sender=person(id=8, name="Helper", username="helperbot", bot=True),
    )
    assert build(event) is None


def test_our_own_messages_are_ignored():
    assert build(FakeEvent(FakeMessage(text="hi", out=True))) is None
    assert build(FakeEvent(FakeMessage(text="hi", sender_id=ME.id))) is None


def test_the_offline_backlog_is_ignored_but_a_fresh_message_is_not():
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert (
        build(
            FakeEvent(FakeMessage(text="@MyBot hi", date=old)),
            since=datetime.now(timezone.utc).timestamp(),
        )
        is None
    )
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)
    assert (
        build(FakeEvent(FakeMessage(text="hi", date=recent)), since=recent.timestamp()) is not None
    )


def test_a_group_message_carries_who_and_where():
    incoming = build(
        FakeEvent(FakeMessage(text="你好", media=types.MessageMediaPhoto(photo=photo())))
    )
    assert incoming.is_group is True
    assert incoming.chat_id < 0  # the marked id, as the agent should see it
    assert incoming.chat_title == "测试群"
    assert incoming.chat_username == "testgroup"
    assert incoming.sender_name == "小明"
    assert incoming.sender_username == "ming"
    assert incoming.sender_id == 7
    assert incoming.content.text == "你好"
    assert incoming.content.media == "[photo (1280×720)]"


def test_a_private_chat_has_no_group():
    event = FakeEvent(FakeMessage(text="hi"), chat=person(), is_group=False)
    incoming = build(event)
    assert incoming.is_group is False
    assert incoming.chat_title == "小明"


def test_a_mention_is_noticed_however_it_is_written():
    assert mentions_us(FakeMessage(text="@MyBot 你好"), ME) is True
    assert mentions_us(FakeMessage(text="@mybot 你好"), ME) is True
    assert mentions_us(FakeMessage(text="你好 @otherbot"), ME) is False

    named = FakeMessage(
        text="你好",
        entities=[(types.MessageEntityMentionName(offset=0, length=2, user_id=ME.id), "你好")],
    )
    assert mentions_us(named, ME) is True
    elsewhere = FakeMessage(
        text="你好",
        entities=[(types.MessageEntityMentionName(offset=0, length=2, user_id=9), "你好")],
    )
    assert mentions_us(elsewhere, ME) is False

    typed = FakeMessage(
        text="@mybot 你好",
        entities=[(types.MessageEntityMention(offset=0, length=6), "@mybot")],
    )
    assert mentions_us(typed, ME) is True


def test_a_reply_to_someone_else_brings_that_message_along():
    header = types.MessageReplyHeader(
        reply_to_msg_id=999, reply_to_peer_id=types.PeerUser(user_id=9)
    )
    reply = tg_message(id=999, text="看这个", sender=person(id=9, name="小红", username="hong"))
    incoming = build(FakeEvent(FakeMessage(text="这是什么？", reply_to=header, reply=reply)))
    assert incoming.reply_to_message_id == 999
    assert incoming.quoted is not None
    assert incoming.quoted.content.text == "看这个"
    assert incoming.quoted.sender_name == "小红"
    assert incoming.quoted.sender_username == "hong"
    assert incoming.quoted.sender_id == 9


def test_a_reply_to_our_own_message_is_not_quoted_back():
    header = types.MessageReplyHeader(
        reply_to_msg_id=1000, reply_to_peer_id=types.PeerUser(user_id=ME.id)
    )
    incoming = build(FakeEvent(FakeMessage(text="继续", reply_to=header)))
    assert incoming.reply_to_message_id == 1000
    assert incoming.quoted is None


def test_a_deleted_message_leaves_nothing_to_quote():
    header = types.MessageReplyHeader(
        reply_to_msg_id=999, reply_to_peer_id=types.PeerUser(user_id=9)
    )
    incoming = build(FakeEvent(FakeMessage(text="这是什么？", reply_to=header, reply=None)))
    assert incoming.quoted is None


def test_a_quoted_selection_comes_along():
    header = types.MessageReplyHeader(
        reply_to_msg_id=999,
        reply_to_peer_id=types.PeerUser(user_id=9),
        quote=True,
        quote_text="主要来自海外市场",
    )
    reply = tg_message(id=999, text="整段话")
    incoming = build(FakeEvent(FakeMessage(text="详细说说", reply_to=header, reply=reply)))
    assert incoming.quoted.excerpt == "主要来自海外市场"


def test_a_story_reply_is_not_a_message_reply():
    header = types.MessageReplyStoryHeader(peer=types.PeerUser(user_id=9), story_id=5)
    message = FakeMessage(text="看了你的故事", reply_to=header)
    assert asyncio.run(quoted_from(message, ME, None)) is None
    assert build(FakeEvent(message)).reply_to_message_id is None


def test_a_picture_in_the_message_and_in_the_quote_comes_along():
    photo = types.MessageMediaPhoto(
        photo=types.Photo(
            id=1,
            access_hash=1,
            file_reference=b"",
            date=None,
            dc_id=2,
            sizes=[types.PhotoSize(type="x", w=800, h=600, size=10)],
        )
    )
    header = types.MessageReplyHeader(
        reply_to_msg_id=999, reply_to_peer_id=types.PeerUser(user_id=9)
    )
    reply = tg_message(id=999, text="看这个", media=photo, sender=person(id=9, name="小红"))

    incoming = build(
        FakeEvent(FakeMessage(text="这是什么？", media=photo, reply_to=header, reply=reply))
    )
    assert incoming.images[0].startswith("data:image/jpeg;base64,")
    assert incoming.quoted.images == incoming.images  # the same picture, twice asked for


def test_a_links_preview_picture_reaches_the_agent_too():
    webpage = types.WebPage(
        id=1,
        url="https://example.com/how",
        display_url="example.com/how",
        hash=1,
        site_name="Example",
        title="How X works",
        description="",
        photo=photo(),
    )
    files = FakeFiles()
    incoming = build(
        FakeEvent(
            FakeMessage(text="@MyBot 看这个 https://example.com/how", webpage=webpage),
            client=files,
        )
    )

    assert incoming.images and incoming.images[0].startswith("data:image/jpeg;base64,")
    # the preview's own photo is what was fetched, at the size a message's gets
    assert files.asked == [{"message": webpage.photo, "thumb": 0}]


def test_a_message_without_a_picture_carries_none():
    incoming = build(FakeEvent(FakeMessage(text="就一句话")))
    assert incoming.images == []
    assert incoming.quoted is None


def test_a_hidden_sender_is_described_by_what_is_left():
    silent = tg_message(id=7, text="匿名发帖", peer=None)
    assert asyncio.run(sender_of(silent)) == Sender("anonymous admin")

    signed = tg_message(id=8, text="署名发帖", peer=None, post_author="Channel")
    assert asyncio.run(sender_of(signed)) == Sender("Channel")


def test_an_anonymous_post_carries_no_id_into_the_prompt():
    message = FakeMessage(text="@MyBot 看一下", sender_id=None, post_author="Channel")
    message.sender = None
    incoming = build(FakeEvent(message, sender=None))

    assert incoming.sender_name == "Channel"
    assert incoming.sender_id is None
    assert "from: Channel\n" in build_prompt(incoming)  # no "user id 0"


def picture():
    return types.MessageMediaPhoto(
        photo=types.Photo(
            id=1,
            access_hash=1,
            file_reference=b"",
            date=None,
            dc_id=2,
            sizes=[
                types.PhotoStrippedSize(type="i", bytes=b"tiny"),
                types.PhotoSize(type="m", w=320, h=240, size=10),
                types.PhotoSize(type="x", w=800, h=600, size=20),
            ],
        )
    )


def test_a_hidden_sender_picture_downloads_like_any_other():
    """Who sent it says nothing about the file: the media is its own."""
    hidden = tg_message(id=9, text="看这个", peer=None, media=picture())
    files = FakeFiles()

    assert asyncio.run(sender_of(hidden)) == Sender("anonymous admin")
    images = asyncio.run(images_of(files, hidden))
    assert images[0].startswith("data:image/jpeg;base64,")
    # the same readable size a named sender's photo would get, past the stripped one
    assert files.asked == [{"message": hidden, "thumb": 2}]


def test_an_anonymous_post_with_a_picture_reaches_the_agent():
    message = FakeMessage(text="@MyBot 看看", sender_id=None, media=picture())
    message.sender = None
    incoming = build(FakeEvent(message, sender=None, client=FakeFiles()))

    assert incoming.sender_name == "anonymous admin" and incoming.sender_id is None
    assert incoming.images and incoming.images[0].startswith("data:image/jpeg;base64,")


def test_an_instant_view_reaches_the_agent_with_its_pictures():
    """A link's page is read the way the message's own media is."""
    page = types.Page(
        url="https://example.com/how",
        blocks=[types.PageBlockPhoto(photo_id=1, caption=types.TextPlain("图注"))],
        photos=[photo()],
        documents=[],
    )
    link = types.WebPage(
        id=1,
        url="https://example.com/how",
        display_url="example.com/how",
        hash=0x8B1D2F0A1E5C8D3B,
        site_name="Example",
        title="How X works",
        description="A short summary.",
        cached_page=page,
    )
    files = FakeFiles()
    incoming = build(FakeEvent(FakeMessage(text="@MyBot 看看这个链接", webpage=link), client=files))

    assert incoming.images and incoming.images[0].startswith("data:image/jpeg;base64,")
    assert "[instant view content]" in incoming.content.media
    # the page's own photo is what was fetched, not anything of the message
    assert files.asked == [{"message": page.photos[0], "thumb": 0}]


def test_a_reply_to_an_anonymous_admin_is_quoted():
    """The hidden sender says nothing about the quote: it is a message like any."""
    header = types.MessageReplyHeader(
        reply_to_msg_id=999, reply_to_peer_id=types.PeerChannel(channel_id=99)
    )
    hidden = tg_message(id=999, text="匿名管理员说的话", peer=None, media=picture())
    hidden._sender = None

    incoming = build(
        FakeEvent(FakeMessage(text="@MyBot 这句什么意思？", reply_to=header, reply=hidden))
    )
    assert incoming.quoted is not None
    assert incoming.quoted.content.text == "匿名管理员说的话"
    assert incoming.quoted.sender_name == "anonymous admin"
    assert incoming.quoted.sender_id is None and incoming.quoted.sender_username is None
    assert incoming.quoted.images and incoming.quoted.images[0].startswith("data:image/jpeg")

    prompt = build_prompt(incoming)
    assert "replying to anonymous admin:" in prompt
    assert "匿名管理员说的话" in prompt


def test_a_reply_to_our_own_anonymous_post_is_a_continuation():
    """A post the userbot made through an anonymous admin alias is still ours."""
    header = types.MessageReplyHeader(
        reply_to_msg_id=1000, reply_to_peer_id=types.PeerChannel(channel_id=99)
    )
    ours = tg_message(id=1000, text="我们自己发的", peer=None, out=True)
    ours._sender = None

    incoming = build(FakeEvent(FakeMessage(text="继续说", reply_to=header, reply=ours)))
    assert incoming.reply_to_message_id == 1000
    assert incoming.quoted is None  # not quoted back at us: it is our own message

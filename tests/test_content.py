"""What a message's non-text parts look like to the agent."""

from __future__ import annotations

from telethon.tl import types

from userbot.content import (
    Content,
    content_of,
    instant_view_url,
    media_of,
    page_text,
    placeholder_for,
)


class FakeMessage:
    """The bits of a Telethon message that `content_of` reads."""

    def __init__(self, text="", media=None, webpage=None) -> None:
        self.raw_text = text
        self.media = media
        self.web_preview = webpage


def document(attributes, mime="application/octet-stream", size=0):
    return types.Document(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=None,
        mime_type=mime,
        size=size,
        dc_id=2,
        attributes=attributes,
    )


def photo(width=1280, height=720):
    return types.Photo(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=None,
        sizes=[types.PhotoSize(type="m", w=width, h=height, size=100)],
        dc_id=2,
    )


def test_text_and_media_render_together():
    content = Content(text="听听这个", media="[voice message (0:12)]")
    assert content.render() == "听听这个\n[voice message (0:12)]"


def test_a_message_without_text_says_so():
    assert Content().render() == "(no text)"
    assert Content(media="[photo]").render() == "[photo]"


def test_text_is_taken_from_the_message_and_media_described():
    message = FakeMessage("你好", types.MessageMediaPhoto(photo=photo()))
    content = content_of(message)
    assert content.text == "你好"
    assert content.media == "[photo (1280×720)]"


def test_a_file_keeps_its_name_size_and_type():
    media = types.MessageMediaDocument(
        document=document(
            [types.DocumentAttributeFilename(file_name="report.pdf")],
            mime="application/pdf",
            size=1_200_000,
        )
    )
    assert placeholder_for(media) == "[file: report.pdf (application/pdf, 1.1 MB)]"


def test_a_video_says_how_long_it_is():
    media = types.MessageMediaDocument(
        document=document(
            [
                types.DocumentAttributeFilename(file_name="clip.mp4"),
                types.DocumentAttributeVideo(duration=92, w=1920, h=1080),
            ],
            mime="video/mp4",
            size=5_242_880,
        )
    )
    assert placeholder_for(media) == ("[video: clip.mp4 (video/mp4, 5.0 MB, 1:32, 1920×1080)]")


def test_a_video_message_is_a_video_message():
    media = types.MessageMediaDocument(
        document=document(
            [types.DocumentAttributeVideo(duration=5, w=320, h=320, round_message=True)]
        )
    )
    assert placeholder_for(media) == "[video message (0:05, 320×320)]"


def test_a_voice_note_and_an_audio_track_read_differently():
    voice = types.MessageMediaDocument(
        document=document([types.DocumentAttributeAudio(duration=12, voice=True)])
    )
    audio = types.MessageMediaDocument(
        document=document(
            [
                types.DocumentAttributeFilename(file_name="song.mp3"),
                types.DocumentAttributeAudio(duration=225, title="标题", performer="歌手"),
            ],
            mime="audio/mpeg",
            size=3_000_000,
        )
    )
    assert placeholder_for(voice) == "[voice message (0:12)]"
    assert placeholder_for(audio) == "[audio: 歌手 — 标题 (audio/mpeg, 2.9 MB, 3:45)]"


def test_a_sticker_keeps_its_emoji():
    media = types.MessageMediaDocument(
        document=document(
            [
                types.DocumentAttributeSticker(alt="🐱", stickerset=types.InputStickerSetEmpty()),
            ]
        )
    )
    assert placeholder_for(media) == "[sticker: 🐱]"


def test_an_animation_is_not_a_plain_file():
    media = types.MessageMediaDocument(
        document=document(
            [
                types.DocumentAttributeFilename(file_name="cat.gif"),
                types.DocumentAttributeAnimated(),
            ],
            mime="video/mp4",
            size=1_100_000,
        )
    )
    assert placeholder_for(media) == "[animation: cat.gif (video/mp4, 1.0 MB)]"


def test_a_poll_keeps_its_question_and_options():
    poll = types.Poll(
        id=1,
        question="晚饭吃什么？",
        answers=[
            types.PollAnswer(text="火锅", option=b"1"),
            types.PollAnswer(text="烧烤", option=b"2"),
        ],
        hash=1,
    )
    media = types.MessageMediaPoll(poll=poll, results=types.PollResults())
    assert placeholder_for(media) == "[poll: 晚饭吃什么？ (2 options: 火锅 / 烧烤)]"


def test_a_question_wrapped_in_entities_still_reads():
    poll = types.Poll(
        id=1,
        question=types.TextWithEntities(text="晚饭吃什么？", entities=[]),
        answers=[types.PollAnswer(text="火锅", option=b"1")],
        hash=1,
    )
    media = types.MessageMediaPoll(poll=poll, results=types.PollResults())
    assert "晚饭吃什么？" in placeholder_for(media)


def test_a_to_do_list_keeps_its_items():
    todo = types.TodoList(
        title="买菜",
        list=[types.TodoItem(id=1, title="鸡蛋"), types.TodoItem(id=2, title="牛奶")],
    )
    assert placeholder_for(types.MessageMediaToDo(todo=todo)) == (
        "[to-do list: 买菜 (2 items: 鸡蛋 / 牛奶)]"
    )


def test_a_location_a_contact_and_a_dice():
    geo = types.GeoPoint(lat=39.9042, long=116.4074, access_hash=1)
    assert placeholder_for(types.MessageMediaGeo(geo=geo)) == "[location (39.9042, 116.4074)]"
    contact = types.MessageMediaContact(
        phone_number="+8613800138000", first_name="张", last_name="三", vcard="", user_id=9
    )
    assert placeholder_for(contact) == "[contact: 张 三 (+8613800138000, user id 9)]"
    assert placeholder_for(types.MessageMediaDice(value=5, emoticon="🎲")) == "[dice: 🎲 (5)]"


def test_a_venue_keeps_its_address():
    venue = types.MessageMediaVenue(
        geo=types.GeoPoint(lat=1.0, long=2.0, access_hash=1),
        title="天安门",
        address="东长安街",
        provider="",
        venue_id="1",
        venue_type="public",
    )
    assert placeholder_for(venue) == "[venue: 天安门 (东长安街, 1.0000, 2.0000)]"


def test_something_unrecognised_is_named_rather_than_dropped():
    media = types.MessageMediaStory(peer=types.PeerUser(user_id=1), id=5)
    assert placeholder_for(media) == "[story]"
    assert placeholder_for(types.MessageMediaUnsupported()) == "[unsupported media]"
    assert placeholder_for(types.MessageMediaEmpty()) == ""
    assert placeholder_for(None) == ""


def test_a_deleted_photo_is_still_mentioned():
    assert placeholder_for(types.MessageMediaPhoto(photo=None)) == "[photo (unavailable)]"
    assert placeholder_for(types.MessageMediaDocument(document=None)) == "[file (unavailable)]"
    expired = types.MessageMediaDocument(document=types.DocumentEmpty(id=1))
    assert placeholder_for(expired) == "[file (unavailable)]"


# --- links and Instant Views -------------------------------------------------


def webpage(**fields):
    base = {
        "id": 1,
        "url": "https://example.com/how",
        "display_url": "example.com/how",
        "hash": 0x8B1D2F0A1E5C8D3B,
        "site_name": "Example",
        "title": "How X works",
        "description": "A short summary of the article.",
    }
    base.update(fields)
    return types.WebPage(**base)


def test_a_link_preview_gives_the_title_and_the_url():
    lines = media_of(FakeMessage(webpage=webpage())).splitlines()
    assert lines[0] == "[link: How X works (Example, https://example.com/how)]"
    assert lines[1] == "link summary: A short summary of the article."


def test_an_uncached_link_is_only_a_preview():
    assert "[instant view" not in media_of(FakeMessage(webpage=webpage()))


def test_an_instant_view_gives_its_link_and_its_text():
    page = types.Page(
        url="https://example.com/how",
        blocks=[
            types.PageBlockTitle(text=types.TextPlain("How X works")),
            types.PageBlockParagraph(text=types.TextPlain("第一段。")),
            types.PageBlockParagraph(
                text=types.TextConcat(
                    texts=[types.TextPlain("还有 "), types.TextBold(types.TextPlain("粗体"))]
                )
            ),
            types.PageBlockDetails(
                title=types.TextPlain("细节"),
                blocks=[types.PageBlockParagraph(text=types.TextPlain("藏起来的段落"))],
                open=False,
            ),
            types.PageBlockList(
                items=[
                    types.PageListItemText(text=types.TextPlain("第一项")),
                    types.PageListItemText(text=types.TextPlain("第二项")),
                ]
            ),
            types.PageBlockTable(
                title=types.TextPlain("表格"),
                rows=[
                    types.PageTableRow(
                        cells=[
                            types.PageTableCell(text=types.TextPlain("A"), header=True),
                            types.PageTableCell(text=types.TextPlain("B")),
                        ]
                    )
                ],
            ),
            types.PageBlockPhoto(photo_id=1, caption=types.TextPlain("图注")),
            types.PageBlockUnsupported(),
        ],
        photos=[],
        documents=[],
    )
    lines = media_of(FakeMessage(webpage=webpage(cached_page=page))).splitlines()
    assert (
        "[instant view: https://t.me/iv?url=https%3A%2F%2Fexample.com%2Fhow&rhash=8b1d2f0a1e5c8d3b]"
        in lines
    )
    assert "[instant view content]" in lines
    assert lines[-1] == "[/instant view content]"
    body = "\n".join(lines)
    for expected in (
        "How X works",
        "第一段。",
        "还有 粗体",
        "细节",
        "藏起来的段落",
        "第一项",
        "表格",
        "A | B",
        "图注",
    ):
        assert expected in body


def test_a_partial_instant_view_says_it_is_partial():
    page = types.Page(
        url="https://example.com/how",
        blocks=[types.PageBlockParagraph(text=types.TextPlain("开头"))],
        photos=[],
        documents=[],
        part=True,
    )
    assert "only part of the page" in media_of(FakeMessage(webpage=webpage(cached_page=page)))


def test_an_empty_instant_view_leaves_just_the_link():
    page = types.Page(url="https://example.com/how", blocks=[], photos=[], documents=[])
    lines = media_of(FakeMessage(webpage=webpage(cached_page=page))).splitlines()
    assert lines[-1].startswith("[instant view: https://t.me/iv?")
    assert "instant view content" not in "\n".join(lines)


def test_the_instant_view_link_is_built_from_the_page():
    url = instant_view_url(webpage())
    assert url.startswith("https://t.me/iv?url=https%3A%2F%2Fexample.com%2Fhow&rhash=")
    assert url.endswith("8b1d2f0a1e5c8d3b")


def test_a_long_page_is_cut_short():
    page = types.Page(
        url="https://example.com/long",
        blocks=[types.PageBlockParagraph(text=types.TextPlain("很长。" * 40)) for _ in range(200)],
        photos=[],
        documents=[],
    )
    text = page_text(page, limit=500)
    assert len(text) <= 501
    assert text.endswith("…")

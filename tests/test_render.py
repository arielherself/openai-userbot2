"""Rendering a draft: bold only, a collapsed quote, and the length limit."""

from __future__ import annotations

from support import bold_of, text_of
from telethon.tl.types import MessageEntityBlockquote

from userbot.render import (
    MAX_PARTS,
    MAX_UNITS,
    build_reply_parts,
    clamp_units,
    parse_bold,
    text_units,
)


def test_bold_markers_become_spans():
    text, spans = parse_bold("前 **粗体** 后")
    assert text == "前 粗体 后"
    assert spans == [(2, 4)]


def test_other_markdown_is_left_as_plain_text():
    source = "__斜体__ `代码` ~~删除~~ [链接](https://example.invalid)"
    text, spans = parse_bold(source)
    assert text == source
    assert spans == []


def test_asterisks_that_are_not_bold_are_kept():
    assert parse_bold("2 ** 3") == ("2 ** 3", [])
    assert parse_bold("**a**b**")[0] == "ab**"


def test_the_summary_comes_first_and_the_details_sit_in_a_collapsed_quote():
    parts = build_reply_parts("一句话总结", "详细内容")
    assert len(parts) == 1
    part = parts[0]
    assert part.text == "一句话总结\n\n详细内容"
    quote = part.entities[0]
    assert isinstance(quote, MessageEntityBlockquote)
    assert quote.collapsed is True
    assert quote.offset == len("一句话总结\n\n")
    assert quote.length == len("详细内容")
    assert text_of(part) == "详细内容"


def test_bold_details_arrive_inside_the_quote():
    parts = build_reply_parts("总结", "看 **这里** 和 **那里**")
    assert bold_of(parts[0]) == ["这里", "那里"]
    assert text_of(parts[0]) == "看 这里 和 那里"


def test_offsets_are_counted_in_utf16_units():
    parts = build_reply_parts("", "🙂 **粗体**")
    quote = parts[0].entities[0]
    assert quote.offset == 0
    assert quote.length == 2 + 1 + 2  # the emoji is a surrogate pair
    assert bold_of(parts[0]) == ["粗体"]


def test_a_summary_alone_needs_no_quote():
    parts = build_reply_parts("只有总结", "   ")
    assert len(parts) == 1
    assert parts[0].text == "只有总结"
    assert parts[0].entities == []


def test_an_empty_draft_renders_nothing():
    assert build_reply_parts("", "") == []
    assert build_reply_parts("  ", "\n") == []


def test_a_long_reply_becomes_several_quoted_messages():
    details = "\n\n".join(f"第 {index} 段：" + "内容" * 60 for index in range(60))
    parts = build_reply_parts("摘要", details)
    assert len(parts) > 1
    for part in parts:
        assert text_units(part.text) <= MAX_UNITS
        quote = [e for e in part.entities if isinstance(e, MessageEntityBlockquote)]
        assert len(quote) == 1 and quote[0].collapsed is True
    assert parts[0].text.startswith("摘要\n\n")
    assert not parts[1].text.startswith("摘要")
    # every part still carries the details, and nothing was dropped at a break
    joined = "".join(text_of(part) for part in parts)
    assert joined.replace(" ", "").replace("\n", "") == details.replace(" ", "").replace("\n", "")


def test_a_long_reply_keeps_its_bold():
    details = "**开头** " + "很长的正文。" * 400 + " **结尾**"
    parts = build_reply_parts("", details)
    assert len(parts) > 1
    assert "开头" in bold_of(parts[0])
    assert "结尾" in bold_of(parts[-1])


def test_breaking_prefers_paragraphs():
    paragraphs = [f"第 {index} 段：" + "内容" * 80 for index in range(30)]
    parts = build_reply_parts("", "\n\n".join(paragraphs))
    assert len(parts) > 1
    for part in parts[:-1]:
        body = text_of(part)
        assert any(body.endswith(paragraph) for paragraph in paragraphs)


def test_a_runaway_reply_is_capped():
    parts = build_reply_parts("摘要", "很长。" * 20000)
    assert len(parts) == MAX_PARTS
    assert "(truncated" in parts[-1].text


def test_an_overlong_summary_is_cut_marked():
    text = clamp_units("字" * 5000, 100)
    assert text_units(text) <= 100
    assert text.endswith("…")

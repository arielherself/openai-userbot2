"""Turning an agent's draft into Telegram messages.

`tg_draft_response` brings two halves: a short `summary`, and the full reply as
`details`. The summary goes out as plain text, the details as a collapsed
blockquote under it — so a reader sees the gist first and expands for the rest.

Telegram allows 4096 characters per message, so a long reply becomes several
messages, each keeping its own collapsed blockquote. The draft's markdown is
parsed here rather than by Telegram: only `**bold**` is honoured, everything else
the model might emit stays as literal text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from telethon.tl.types import MessageEntityBlockquote, MessageEntityBold

# 4096 is the cap. Clients count UTF-16 code units, the server counts UTF-8
# bytes, so a chunk is measured by whichever of the two is larger and kept under
# a slightly lower budget.
MAX_UNITS = 3900
SUMMARY_MAX_UNITS = 800
MAX_PARTS = 10
TRUNCATED = "\n\n(truncated — the reply was too long)"

# `**bold**` — the closing marker must follow a non-space, so prose like "2 ** 3"
# is left alone.
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)


@dataclass
class Part:
    """One Telegram message: its text and the entities that format it."""

    text: str
    entities: list = field(default_factory=list)


def text_units(text: str) -> int:
    """How much of Telegram's per-message budget a piece of text uses."""
    return max(len(text.encode("utf-16-le")) // 2, len(text.encode("utf-8")))


def clamp_units(text: str, limit: int) -> str:
    """Shorten `text` to fit the budget, marking where it was cut."""
    if text_units(text) <= limit:
        return text
    room = max(1, limit - text_units("…"))
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if text_units(text[:middle]) <= room:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def parse_bold(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Strip the `**bold**` markers; return the plain text and the bold spans.

    Spans are character indices into the returned text. No other markdown is
    touched, so anything else the model writes stays readable as plain text.
    """
    pieces: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = position = 0
    for match in _BOLD.finditer(text):
        head = text[position : match.start()]
        pieces.append(head)
        cursor += len(head)
        body = match.group(1)
        spans.append((cursor, cursor + len(body)))
        pieces.append(body)
        cursor += len(body)
        position = match.end()
    pieces.append(text[position:])
    return "".join(pieces), spans


def _u16_table(text: str) -> list[int]:
    """Prefix sums of UTF-16 lengths: character index -> Telegram offset."""
    table = [0] * (len(text) + 1)
    for index, char in enumerate(text):
        table[index + 1] = table[index] + (2 if ord(char) > 0xFFFF else 1)
    return table


def _fit(text: str, start: int, end: int, room: int) -> int:
    """The largest cut in `(start, end]` whose slice fits in `room` units."""
    low, high = start, end
    while low < high:
        middle = (low + high + 1) // 2
        if text_units(text[start:middle]) <= room:
            low = middle
        else:
            high = middle - 1
    return low


def _break(text: str, start: int, end: int) -> tuple[int, int]:
    """Where to break in `[start, end)` — paragraph, line, word, or not at all.

    Returns the end of this chunk and the start of the next, so the whitespace
    around a break belongs to neither and both stay clean.
    """
    floor = start + (end - start) // 2
    for separator in ("\n\n", "\n", " "):
        at = text.rfind(separator, floor, end)
        if at > start:
            return at, at + len(separator)
    return end, end


def split_ranges(text: str, limit: int, first_limit: int | None = None) -> list[tuple[int, int]]:
    """Break `text` into character ranges that each fit their share of the budget."""
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        room = first_limit if first_limit is not None and not ranges else limit
        if text_units(text[start:]) <= room:
            ranges.append((start, len(text)))
            break
        end = _fit(text, start, len(text), room)
        if end <= start:
            end = start + 1  # one character can still overflow a tiny room
        end, following = _break(text, start, end)
        if end <= start:
            end = following = start + 1
        ranges.append((start, end))
        start = following
    return ranges


def split_text(text: str, limit: int, first_limit: int | None = None) -> list[str]:
    """The chunks themselves, for callers that do not need their offsets."""
    return [text[start:end] for start, end in split_ranges(text, limit, first_limit)]


def build_reply_parts(
    summary: str,
    details: str,
    limit: int = MAX_UNITS,
    max_parts: int = MAX_PARTS,
) -> list[Part]:
    """The Telegram messages for one draft: summary first, then the blockquoted rest."""
    summary = (summary or "").strip()
    details = (details or "").strip()
    plain, spans = parse_bold(details)

    if summary:
        summary = clamp_units(summary, SUMMARY_MAX_UNITS)
    prefix = summary + "\n\n" if summary else ""

    if not plain.strip():
        return [Part(summary)] if summary else []

    ranges = split_ranges(plain, limit, first_limit=max(1, limit - text_units(prefix)))
    overrides: dict[int, str] = {}
    if len(ranges) > max_parts:
        tail = ranges[max_parts - 1][0]
        ranges = ranges[: max_parts - 1] + [(tail, tail)]
        overrides[max_parts - 1] = (
            clamp_units(plain[tail:], max(1, limit - text_units(TRUNCATED))) + TRUNCATED
        )

    parts: list[Part] = []
    for index, (start, end) in enumerate(ranges):
        chunk = overrides[index] if index in overrides else plain[start:end]
        # The summary rides with the first message only; every later message is
        # nothing but its own slice of the reply.
        part_text = prefix + chunk if index == 0 else chunk
        table = _u16_table(part_text)
        body_at = len(prefix) if index == 0 else 0
        entities = [
            MessageEntityBlockquote(
                offset=table[body_at],
                length=table[len(part_text)] - table[body_at],
                collapsed=True,
            )
        ]
        for span_start, span_end in spans:
            low, high = max(span_start, start), min(span_end, end)
            if high <= low:
                continue
            body_from = body_at + (low - start)
            body_to = body_at + (high - start)
            entities.append(
                MessageEntityBold(offset=table[body_from], length=table[body_to] - table[body_from])
            )
        entities.sort(key=lambda entity: (entity.offset, -entity.length))
        parts.append(Part(part_text, entities))
    return parts

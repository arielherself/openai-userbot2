"""What a Telegram message carries, in words the agent can read.

The agent never sees a message object — only text. So everything that is not text
becomes a placeholder that says what it is and, where that is useful, what is in
it: a file keeps its name and size, a poll its question and options, a link its
title and the article Telegram cached for its Instant View.

A placeholder is always bracketed, one per line, so a model can tell at a glance
that it is reading a description rather than the sender's words.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from telethon.tl import types

# How much of a cached Instant View page is handed over.
MAX_PAGE_CHARS = 6000

# How deep the walkers will follow nested rich text and page blocks.
MAX_DEPTH = 16

# Longest name/title/description kept inside a placeholder.
MAX_LABEL = 120


@dataclass
class Content:
    """A message's text, and a placeholder for whatever else it carries."""

    text: str = ""
    media: str = ""

    def render(self) -> str:
        pieces = []
        if self.text.strip():
            pieces.append(self.text.strip())
        if self.media:
            pieces.append(self.media)
        return "\n".join(pieces) or "(no text)"


@dataclass
class Quoted:
    """A message the sender replied to, when it was not one of ours."""

    content: Content = field(default_factory=Content)
    sender_name: str = ""
    sender_id: int | None = None
    sender_username: str | None = None
    excerpt: str = ""  # the part the sender highlighted, if they quoted a selection
    images: list[str] = field(default_factory=list)  # its pictures, as `data:` URIs


# --- a message --------------------------------------------------------------


def content_of(message) -> Content:
    """The text of a message, plus placeholders for its media and link preview."""
    return Content(text=getattr(message, "raw_text", None) or "", media=media_of(message))


def media_of(message) -> str:
    lines = []
    placeholder = placeholder_for(getattr(message, "media", None))
    if placeholder:
        lines.append(placeholder)
    lines.extend(page_lines(getattr(message, "web_preview", None)))
    return "\n".join(lines)


def placeholder_for(media) -> str:
    """One line describing a media object, or "" when there is nothing to say."""
    if media is None:
        return ""
    if isinstance(media, types.MessageMediaEmpty):
        return ""
    if isinstance(media, types.MessageMediaWebPage):
        return ""  # the preview, and its Instant View, are described separately
    if isinstance(media, types.MessageMediaPhoto):
        if media.photo is None:  # revoked, or too old to fetch
            return _bracket("photo", "", ["unavailable"])
        return _bracket("photo", "", [_dimensions(media.photo)])
    if isinstance(media, types.MessageMediaDocument):
        return _document(media)
    if isinstance(media, types.MessageMediaPoll):
        return _poll(media.poll)
    if isinstance(media, types.MessageMediaGeo):
        return _bracket("location", "", [_point(media.geo)])
    if isinstance(media, types.MessageMediaGeoLive):
        return _bracket("live location", "", [_point(media.geo)])
    if isinstance(media, types.MessageMediaVenue):
        return _bracket("venue", media.title, [_short(media.address), _point(media.geo)])
    if isinstance(media, types.MessageMediaContact):
        name = " ".join(part for part in (media.first_name, media.last_name) if part)
        facts = []
        if media.phone_number:
            facts.append(media.phone_number)
        if media.user_id:
            facts.append(f"user id {media.user_id}")
        return _bracket("contact", name, facts)
    if isinstance(media, types.MessageMediaDice):
        return _bracket("dice", media.emoticon, [str(media.value)])
    if isinstance(media, types.MessageMediaGame):
        return _bracket("game", getattr(media.game, "title", ""))
    if isinstance(media, types.MessageMediaToDo):
        return _todo(media.todo)
    if isinstance(media, types.MessageMediaStory):
        return _bracket("story")
    if isinstance(media, types.MessageMediaUnsupported):
        return _bracket("unsupported media")
    return _bracket(f"unsupported media: {type(media).__name__}")


def _document(media) -> str:
    document = media.document
    if document is None or isinstance(document, types.DocumentEmpty):
        return _bracket("file", "", ["unavailable"])
    attributes = list(getattr(document, "attributes", None) or [])
    mime, size = getattr(document, "mime_type", ""), getattr(document, "size", 0)
    filename = next(
        (a.file_name for a in attributes if isinstance(a, types.DocumentAttributeFilename)), ""
    )
    sticker = next((a for a in attributes if isinstance(a, types.DocumentAttributeSticker)), None)
    if sticker is not None:
        return _bracket("sticker", _short(sticker.alt or "", 40))
    video = next((a for a in attributes if isinstance(a, types.DocumentAttributeVideo)), None)
    if video is not None:
        if video.round_message:
            return _bracket(
                "video message", "", [_duration(video.duration), f"{video.w}×{video.h}"]
            )
        return _bracket(
            "video",
            filename,
            [mime, _size(size), _duration(video.duration), _dimensions(document)],
        )
    audio = next((a for a in attributes if isinstance(a, types.DocumentAttributeAudio)), None)
    if audio is not None:
        if audio.voice:
            return _bracket("voice message", "", [_duration(audio.duration)])
        name = " — ".join(part for part in (audio.performer, audio.title) if part) or filename
        return _bracket("audio", _short(name), [mime, _size(size), _duration(audio.duration)])
    if any(isinstance(a, types.DocumentAttributeAnimated) for a in attributes):
        return _bracket("animation", filename, [mime, _size(size)])
    return _bracket("file", filename, [mime, _size(size)])


def _poll(poll) -> str:
    question = _rich(getattr(poll, "question", ""))
    answers = [_rich(answer.text) for answer in (getattr(poll, "answers", None) or [])]
    shown = " / ".join(answers[:6])
    if len(answers) > 6:
        shown += " / …"
    facts = [f"{len(answers)} options: {shown}"] if answers else []
    return _bracket("poll", _short(question), facts)


def _todo(todo) -> str:
    items = list(getattr(todo, "list", None) or [])
    titles = " / ".join(_rich(item.title) for item in items[:6])
    if len(items) > 6:
        titles += " / …"
    facts = [f"{len(items)} items: {titles}"] if items else []
    return _bracket("to-do list", _short(_rich(getattr(todo, "title", ""))), facts)


# --- link previews and Instant Views ----------------------------------------


def page_lines(webpage) -> list[str]:
    """A link preview, and — when Telegram cached one — the Instant View itself."""
    if webpage is None or isinstance(webpage, types.WebPageEmpty):
        return []
    if isinstance(webpage, types.WebPagePending):
        return ["[link: preview loading]"]
    if not isinstance(webpage, types.WebPage):
        return []

    title = _short(webpage.title or "")
    site = _short(webpage.site_name or webpage.display_url or "")
    facts = [part for part in (site, webpage.url) if part]
    lines = [_bracket("link", title or "untitled", facts)]
    if webpage.description:
        lines.append(f"link summary: {_short(webpage.description)}")

    page = webpage.cached_page
    if page is None:
        return lines
    lines.append(f"[instant view: {instant_view_url(webpage)}]")
    body = page_text(page)
    if body:
        lines.append("[instant view content]")
        lines.append(body)
        if page.part:
            lines.append("(this excerpt is only part of the page)")
        lines.append("[/instant view content]")
    return lines


def instant_view_url(webpage) -> str:
    """The link Telegram's clients open the Instant View with.

    Telegram hands the page's hash to `t.me/iv` as `rhash`; the hex rendering is
    what the official clients use.
    """
    return f"https://t.me/iv?url={quote(webpage.url, safe='')}&rhash={webpage.hash:x}"


def page_text(page, limit: int = MAX_PAGE_CHARS) -> str:
    """The readable text of a cached Instant View page."""
    lines: list[str] = []
    for block in page.blocks or []:
        lines.extend(_block_lines(block))
        if sum(len(line) for line in lines) > limit:
            break
    text = "\n".join(line for line in lines if line.strip())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _block_lines(block, depth: int = 0) -> list[str]:
    """One page block, as lines — titles and captions first, then what it holds."""
    if depth > MAX_DEPTH:
        return []
    lines: list[str] = []
    if isinstance(block, types.PageBlockAuthorDate):
        author = _rich(block.author)
        if author:
            lines.append(f"{author} · {block.published_date}")
    if isinstance(block, types.PageBlockRelatedArticles):
        lines.append(_rich(block.title))
        for article in block.articles or []:
            lines.append(_short(f"{article.title} — {article.url}"))
    if isinstance(block, types.PageBlockTable):
        for row in block.rows or []:
            cells = [_rich(cell.text) for cell in row.cells or []]
            lines.append(" | ".join(cell for cell in cells if cell))
    for attribute in ("title", "text", "caption"):
        value = getattr(block, attribute, None)
        if isinstance(value, (str, types.TypeRichText)):
            rendered = _rich(value)
            if rendered:
                lines.append(rendered)
    for child in _children(block):
        lines.extend(_block_lines(child, depth + 1))
    return [line for line in lines if line.strip()]


def _children(block):
    """The blocks nested inside a block — a list's items, a collage, a cover."""
    for name in ("blocks", "items", "cover"):
        value = getattr(block, name, None)
        if value is None:
            continue
        if isinstance(value, list):
            yield from value
        else:
            yield value


# --- small formatting helpers -----------------------------------------------


def _rich(node, depth: int = 0) -> str:
    """Plain text out of a RichText node, of any shape."""
    if node is None or depth > MAX_DEPTH or isinstance(node, types.TextEmpty):
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, types.TextConcat):
        return "".join(_rich(part, depth + 1) for part in node.texts or [])
    if isinstance(node, types.TextImage):
        return "[image]"
    if isinstance(node, types.TextUrl):
        label = _rich(node.text, depth + 1)
        return f"{label} ({node.url})" if label else node.url
    text = getattr(node, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(text, types.TypeRichText):  # TextBold, TextItalic, TextFixed, …
        return _rich(text, depth + 1)
    source = getattr(node, "source", None)
    if isinstance(source, str):
        return source
    return ""


def _bracket(label: str, name: str = "", facts=None) -> str:
    """`[label: name (fact, fact)]`, dropping the parts that are empty."""
    body = f"{label}: {name}" if name else label
    kept = [str(fact) for fact in (facts or []) if fact]
    return f"[{body} ({', '.join(kept)})]" if kept else f"[{body}]"


def _short(text: str, limit: int = MAX_LABEL) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _size(count) -> str:
    if not count:
        return ""
    for unit, step in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if count >= step:
            return f"{count / step:.1f} {unit}"
    return f"{count} B"


def _duration(seconds) -> str:
    total = int(seconds or 0)
    if total <= 0:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _dimensions(document) -> str:
    """The pixel size of a photo or a document, from whichever object carries it."""
    if isinstance(document, types.Photo):
        sizes = [
            (size.w, size.h)
            for size in document.sizes or []
            if isinstance(size, (types.PhotoSize, types.PhotoSizeProgressive))
        ]
        if sizes:
            width, height = max(sizes, key=lambda pair: pair[0] * pair[1])
            return f"{width}×{height}"
        return ""
    if isinstance(document, types.Document):
        for attribute in document.attributes or []:
            if isinstance(attribute, types.DocumentAttributeVideo):
                return f"{attribute.w}×{attribute.h}"
    return ""


def _point(geo) -> str:
    if geo is None:
        return ""
    return f"{geo.lat:.4f}, {geo.long:.4f}"

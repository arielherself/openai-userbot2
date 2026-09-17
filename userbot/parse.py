"""The tool that has the parse bot render a link.

Give @ParseHubot a link and it answers that very message with the content behind
it — an article, a video, a post. That answer is what gets forwarded into the
chat, so the userbot only has to wait for it and pass it on.
"""

from __future__ import annotations

import asyncio

from .relay import ask
from .telegram import BotMessage, Delivery
from .tools import Answer

# @ParseHubot — the account that renders the pages.
PARSE_BOT = "ParseHubot"

# How long the bot has to acknowledge a link at all, and how long the whole call
# may take — parsing a video is not instant.
FIRST_REPLY_TIMEOUT = 15.0
PARSE_TIMEOUT = 300.0

SITES = (
    "酷安 (图文), 贴吧 (视频, 图文), 豆瓣 (视频, 图文), 知乎 (问答, 专栏, 圈子, 日报), "
    "皮皮虾 (视频, 图文), 最右 (视频, 图文), 抖音 (视频, 图文, 日常), 快手 (视频, 图文), "
    "微博 (视频, 图文), 微信公众号 (图文), 小黑盒 (视频, 图文), 小红书 (视频, 图文), "
    "Youtube (视频, 音乐), Twitter (视频, 图文), TikTok (视频, 图文), Threads (视频, 图文), "
    "Snapchat (视频), Instagram (视频, 图文), Facebook (视频), Bilibili (视频, 动态)"
)

TOOL = {
    "name": "tg_send_parsed_content",
    "description": (
        "Have the parse bot render a link and send what it holds into this chat. "
        "The bot answers the link with the parsed content — a post, a video, an "
        "article — and that answer is forwarded here.\n"
        "`url`: the link to parse.\n"
        f"It handles: {SITES}.\n"
        "Waits for the bot to finish, which can take a moment. Returns a "
        "confirmation once the content has been sent, and an error when the bot "
        "never answers."
    ),
    "params": [
        {"name": "url", "type": "string", "description": "the link to parse and send"},
    ],
    # The content really was posted, so a turn that fails afterwards takes it back.
    "rollback": True,
    "external_effects": True,
}


def is_the_parsed_content(message: BotMessage, sent: int) -> bool:
    """The bots's answer to the link we sent, once it carries a link itself.

    The answer is a reply to that very message, and what makes it the parsed
    content rather than a note like "解析中…" is the link inside it.
    """
    return message.reply_to == sent and message.has_link


async def send(
    delivery: Delivery,
    url: str,
    to_chat: int,
    bot: str = PARSE_BOT,
    timeout: float = PARSE_TIMEOUT,
    first_timeout: float = FIRST_REPLY_TIMEOUT,
) -> Answer:
    """Give the bot a link, and put the content it renders into `to_chat`."""
    deadline = asyncio.get_running_loop().time() + timeout
    message, complaint = await ask(
        delivery,
        bot,
        url,
        is_the_parsed_content,
        "parse bot",
        first_timeout,
        deadline,
        timeout,
    )
    if message is None:
        return Answer(error=complaint)
    try:
        forwarded = await delivery.forward(bot, message.id, to_chat)
    except Exception as error:
        return Answer(error=f"the content arrived but forwarding it failed: {error}")
    return Answer(result="the parsed content was sent to the chat", forwarded=forwarded)

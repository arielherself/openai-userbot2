"""The parse tool: the link it sends, and the answer it forwards."""

from __future__ import annotations

import asyncio

from support import FakeTelegram, bot_message

from userbot import parse

URL = "https://www.bilibili.com/video/BV1xx411c7mD"


def answering(delivery, **answers):
    """Make the parse bot reply to the link it is sent."""

    def on_send(chat_id, text):
        for kind, message in answers.items():
            if kind == "reply":
                sent = delivery.sent[-1]["id"]
                delivery.inbox.append(bot_message(chat_id=chat_id, reply_to=sent, **message))
            else:  # an unrelated message from the bot
                delivery.inbox.append(bot_message(chat_id=chat_id, **message))

    delivery.on_send = on_send


def test_the_link_goes_to_the_parse_bot_and_the_answer_comes_back():
    async def scenario():
        delivery = FakeTelegram()
        answering(delivery, reply={"id": 90, "text": "解析结果", "has_link": True})

        answer = await parse.send(delivery, URL, to_chat=-100)
        assert delivery.sent[-1]["chat_id"] == parse.PARSE_BOT
        assert delivery.sent[-1]["text"] == URL  # the link itself is the command
        assert answer.error == "" and answer.forwarded is not None
        assert delivery.forwarded == [
            {"id": answer.forwarded, "from": parse.PARSE_BOT, "message_id": 90, "to": -100}
        ]

    asyncio.run(scenario())


def test_silence_from_the_bot_is_an_error():
    async def scenario():
        delivery = FakeTelegram()
        answer = await parse.send(delivery, URL, to_chat=-100, first_timeout=0.01)
        assert answer.result == "" and answer.forwarded is None
        assert "said nothing within 0.01s" in answer.error

    asyncio.run(scenario())


def test_a_reply_without_a_link_is_not_the_content_yet():
    async def scenario():
        delivery = FakeTelegram()
        answering(delivery, reply={"text": "解析中…"})  # a reply, but no link

        answer = await parse.send(delivery, URL, to_chat=-100, timeout=0.02, first_timeout=0.01)
        assert "did not answer within 0.02s" in answer.error
        assert delivery.forwarded == []

    asyncio.run(scenario())


def test_a_link_that_is_not_a_reply_to_our_message_is_ignored():
    async def scenario():
        delivery = FakeTelegram()
        answering(delivery, stray={"text": "some other link https://example.com", "has_link": True})

        answer = await parse.send(delivery, URL, to_chat=-100, timeout=0.02, first_timeout=0.01)
        assert answer.forwarded is None
        assert "did not answer within" in answer.error

    asyncio.run(scenario())


def test_a_reply_edited_to_carry_the_link_is_forwarded():
    """The bot may answer first and fill the link in afterwards."""

    async def scenario():
        delivery = FakeTelegram()
        sent = []

        def on_send(chat_id, text):
            sent.append(delivery.sent[-1]["id"])
            # the first version is a note; the link arrives as an edit of it
            delivery.inbox.append(
                bot_message(id=91, chat_id=chat_id, reply_to=sent[-1], text="解析中…")
            )
            delivery.inbox.append(
                bot_message(
                    id=91,
                    chat_id=chat_id,
                    reply_to=sent[-1],
                    text="看这里 https://v.douyin.com/xyz",
                    has_link=True,
                )
            )

        delivery.on_send = on_send
        answer = await parse.send(delivery, URL, to_chat=-100)
        assert answer.forwarded is not None
        assert delivery.forwarded[-1]["message_id"] == 91

    asyncio.run(scenario())


def test_a_command_that_cannot_be_sent_is_an_error():
    async def scenario():
        delivery = FakeTelegram()
        delivery.fail_send = True
        answer = await parse.send(delivery, URL, to_chat=-100, first_timeout=0.01)
        assert "could not reach the parse bot" in answer.error

    asyncio.run(scenario())


def test_content_that_cannot_be_forwarded_is_an_error():
    async def scenario():
        delivery = FakeTelegram()
        answering(delivery, reply={"id": 90, "text": "解析结果", "has_link": True})
        delivery.fail_forward = True

        answer = await parse.send(delivery, URL, to_chat=-100)
        assert answer.forwarded is None
        assert "forwarding it failed" in answer.error

    asyncio.run(scenario())


def test_the_tool_says_what_it_can_parse_and_can_be_undone():
    tool = parse.TOOL
    assert tool["name"] == "tg_send_parsed_content"
    assert [param["name"] for param in tool["params"]] == ["url"]
    assert tool["rollback"] is True and tool["external_effects"] is True
    for site in ("酷安", "贴吧", "知乎", "小红书", "Bilibili", "Youtube"):
        assert site in tool["description"]
    assert parse.FIRST_REPLY_TIMEOUT == 15
    assert parse.PARSE_TIMEOUT >= 300

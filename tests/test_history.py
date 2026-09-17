"""The two tools that read a chat's history."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from support import FakeTelegram, chat_message, chat_view

from userbot import history


def test_every_message_carries_the_time_it_was_sent():
    async def scenario():
        delivery = FakeTelegram()
        sent_at = datetime(2026, 9, 18, 4, 12, tzinfo=timezone.utc)
        delivery.chats[-100] = chat_view(
            chat_id=-100, messages=[chat_message(date=sent_at, text="刚发的")]
        )

        answer = await history.current_chat(delivery, -100)
        shown = sent_at.astimezone().strftime("%Y-%m-%d %H:%M %z")
        assert f"1. [1] {shown} 小明 (@ming, user id 7):" in answer.result

    asyncio.run(scenario())


def test_a_message_without_a_time_still_renders():
    assert history.stamp(None) == ""
    view = chat_view(chat_id=-100, messages=[chat_message(date=None)])
    assert "1. [1] 小明 (@ming, user id 7):" in history.render(view)


def test_the_current_chat_is_read_oldest_first_with_who_said_what():
    async def scenario():
        delivery = FakeTelegram()
        delivery.chats[-1001234567890] = chat_view(
            title="测试群",
            username="testgroup",
            chat_id=-1001234567890,
            messages=[
                chat_message(id=1, name="小明", username="ming", user_id=7, text="大家好"),
                chat_message(
                    id=2,
                    name="小红",
                    username="hong",
                    user_id=9,
                    text="看这个",
                    media="[photo (1280×720)]",
                ),
                chat_message(id=3, name="没有用户名", username=None, user_id=11, text="我也来了"),
            ],
        )

        answer = await history.current_chat(delivery, -1001234567890)
        assert answer.error == ""
        assert "the last 3 message(s) in 测试群 (@testgroup), -1001234567890, oldest first:" in (
            answer.result
        )
        assert "1. [1] " in answer.result
        assert "小明 (@ming, user id 7):\n大家好" in answer.result
        assert "2. [2] " in answer.result
        assert "小红 (@hong, user id 9):\n看这个\n[photo (1280×720)]" in answer.result
        assert "3. [3] " in answer.result
        assert "没有用户名 (user id 11):\n我也来了" in answer.result

    asyncio.run(scenario())


def test_a_chat_with_nothing_in_it_is_not_an_error():
    async def scenario():
        delivery = FakeTelegram()
        delivery.chats[-100] = chat_view(chat_id=-100)

        answer = await history.current_chat(delivery, -100)
        assert answer.error == ""
        assert answer.result.endswith("测试群 (@testgroup), -100: no messages")

    asyncio.run(scenario())


def test_only_the_last_messages_are_read():
    async def scenario():
        delivery = FakeTelegram()
        delivery.chats[-100] = chat_view(
            chat_id=-100,
            messages=[chat_message(id=n, text=f"第 {n} 条") for n in range(60)],
        )

        answer = await history.current_chat(delivery, -100)
        assert "the last 50 message(s)" in answer.result
        assert "第 59 条" in answer.result
        assert "第 9 条" not in answer.result

    asyncio.run(scenario())


def test_a_public_chat_is_read_by_its_username():
    async def scenario():
        delivery = FakeTelegram()
        delivery.chats["@testgroup"] = chat_view(
            title="测试群",
            username="testgroup",
            chat_id=-100,
            messages=[chat_message(text="公开的消息")],
        )

        answer = await history.public_chat(delivery, "@testgroup")
        assert answer.error == ""
        assert "公开的消息" in answer.result

    asyncio.run(scenario())


def test_a_username_without_the_at_is_refused():
    async def scenario():
        delivery = FakeTelegram()
        answer = await history.public_chat(delivery, "testgroup")
        assert answer.result == ""
        assert "must start with @" in answer.error

    asyncio.run(scenario())


def test_a_chat_that_cannot_be_read_is_an_error():
    async def scenario():
        delivery = FakeTelegram()
        unknown = await history.public_chat(delivery, "@nobody")
        assert "could not read that chat" in unknown.error
        assert "no such chat" in unknown.error

        delivery.chats[-100] = chat_view(chat_id=-100)
        delivery.fail_history = True
        broken = await history.current_chat(delivery, -100)
        assert "could not read this chat" in broken.error
        assert "telegram said no" in broken.error

    asyncio.run(scenario())


def test_one_message_can_be_read_by_its_id():
    async def scenario():
        delivery = FakeTelegram()
        delivery.chats[-100] = chat_view(
            chat_id=-100,
            messages=[
                chat_message(id=41, text="第一条"),
                chat_message(id=42, name="小红", username="hong", user_id=9, text="看这个"),
            ],
        )

        answer = await history.read_message(delivery, -100, 42)
        assert answer.error == ""
        assert "[42] " in answer.result
        assert "小红 (@hong, user id 9):" in answer.result
        assert answer.result.endswith("看这个")

    asyncio.run(scenario())


def test_reading_a_message_that_is_not_there_is_an_error():
    async def scenario():
        delivery = FakeTelegram()
        delivery.chats[-100] = chat_view(chat_id=-100, messages=[chat_message(id=41)])

        missing = await history.read_message(delivery, -100, 999)
        assert missing.result == ""
        assert missing.error == "there is no message 999 in this chat"

    asyncio.run(scenario())


def test_a_message_that_cannot_be_read_is_an_error():
    async def scenario():
        delivery = FakeTelegram()
        delivery.fail_history = True
        answer = await history.read_message(delivery, -100, 41)
        assert "could not read message 41 from this chat" in answer.error
        assert "telegram said no" in answer.error

    asyncio.run(scenario())


def test_a_message_is_forwarded_into_the_chat_it_is_in():
    async def scenario():
        delivery = FakeTelegram()
        delivery.chats[-100] = chat_view(chat_id=-100, messages=[chat_message(id=41)])

        answer = await history.forward_message(delivery, -100, 41)
        assert answer.error == "" and answer.forwarded is not None
        assert delivery.forwarded == [
            {"id": answer.forwarded, "from": -100, "message_id": 41, "to": -100}
        ]
        assert "forwarded into this chat" in answer.result

    asyncio.run(scenario())


def test_forwarding_something_that_is_not_there_does_nothing():
    async def scenario():
        delivery = FakeTelegram()
        delivery.chats[-100] = chat_view(chat_id=-100)
        answer = await history.forward_message(delivery, -100, 999)
        assert "there is no message 999" in answer.error
        assert delivery.forwarded == []

    asyncio.run(scenario())


def test_a_message_that_cannot_be_forwarded_is_an_error():
    async def scenario():
        delivery = FakeTelegram()
        delivery.chats[-100] = chat_view(chat_id=-100, messages=[chat_message(id=41)])
        delivery.fail_forward = True
        answer = await history.forward_message(delivery, -100, 41)
        assert answer.forwarded is None
        assert "could not forward that message" in answer.error

    asyncio.run(scenario())


def test_a_message_id_is_read_however_it_is_written():
    from userbot.tools import message_id_argument

    assert message_id_argument({"message_id": 42}) == (42, None)
    assert message_id_argument({"message_id": "42"}) == (42, None)
    assert "must be a number" in message_id_argument({"message_id": "abc"})[1]
    assert "must be a number" in message_id_argument({"message_id": True})[1]
    assert "positive" in message_id_argument({"message_id": 0})[1]
    assert "must be a number" in message_id_argument({})[1]


def test_the_tools_declare_what_they_need():
    current, public, read, forward = history.TOOLS
    assert current["name"] == "tg_view_current_chat"
    assert current["params"] == []  # this chat: nothing to name
    assert "rollback" not in current

    assert public["name"] == "tg_view_public_chat"
    assert [param["name"] for param in public["params"]] == ["username"]
    assert "starting with @" in public["description"]

    assert read["name"] == "tg_read_message"
    assert [param["name"] for param in read["params"]] == ["message_id", "from_chat"]
    assert read["params"][0]["type"] == "integer"
    assert "rollback" not in read  # reading changes nothing

    assert forward["name"] == "tg_forward_message"
    assert [param["name"] for param in forward["params"]] == ["message_id", "from_chat"]
    assert forward["rollback"] is True and forward["external_effects"] is True

    assert history.LIMIT == 50


def test_a_message_can_be_read_from_another_chat():
    async def scenario():
        delivery = FakeTelegram()
        delivery.chats["@elsewhere"] = chat_view(
            title="别的群",
            username="elsewhere",
            chat_id=-200,
            messages=[chat_message(id=16193, text="那边的一条消息")],
        )

        answer = await history.read_message(delivery, -100, 16193, "@elsewhere")
        assert answer.error == ""
        assert "[16193] " in answer.result and "那边的一条消息" in answer.result

    asyncio.run(scenario())


def test_a_message_can_be_forwarded_from_another_chat_into_this_one():
    async def scenario():
        delivery = FakeTelegram()
        delivery.chats["@elsewhere"] = chat_view(
            chat_id=-200, messages=[chat_message(id=16193, text="那边的一条消息")]
        )

        answer = await history.forward_message(delivery, -100, 16193, "@elsewhere")
        assert answer.error == "" and answer.forwarded is not None
        # read from there, posted here
        assert delivery.forwarded == [
            {"id": answer.forwarded, "from": "@elsewhere", "message_id": 16193, "to": -100}
        ]

    asyncio.run(scenario())


def test_a_chat_id_works_as_well_as_a_name():
    from userbot.tools import chat_argument

    async def scenario():
        delivery = FakeTelegram()
        delivery.chats[-200] = chat_view(chat_id=-200, messages=[chat_message(id=7)])

        by_id = await history.read_message(delivery, -100, 7, -200)
        assert by_id.error == ""
        # the bridge reads a written id the same way it reads a written @name
        written, complaint = chat_argument({"from_chat": "-200"})
        assert complaint is None
        assert (await history.read_message(delivery, -100, 7, written)).error == ""

    asyncio.run(scenario())


def test_a_missing_message_names_where_it_looked():
    async def scenario():
        delivery = FakeTelegram()
        delivery.chats["@elsewhere"] = chat_view(chat_id=-200)

        answer = await history.forward_message(delivery, -100, 16193, "@elsewhere")
        assert answer.error == "there is no message 16193 in the chat @elsewhere"
        assert delivery.forwarded == []

    asyncio.run(scenario())


def test_the_source_chat_is_read_leniently():
    from userbot.tools import chat_argument

    assert chat_argument({}) == (None, None)  # this chat
    assert chat_argument({"from_chat": ""}) == (None, None)
    assert chat_argument({"from_chat": "@telegram"}) == ("@telegram", None)
    assert chat_argument({"from_chat": " -100123 "}) == (-100123, None)
    assert chat_argument({"from_chat": -100123}) == (-100123, None)
    assert "must be a chat name" in chat_argument({"from_chat": "telegram"})[1]
    assert "must be a chat name" in chat_argument({"from_chat": []})[1]


def test_a_message_from_a_hidden_sender_prints_what_there_is():
    """An anonymous admin: no id, no username, maybe a signature."""
    view = chat_view(
        chat_id=-100,
        messages=[
            chat_message(
                id=1, name="anonymous admin", username=None, user_id=None, text="匿名发帖"
            ),
            chat_message(id=2, name="Channel", username=None, user_id=None, text="署名发帖"),
        ],
    )
    reading = history.render(view)
    assert "1. [1] 2026-09-18 12:12 +0800 anonymous admin:\n匿名发帖" in reading
    assert "user id 0" not in reading
    assert "2. [2] 2026-09-18 12:12 +0800 Channel:\n署名发帖" in reading

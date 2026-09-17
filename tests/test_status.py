"""The status message: when it appears, how often it is edited, and when it goes."""

from __future__ import annotations

import asyncio

from support import FakeTelegram

from userbot.status import StatusMessage, TurnStatus


def test_nothing_is_posted_until_the_agent_has_actually_started():
    async def scenario():
        delivery = FakeTelegram()
        tracker = StatusMessage(delivery, chat_id=-1, reply_to=7, min_interval=0)
        status = TurnStatus(phase="starting")
        await tracker.update(status)  # the run was accepted: nothing to report yet
        assert delivery.sent == []
        assert tracker.posted is False

        status.phase = "thinking"
        status.reasoning = 12  # the harness started talking
        await tracker.update(status)
        assert len(delivery.sent) == 1
        assert delivery.sent[0]["chat_id"] == -1
        assert delivery.sent[0]["reply_to"] == 7
        assert delivery.sent[0]["text"] == "🧠 thinking · 12 chars"
        assert tracker.posted is True

    asyncio.run(scenario())


def test_a_tool_call_on_its_own_is_enough_to_start_reporting():
    async def scenario():
        delivery = FakeTelegram()
        tracker = StatusMessage(delivery, chat_id=-1, reply_to=7, min_interval=0)
        status = TurnStatus(phase="thinking")
        status.tool("web_search")
        await tracker.update(status)
        assert delivery.sent[0]["text"] == "🧠 thinking\n🔧 calling web_search"

    asyncio.run(scenario())


def test_the_count_rides_on_the_thinking_line():
    assert TurnStatus(phase="thinking", reasoning=1234).render() == "🧠 thinking · 1,234 chars"
    status = TurnStatus(phase="thinking", reasoning=1234)
    status.tool("web_search")
    assert status.render() == "🧠 thinking · 1,234 chars\n🔧 calling web_search"
    # once the turn has an outcome of its own, the count becomes its own line
    status.phase = "done"
    assert status.render() == "✅ done\n🧠 thinking · 1,234 chars\n🔧 calling web_search"


def test_counting_ticks_and_tool_calls_show_up_in_the_text():
    status = TurnStatus(phase="thinking", reasoning=1234, content=56)
    status.tool("web_search").state = "ok"
    status.tool("tg_draft_response").state = "running"
    text = status.render()
    assert "1,234" in text and "56" in text
    assert "✅ web_search" in text
    assert "🔧 calling tg_draft_response" in text


def test_the_end_of_the_turn_is_always_published():
    async def scenario():
        delivery = FakeTelegram()
        tracker = StatusMessage(delivery, chat_id=-1, reply_to=7, min_interval=30)
        status = TurnStatus(phase="thinking", reasoning=10)
        await tracker.update(status)  # the first thing worth showing: posted
        status.tool("web_search")
        await tracker.update(status)  # too soon: dropped
        assert delivery.edits == []
        await tracker.update(status, force=True)  # the end of the turn: it lands
        assert "web_search" in delivery.edits[-1]["text"]

    asyncio.run(scenario())


def test_an_unchanged_status_is_not_re_edited():
    async def scenario():
        delivery = FakeTelegram()
        tracker = StatusMessage(delivery, chat_id=-1, reply_to=7, min_interval=0)
        status = TurnStatus(phase="thinking", reasoning=5)
        await tracker.update(status)
        await tracker.update(status)
        await tracker.update(status)
        assert delivery.edits == []

    asyncio.run(scenario())


def test_a_failure_is_written_into_the_status():
    status = TurnStatus(phase="failed", error="provider hung up")
    text = status.render()
    assert text.startswith("❌")
    assert "provider hung up" in text


def test_a_finished_turn_can_say_it_produced_nothing():
    status = TurnStatus(phase="done", note="no reply was drafted")
    assert status.render() == "✅ done (no reply was drafted)"


def test_deleting_the_status_message_stops_further_updates():
    async def scenario():
        delivery = FakeTelegram()
        tracker = StatusMessage(delivery, chat_id=-1, reply_to=7, min_interval=0)
        status = TurnStatus(phase="thinking", reasoning=4)
        await tracker.update(status)
        message_id = delivery.sent[0]["id"]
        await tracker.delete()
        assert delivery.deleted == [(-1, (message_id,))]
        status.reasoning = 100
        await tracker.update(status, force=True)
        assert delivery.edits == []
        await tracker.delete()  # idempotent
        assert len(delivery.deleted) == 1

    asyncio.run(scenario())


def test_a_chat_that_refuses_the_status_does_not_stop_the_turn():
    async def scenario():
        delivery = FakeTelegram()
        delivery.fail_send = True
        tracker = StatusMessage(delivery, chat_id=-1, reply_to=7, min_interval=0)
        status = TurnStatus(phase="thinking", reasoning=3)
        await tracker.update(status)
        assert tracker.posted is False
        delivery.fail_send = False
        await tracker.update(status, force=True)  # gives up instead of retrying
        assert delivery.sent == []

    asyncio.run(scenario())


def test_a_long_tool_error_is_kept_short():
    status = TurnStatus(phase="thinking")
    status.tool("tg_search_music").state = "failed"
    status.tool("tg_search_music").detail = "could not reach the music bot: " + "x" * 200
    line = next(line for line in status.render().splitlines() if "tg_search_music" in line)
    assert line.startswith("❌ tg_search_music failed (could not reach the music bot: ")
    assert "…" in line and len(line) < 120

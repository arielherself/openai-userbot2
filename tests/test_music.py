"""The music tools on their own: the commands they send, and the answers they take."""

from __future__ import annotations

import asyncio
import itertools

from support import FakeTelegram, bot_message

from userbot import music


def answer_with(delivery, **messages):
    """Make the music bot reply to whatever command it is sent."""

    def on_send(chat_id, text):
        for prefix, message in messages.items():
            if text.startswith(prefix):
                delivery.inbox.append(bot_message(chat_id=chat_id, **message))

    delivery.on_send = on_send


def test_the_platforms_map_to_the_names_the_bot_knows():
    assert music.PLATFORMS == {
        "NetEase": "163",
        "AppleMusic": "am",
        "QQMusic": "qq",
        "Soda": "qs",
    }
    assert list(music.PLATFORMS) == ["NetEase", "AppleMusic", "QQMusic", "Soda"]  # the order to try


def test_a_platform_is_recognised_however_it_is_written():
    assert music.resolve_platform("netease") == "NetEase"
    assert music.resolve_platform(" QQMusic ") == "QQMusic"
    assert music.resolve_platform("Spotify") is None
    assert music.resolve_platform(None) is None
    assert music.resolve_platform(163) is None


def test_a_search_sends_the_command_and_returns_the_listing():
    async def scenario():
        delivery = FakeTelegram()
        listing = "🎶 NetEase search results\n1. 「你好」 - 某人"
        answer_with(delivery, **{"/search": {"text": listing, "has_buttons": True}})

        answer = await music.search(delivery, "你好", "NetEase")
        assert answer.result == listing and answer.error == ""
        assert delivery.sent[-1]["chat_id"] == music.MUSIC_BOT
        assert delivery.sent[-1]["text"] == "/search 你好 163"

    asyncio.run(scenario())


def test_silence_from_the_bot_is_an_error():
    async def scenario():
        delivery = FakeTelegram()
        answer = await music.search(delivery, "你好", "Soda", first_timeout=0.01)
        assert answer.result == ""
        assert "said nothing within 0.01s of the command" in answer.error
        assert delivery.sent[-1]["text"] == "/search 你好 qs"

    asyncio.run(scenario())


def test_a_bot_that_only_chatters_never_answers():
    async def scenario():
        delivery = FakeTelegram()
        answer_with(delivery, **{"/search": {"text": "searching…"}})  # no buttons
        answer = await music.search(delivery, "你好", "NetEase", timeout=0.01, first_timeout=0.01)
        assert "did not answer within 0.01s" in answer.error

    asyncio.run(scenario())


def test_a_track_is_forwarded_into_the_chat():
    async def scenario():
        delivery = FakeTelegram()
        answer_with(delivery, **{"/music": {"id": 9, "has_music": True}})

        answer = await music.send(delivery, "https://y.qq.com/x", "AppleMusic", to_chat=-100)
        assert delivery.sent[-1]["text"] == "/music https://y.qq.com/x am"
        assert answer.forwarded is not None and answer.error == ""
        assert delivery.forwarded == [
            {"id": answer.forwarded, "from": music.MUSIC_BOT, "message_id": 9, "to": -100}
        ]
        assert "sent to the chat" in answer.result

    asyncio.run(scenario())


def test_a_refusal_is_the_result_and_nothing_is_forwarded():
    async def scenario():
        delivery = FakeTelegram()
        answer_with(delivery, **{"/music": {"text": "Fail: link not supported"}})

        answer = await music.send(delivery, "https://x/y", "NetEase", to_chat=-100)
        assert answer.forwarded is None
        assert answer.result == "Fail: link not supported"
        assert answer.error == ""  # the bot answered; that is a result, not our failure
        assert delivery.forwarded == []

    asyncio.run(scenario())


def test_progress_messages_are_not_mistaken_for_the_answer():
    async def scenario():
        delivery = FakeTelegram()

        def on_send(chat_id, text):
            delivery.inbox.append(bot_message(id=1, chat_id=chat_id, text="downloading…"))
            delivery.inbox.append(bot_message(id=2, chat_id=chat_id, has_music=True))

        delivery.on_send = on_send
        answer = await music.send(delivery, "https://x/y", "QQMusic", to_chat=-100)
        assert answer.forwarded is not None

    asyncio.run(scenario())


def test_a_command_that_cannot_be_sent_is_an_error():
    async def scenario():
        delivery = FakeTelegram()
        delivery.fail_send = True
        answer = await music.search(delivery, "你好", "NetEase", first_timeout=0.01)
        assert answer.result == ""
        assert "could not reach the music bot" in answer.error

    asyncio.run(scenario())


def test_a_track_that_cannot_be_forwarded_is_an_error():
    async def scenario():
        delivery = FakeTelegram()
        answer_with(delivery, **{"/music": {"id": 9, "has_music": True}})
        delivery.fail_forward = True

        answer = await music.send(delivery, "https://x/y", "Soda", to_chat=-100)
        assert answer.forwarded is None
        assert "forwarding it failed" in answer.error

    asyncio.run(scenario())


def test_the_waits_allow_a_slow_fetch_but_not_silence():
    assert music.FIRST_REPLY_TIMEOUT == 30
    assert music.SEARCH_TIMEOUT >= 300
    assert music.MUSIC_TIMEOUT >= 300
    # the harness must outlast the whole exchange, or it reports our own timeout
    assert music.LOCAL_TIMEOUT > music.FIRST_REPLY_TIMEOUT + music.MUSIC_TIMEOUT


def test_the_listing_keeps_the_links_the_bot_wrote():
    """A bot's markdown becomes entities on the wire; the agent needs it back."""

    async def scenario():
        delivery = FakeTelegram()
        url = "https://y.qq.com/n/ryqq_v2/songDetail/002OrhQA0bNYFg"
        plain = "1. 「明天，你好」 - 牛奶咖啡"
        answer_with(
            delivery,
            **{
                "/search": {
                    "text": plain,
                    "markdown": f"1. 「[明天，你好]({url})」 - [牛奶咖啡](https://y.qq.com/n/ryqq_v2/singer/0012bj8d36Xkw1)",
                    "has_buttons": True,
                }
            },
        )

        answer = await music.search(delivery, "你好", "QQMusic")
        assert url in answer.result  # the link the agent has to hand to tg_send_music
        assert answer.result.startswith("1. 「[明天，你好](")

    asyncio.run(scenario())


def test_the_tools_declare_what_a_rollback_needs_to_know():
    search, send = music.TOOLS
    assert search["name"] == "tg_search_music"
    assert [param["name"] for param in search["params"]] == ["keyword", "platform"]
    assert "NetEase, AppleMusic, QQMusic" in search["description"]  # the order to try

    assert send["name"] == "tg_send_music"
    assert [param["name"] for param in send["params"]] == ["url", "platform"]
    assert send["rollback"] is True and send["external_effects"] is True
    assert "tg_search_music" in send["description"]


def test_one_call_at_a_time_with_a_pause_after_each():
    async def scenario():
        gate = music.Gate(gap=0.05)
        loop = asyncio.get_running_loop()
        events: list[tuple[str, float]] = []

        async def call():
            async with gate.slot(loop.time() + 5, 5):
                events.append(("start", loop.time()))
                await asyncio.sleep(0.02)  # the call itself takes a moment
                events.append(("end", loop.time()))

        await asyncio.gather(call(), call(), call())

        assert [kind for kind, _ in events] == ["start", "end"] * 3  # never overlapped
        # each pause is measured from the end of the call before it
        for (kind, at), (_, following) in itertools.pairwise(events):
            if kind == "end":
                assert following - at >= 0.05

    asyncio.run(scenario())


def test_the_gap_is_measured_from_the_end_of_a_slow_call():
    async def scenario():
        gate = music.Gate(gap=0.05)
        loop = asyncio.get_running_loop()
        marks: list[float] = []

        async def slow():
            async with gate.slot(loop.time() + 5, 5):
                await asyncio.sleep(0.05)

        async def quick():
            async with gate.slot(loop.time() + 5, 5):
                marks.append(loop.time())

        started = loop.time()
        await asyncio.gather(slow(), quick())
        # 0.05 of work, then the 0.05 pause, then the second call
        assert marks[0] - started >= 0.1

    asyncio.run(scenario())


def test_a_call_that_would_only_get_its_turn_too_late_gives_up():
    async def scenario():
        gate = music.Gate(gap=0.3)
        delivery = FakeTelegram()
        loop = asyncio.get_running_loop()

        async def somebody_else():
            async with gate.slot(loop.time() + 5, 5):
                await asyncio.sleep(0.05)

        holder = asyncio.create_task(somebody_else())
        await asyncio.sleep(0.01)  # the other call is now at the bot

        answer = await music.search(delivery, "你好", "NetEase", timeout=0.02, gate=gate)
        assert answer.result == ""
        assert "the music bot is busy" in answer.error
        assert delivery.sent == []  # it never even sent its command

        await holder

    asyncio.run(scenario())


def test_the_wait_for_a_turn_comes_out_of_the_call_budget():
    async def scenario():
        gate = music.Gate(gap=0.05)
        delivery = FakeTelegram()
        loop = asyncio.get_running_loop()

        async def somebody_else():
            async with gate.slot(loop.time() + 5, 5):
                await asyncio.sleep(0.02)

        holder = asyncio.create_task(somebody_else())
        await asyncio.sleep(0.005)

        started = loop.time()
        # enough budget to queue, and an empty inbox: it gets its turn, then the
        # bot says nothing, so the call reports that rather than the rate limit
        answer = await music.search(
            delivery, "你好", "Soda", timeout=1, first_timeout=0.01, gate=gate
        )
        assert "said nothing within 0.01s" in answer.error
        assert started + 0.05 <= loop.time()  # it really did wait its turn
        assert delivery.sent[-1]["text"] == "/search 你好 qs"

        await holder

    asyncio.run(scenario())

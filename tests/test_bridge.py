"""The bridge: what a mention and a reply each turn into."""

from __future__ import annotations

import asyncio
import time

from support import (
    FakeHarness,
    FakeTelegram,
    bold_of,
    bot_message,
    chat_message,
    chat_view,
    quotes_in,
    text_of,
)

from userbot.bridge import Bridge, Identity, Incoming
from userbot.content import Content, Quoted
from userbot.harness import HHClient
from userbot.music import MUSIC_BOT, Gate
from userbot.parse import PARSE_BOT
from userbot.store import MappingStore


class ScriptedAgent:
    """A harness that answers like the real one would.

    `calls` is what the turn does, in order: `(tool name, arguments)` pairs. The
    default is the one call every turn ends with, `tg_draft_response`.
    """

    #: the tools that declare a rollback, so a failed turn offers their undo
    ROLLBACKS = (
        "tg_draft_response",
        "tg_send_music",
        "tg_send_parsed_content",
        "tg_forward_message",
    )

    def __init__(
        self,
        summary: str = "总结",
        details: str = "正文",
        fail: bool = False,
        draft: bool = True,
        missing_details: bool = False,
        missing_parent: str | None = None,
        quiet: bool = False,
        calls: list[tuple[str, dict]] | None = None,
    ) -> None:
        self.summary, self.details = summary, details
        self.fail, self.draft = fail, draft
        self.missing_details = missing_details
        self.missing_parent = missing_parent
        self.quiet = quiet  # never emits a delta: the turn produces nothing
        if calls is None:
            calls = []
            if draft:
                arguments = {"summary": summary}
                if not missing_details:
                    arguments["details"] = details
                calls = [("tg_draft_response", arguments)]
        self.calls = list(calls)
        self.answers = {self._call_id(index): asyncio.Event() for index in range(len(self.calls))}
        self.undos = {
            f"{self._call_id(index)}:rollback": asyncio.Event()
            for index, (name, _) in enumerate(self.calls)
            if name in self.ROLLBACKS
        }
        self.outcomes: dict[str, bool] = {}
        self.resolved: list[dict] = []

    @staticmethod
    def _call_id(index: int) -> str:
        return f"call_{index + 1}"

    def calls_named(self, name: str) -> list[tuple[str, dict]]:
        return [call for call in self.calls if call[0] == name]

    def answer_for(self, call_id: str) -> dict:
        return next(command for command in self.resolved if command["call_id"] == call_id)

    async def __call__(self, harness, conn, command):
        name = command["command"]
        if name == "create_agent":
            await conn.emit(
                event="agent_created",
                rid=command["rid"],
                agent_id=command["id"],
                parent=None,
                depth=0,
                dirty=False,
                tools=["get_current_time"],
                local_tools=command.get("local_tools", []),
            )
            await self._done(conn, command)
        elif name == "fork":
            if command["id"] == self.missing_parent:
                await conn.emit(
                    event="error",
                    rid=command["rid"],
                    command="fork",
                    code="unknown_agent",
                    message="no such block",
                    detail={"agents": []},
                )
                return
            await conn.emit(
                event="agent_forked",
                rid=command["rid"],
                agent_id=command["new_id"],
                parent=command["id"],
                depth=1,
                dirty=True,
                prompt=command["prompt"],
                prompt_chars=len(command["prompt"]),
                path=[command["id"]],
                context_len=1,
                model="fake-model",
                tools=["get_current_time"],
                local_tools=command.get("local_tools", []),
                state_namespaces=[],
            )
            await self._done(conn, command)
        elif name == "run":
            asyncio.create_task(self._turn(conn, command))
        elif name == "resolve_tool":
            self.resolved.append(command)
            call_id = command["call_id"]
            self.outcomes[call_id] = "error" not in command
            await conn.emit(
                event="local_tool_answered",
                rid=command["rid"],
                agent_id=command["id"],
                call_id=call_id,
                ok=self.outcomes[call_id],
                result_chars=0,
            )
            await self._done(conn, command)
            waiting = self.answers.get(call_id) or self.undos.get(call_id)
            if waiting is not None:
                waiting.set()

    async def _done(self, conn, command) -> None:
        await conn.emit(
            event="command_finished",
            rid=command["rid"],
            command=command["command"],
            target=command.get("id"),
            status="ok",
            elapsed_ms=1.0,
        )

    async def _turn(self, conn, command) -> None:
        block, rid = command["id"], command["rid"]
        await conn.emit(event="run_accepted", rid=rid, agent_id=block, depth=1, context_len=1)
        await conn.emit(
            event="turn_started",
            agent_id=block,
            prompt="…",
            prompt_chars=1,
            depth=1,
            path=[],
            model="fake-model",
            tools=[],
            context_len=1,
        )
        if not self.quiet:
            await conn.emit(
                event="reasoning_delta", agent_id=block, round=1, text="想一想", chars=3
            )
        for index, (name, arguments) in enumerate(self.calls):
            call_id = self._call_id(index)
            await conn.emit(
                event="tool_call_requested",
                agent_id=block,
                round=1,
                call_id=call_id,
                name=name,
                raw_arguments="{}",
                known=True,
            )
            await conn.emit(
                event="local_tool_called",
                agent_id=block,
                round=1,
                call_id=call_id,
                name=name,
                kind="call",
                arguments=arguments,
                raw_arguments="{}",
                timeout_ms=120_000,
            )
            await asyncio.wait_for(self.answers[call_id].wait(), 3)
            await conn.emit(
                event="tool_call_finished",
                agent_id=block,
                round=1,
                call_id=call_id,
                name=name,
                ok=self.outcomes.get(call_id, True),
                error=None if self.outcomes.get(call_id, True) else "client reported a failure",
                result="ok",
                result_chars=2,
                elapsed_ms=1.0,
            )
        if self.fail:
            # a turn that does not commit is offered the undo of everything it did,
            # newest call first
            undoable = [
                index for index, (name, _) in enumerate(self.calls) if name in self.ROLLBACKS
            ]
            for position, index in enumerate(reversed(undoable), start=1):
                call_id = self._call_id(index)
                name, arguments = self.calls[index]
                await conn.emit(
                    event="rollback_started",
                    call_id=call_id,
                    tool=name,
                    index=position,
                    total=len(undoable),
                )
                await conn.emit(
                    event="local_tool_rollback",
                    agent_id=block,
                    call_id=f"{call_id}:rollback",
                    name=name,
                    kind="rollback",
                    rollback_of=call_id,
                    arguments=arguments,
                    result="ok",
                    call_ok=True,
                    timeout_ms=120_000,
                )
                await asyncio.wait_for(self.undos[f"{call_id}:rollback"].wait(), 3)
                await conn.emit(
                    event="rollback_finished",
                    call_id=call_id,
                    tool=name,
                    ok=True,
                    error=None,
                    result="deleted",
                    result_chars=7,
                    elapsed_ms=1.0,
                )
            await conn.emit(
                event="turn_failed",
                agent_id=block,
                error="the provider hung up",
                error_type="HHAgentError",
                text="",
                elapsed_ms=9.0,
                context_len=1,
                dirty=False,
            )
            await conn.emit(
                event="command_finished",
                rid=rid,
                command="run",
                agent_id=block,
                status="error",
                error="the provider hung up",
                error_type="HHAgentError",
                dirty=False,
                messages=1,
                context_len=1,
                state_committed=[],
            )
        else:
            await conn.emit(
                event="turn_finished",
                agent_id=block,
                text="…",
                text_chars=1,
                rounds=1,
                tool_calls=len(self.calls),
                elapsed_ms=9.0,
                messages=1,
                context_len=1,
                dirty=False,
            )
            await conn.emit(
                event="command_finished",
                rid=rid,
                command="run",
                agent_id=block,
                status="ok",
                error=None,
                error_type=None,
                dirty=False,
                messages=1,
                context_len=1,
                state_committed=[],
            )


def mention(message_id=50, text="你好 @bot", media="") -> Incoming:
    return Incoming(
        chat_id=-100,
        message_id=message_id,
        content=Content(text=text, media=media),
        sender_id=7,
        sender_name="小明",
        sender_username="ming",
        chat_title="测试群",
        chat_username="testgroup",
        is_group=True,
        mentioned=True,
    )


def reply_to(message_id=51, to=1000, text="继续说", quoted=None) -> Incoming:
    return Incoming(
        chat_id=-100,
        message_id=message_id,
        content=Content(text=text),
        sender_id=7,
        sender_name="小明",
        sender_username="ming",
        chat_title="测试群",
        chat_username="testgroup",
        is_group=True,
        mentioned=False,
        reply_to_message_id=to,
        quoted=quoted,
    )


async def run_bridge(
    agent, message, tmp_path, store_ready=None, identity=None, delivery_ready=None, music_gate=None
):
    """Drive one message through the bridge; returns everything to assert on."""
    harness = await FakeHarness(agent).start()
    client = HHClient(harness.host, harness.port)
    await client.connect()
    delivery = FakeTelegram()
    store = MappingStore(str(tmp_path / "mappings.db"))
    if store_ready:
        store_ready(store)
    if delivery_ready:
        delivery_ready(delivery)  # e.g. script the music bot's answers
    bridge = Bridge(
        client, store, delivery, status_interval=0.0, identity=identity, music_gate=music_gate
    )
    try:
        await bridge.handle(message)
    finally:
        await client.close()
        await harness.stop()
    return harness, delivery, store


def test_the_agent_is_told_whose_voice_it_writes_in(tmp_path):
    agent = ScriptedAgent()

    async def scenario():
        harness, _, store = await run_bridge(
            agent,
            mention(),
            tmp_path,
            identity=Identity(name="小助手", username="mybot", user_id=4242),
        )
        try:
            prompt = harness.commands_named("fork")[0]["prompt"]
            assert (
                "you are replying on behalf of the userbot 小助手 (@mybot, user id 4242)" in prompt
            )
            # the language rule, both in the prompt and in the tool the model sees
            assert "Answer in the language of the sender's message above" in prompt
            assert "in English if that language cannot be told" in prompt
            assert "which the sender reads too" in prompt
            tool = harness.commands_named("create_agent")[0]["local_tools"][0]
            assert "on behalf of the userbot account" in tool["description"]
            assert "in the language of the user's message" in tool["description"]
            assert "must be the LAST" in tool["description"]
            # the summary is a reply to the sender, and both halves are read
            assert "your short reply to the user" in tool["description"]
            assert "Assume they read both" in tool["description"]
            assert "appendix" in tool["description"]
            # a link is a bare URL with room around it
            assert "with a space on each side" in tool["description"]
            assert "never as `[text](url)`" in tool["description"]
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_mention_opens_a_conversation_and_delivers_the_draft(tmp_path):
    agent = ScriptedAgent(summary="一句话总结", details="看 **这里** 的正文")

    async def scenario():
        harness, delivery, store = await run_bridge(agent, mention(), tmp_path)
        try:
            created = harness.commands_named("create_agent")[0]
            assert [tool["name"] for tool in created["local_tools"]] == [
                "tg_draft_response",
                "tg_search_music",
                "tg_send_music",
                "tg_send_parsed_content",
                "tg_view_current_chat",
                "tg_view_public_chat",
                "tg_read_message",
                "tg_forward_message",
            ]
            assert created["local_tools"][0]["rollback"] is True
            # the harness must be willing to wait at least as long as the music
            # tools do, or a slow fetch is reported as a tool timeout
            assert created["local_timeout"] >= 300

            fork = harness.commands_named("fork")[0]
            assert "小明 (@ming, user id 7)" in fork["prompt"]
            assert "测试群 (@testgroup, group id -100)" in fork["prompt"]
            assert "你好 @bot" in fork["prompt"]
            # the turn is forked with the tools themselves, not just the root
            assert [tool["name"] for tool in fork["local_tools"]] == [
                "tg_draft_response",
                "tg_search_music",
                "tg_send_music",
                "tg_send_parsed_content",
                "tg_view_current_chat",
                "tg_view_public_chat",
                "tg_read_message",
                "tg_forward_message",
            ]
            assert fork["local_timeout"] >= 300

            # the status message went out as a reply and was taken down again
            status, answer = delivery.sent[0], delivery.sent[1]
            assert status["reply_to"] == 50
            assert delivery.edits, "the status message should have been edited"
            assert any("🧠" in edit["text"] for edit in delivery.edits)
            assert all("想一想" not in edit["text"] for edit in delivery.edits)  # not expanded
            assert delivery.deleted == [(-100, (status["id"],))]

            # the answer: summary, then the details in a collapsed quote
            assert answer["reply_to"] == 50
            assert answer["text"] == "一句话总结\n\n看 这里 的正文"
            assert quotes_in(answer)[0].collapsed is True
            assert text_of(answer) == "看 这里 的正文"
            assert bold_of(answer) == ["这里"]

            # the reply points back at the block that produced it, so replying
            # to it continues this very conversation
            assert store.lookup(-100, answer["id"]) == fork["new_id"]

            resolved = agent.resolved[-1]
            assert resolved["call_id"] == "call_1" and "delivered" in resolved["result"]
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_reply_continues_the_conversation_it_answered(tmp_path):
    agent = ScriptedAgent()

    async def scenario():
        harness, delivery, store = await run_bridge(
            agent, reply_to(), tmp_path, lambda s: s.record(-100, 1000, "tg-old")
        )
        try:
            fork = harness.commands_named("fork")[0]
            assert fork["id"] == "tg-old"  # forked from the block that answered, not a root
            assert harness.commands_named("create_agent") == []
            # a conversation continuing an older chain still gets today's tools
            assert [tool["name"] for tool in fork["local_tools"]] == [
                "tg_draft_response",
                "tg_search_music",
                "tg_send_music",
                "tg_send_parsed_content",
                "tg_view_current_chat",
                "tg_view_public_chat",
                "tg_read_message",
                "tg_forward_message",
            ]
            assert "replies to one you sent earlier" in fork["prompt"]
            assert delivery.sent[1]["reply_to"] == 51
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_reply_to_something_we_never_answered_is_ignored(tmp_path):
    agent = ScriptedAgent()

    async def scenario():
        harness, delivery, store = await run_bridge(agent, reply_to(to=999), tmp_path)
        try:
            assert harness.commands == []
            assert delivery.sent == []
            assert delivery.edits == []
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_mention_inside_a_reply_still_opens_a_conversation(tmp_path):
    agent = ScriptedAgent()

    async def scenario():
        message = reply_to(to=999)
        message.mentioned = True
        harness, delivery, store = await run_bridge(agent, message, tmp_path)
        try:
            assert len(harness.commands_named("create_agent")) == 1
            assert delivery.sent  # answered
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_quoted_message_and_its_media_are_handed_to_the_agent(tmp_path):
    agent = ScriptedAgent()
    quoted = Quoted(
        content=Content(
            text="看这个",
            media="[file: report.pdf (application/pdf, 1.2 MB)]\n[photo (1280×720)]",
        ),
        sender_name="小红",
        sender_id=9,
        sender_username="hong",
        excerpt="这一段",
    )

    async def scenario():
        message = reply_to(to=999, text="这是什么？", quoted=quoted)
        message.mentioned = True
        harness, _, store = await run_bridge(agent, message, tmp_path)
        try:
            prompt = harness.commands_named("fork")[0]["prompt"]
            assert "replying to 小红 (@hong, user id 9)" in prompt
            assert "the sender highlighted: 这一段" in prompt
            assert "--- quoted message ---" in prompt
            assert "--- end of quoted message ---" in prompt
            assert "看这个" in prompt
            assert "report.pdf" in prompt
            assert "这是什么？" in prompt
        finally:
            store.close()

    asyncio.run(scenario())


def test_the_media_of_the_message_itself_is_handed_to_the_agent(tmp_path):
    agent = ScriptedAgent()

    async def scenario():
        harness, _, store = await run_bridge(
            agent, mention(text="听听这个", media="[voice message (0:12)]"), tmp_path
        )
        try:
            prompt = harness.commands_named("fork")[0]["prompt"]
            assert "听听这个" in prompt
            assert "[voice message (0:12)]" in prompt
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_failure_after_the_reply_takes_the_reply_back(tmp_path):
    agent = ScriptedAgent(fail=True)

    async def scenario():
        harness, delivery, store = await run_bridge(agent, mention(), tmp_path)
        try:
            answer = delivery.sent[1]
            # the failed turn rolled the reply back, and the failure was reported
            assert (-100, (answer["id"],)) in delivery.deleted
            assert store.lookup(-100, answer["id"]) is None
            failure = delivery.live()[-1]
            assert failure["reply_to"] == 50
            assert "agent failed" in failure["text"]
            assert "the provider hung up" in failure["text"]
            # the failure message is mapped too, so a reply to it continues on
            assert store.lookup(-100, failure["id"]) == harness.commands_named("fork")[0]["new_id"]
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_failure_before_the_reply_leaves_the_status_saying_so(tmp_path):
    agent = ScriptedAgent(fail=True, draft=False)

    async def scenario():
        _, delivery, store = await run_bridge(agent, mention(), tmp_path)
        try:
            status = delivery.sent[0]
            assert status["reply_to"] == 50
            assert delivery.edits[-1]["text"].startswith("❌ failed")
            failure = delivery.live()[-1]
            assert "agent failed" in failure["text"]
            assert store.lookup(-100, failure["id"]) is not None
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_turn_that_dies_before_saying_anything_leaves_no_status_message(tmp_path):
    agent = ScriptedAgent(fail=True, draft=False, quiet=True)

    async def scenario():
        _, delivery, store = await run_bridge(agent, mention(), tmp_path)
        try:
            # only the failure notice: nothing was ever worth reporting
            assert len(delivery.sent) == 1
            assert "agent failed" in delivery.sent[0]["text"]
            assert delivery.sent[0]["reply_to"] == 50
            assert delivery.edits == []
            assert store.lookup(-100, delivery.sent[0]["id"]) is not None
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_turn_that_ends_without_a_reply_says_so(tmp_path):
    agent = ScriptedAgent(draft=False)

    async def scenario():
        _, delivery, store = await run_bridge(agent, mention(), tmp_path)
        try:
            assert len(delivery.sent) == 1  # the status message, still standing
            assert "no reply was drafted" in delivery.edits[-1]["text"]
            assert store.count() == 0
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_draft_without_details_is_refused(tmp_path):
    agent = ScriptedAgent(missing_details=True)

    async def scenario():
        _, delivery, store = await run_bridge(agent, mention(), tmp_path)
        try:
            assert len(delivery.sent) == 1  # nothing but the status message
            assert "error" in agent.resolved[-1]
            assert store.count() == 0
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_block_the_harness_forgot_starts_a_fresh_conversation(tmp_path):
    agent = ScriptedAgent(missing_parent="tg-gone")

    async def scenario():
        harness, delivery, store = await run_bridge(
            agent, reply_to(), tmp_path, lambda s: s.record(-100, 1000, "tg-gone")
        )
        try:
            forks = harness.commands_named("fork")
            assert forks[0]["id"] == "tg-gone"  # refused
            assert len(harness.commands_named("create_agent")) == 1
            assert forks[-1]["id"] != "tg-gone"  # retried from a fresh root
            assert delivery.sent[1]["reply_to"] == 51
        finally:
            store.close()

    asyncio.run(scenario())


def test_every_message_of_a_long_reply_is_mapped(tmp_path):
    agent = ScriptedAgent(
        summary="摘要",
        details="\n\n".join(f"第 {i} 段" + "内容" * 400 for i in range(20)),
    )

    async def scenario():
        harness, delivery, store = await run_bridge(agent, mention(), tmp_path)
        try:
            status_id = delivery.sent[0]["id"]
            answers = [message for message in delivery.live() if message["id"] != status_id]
            assert len(answers) > 1
            assert answers[0]["reply_to"] == 50  # only the first answers the user
            assert all(answer["reply_to"] is None for answer in answers[1:])
            block = harness.commands_named("fork")[0]["new_id"]
            for answer in answers:
                assert store.lookup(-100, answer["id"]) == block
                assert quotes_in(answer)[0].collapsed is True
        finally:
            store.close()

    asyncio.run(scenario())


# --- the music tools ---------------------------------------------------------


def answer_music(answers):
    """A `delivery_ready` hook: the music bot replies to each command it is sent."""

    def prepare(delivery):
        def on_send(chat_id, text):
            if chat_id != MUSIC_BOT:
                return
            for prefix, message in answers.items():
                if text.startswith(prefix):
                    delivery.inbox.append(
                        bot_message(
                            id=message.get("id", 900),
                            chat_id=chat_id,
                            text=message.get("text", ""),
                            has_buttons=message.get("has_buttons", False),
                            has_music=message.get("has_music", False),
                        )
                    )

        delivery.on_send = on_send

    return prepare


def commands_to_bot(delivery) -> list[str]:
    return [message["text"] for message in delivery.sent if message["chat_id"] == MUSIC_BOT]


def test_a_search_reaches_the_agent_as_the_bots_listing(tmp_path):
    listing = "🎶 QQ Music search results\n1. 「明天，你好 (https://y.qq.com/x)」 - 牛奶咖啡"
    agent = ScriptedAgent(
        calls=[
            ("tg_search_music", {"keyword": "你好", "platform": "QQMusic"}),
            ("tg_draft_response", {"summary": "找到了", "details": "有这些版本。"}),
        ]
    )

    async def scenario():
        _, delivery, store = await run_bridge(
            agent,
            mention(),
            tmp_path,
            delivery_ready=answer_music({"/search": {"text": listing, "has_buttons": True}}),
        )
        try:
            assert commands_to_bot(delivery) == ["/search 你好 qq"]
            assert agent.answer_for("call_1")["result"] == listing
            # the draft still follows, and the listing is not sent to the chat
            replies = [message for message in delivery.sent if message["chat_id"] == -100]
            assert replies[-1]["text"] == "找到了\n\n有这些版本。"
        finally:
            store.close()

    asyncio.run(scenario())


def test_searching_falls_back_when_the_platform_is_not_offered(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_search_music", {"keyword": "你好", "platform": "Spotify"}),
            ("tg_draft_response", {"summary": "s", "details": "d"}),
        ]
    )

    async def scenario():
        _, delivery, store = await run_bridge(agent, mention(), tmp_path)
        try:
            assert commands_to_bot(delivery) == []
            assert "platform" in agent.answer_for("call_1")["error"]
        finally:
            store.close()

    asyncio.run(scenario())


def test_sending_music_forwards_the_file_into_the_chat(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_send_music", {"url": "https://y.qq.com/x", "platform": "AppleMusic"}),
            ("tg_draft_response", {"summary": "发好了", "details": "听听看。"}),
        ]
    )

    async def scenario():
        harness, delivery, store = await run_bridge(
            agent,
            mention(),
            tmp_path,
            delivery_ready=answer_music({"/music": {"id": 77, "has_music": True}}),
        )
        try:
            assert commands_to_bot(delivery) == ["/music https://y.qq.com/x am"]
            assert delivery.forwarded == [
                {"id": delivery.forwarded[0]["id"], "from": MUSIC_BOT, "message_id": 77, "to": -100}
            ]
            assert "sent to the chat" in agent.answer_for("call_1")["result"]
            # the forwarded track belongs to the block too, so a reply to it
            # continues this conversation
            forwarded_id = delivery.forwarded[0]["id"]
            assert store.lookup(-100, forwarded_id) == harness.commands_named("fork")[0]["new_id"]
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_refusal_from_the_music_bot_comes_back_as_the_result(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_send_music", {"url": "https://x/y", "platform": "NetEase"}),
            ("tg_draft_response", {"summary": "s", "details": "d"}),
        ]
    )

    async def scenario():
        _, delivery, store = await run_bridge(
            agent,
            mention(),
            tmp_path,
            delivery_ready=answer_music({"/music": {"text": "fail: no such track"}}),
        )
        try:
            assert commands_to_bot(delivery) == ["/music https://x/y 163"]
            assert delivery.forwarded == []
            assert agent.answer_for("call_1")["result"] == "fail: no such track"
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_failed_turn_takes_the_music_back_with_the_reply(tmp_path):
    agent = ScriptedAgent(
        fail=True,
        calls=[
            ("tg_send_music", {"url": "https://y.qq.com/x", "platform": "Soda"}),
            ("tg_draft_response", {"summary": "s", "details": "d"}),
        ],
    )

    async def scenario():
        _, delivery, store = await run_bridge(
            agent,
            mention(),
            tmp_path,
            delivery_ready=answer_music({"/music": {"id": 77, "has_music": True}}),
        )
        try:
            forwarded_id = delivery.forwarded[0]["id"]
            reply_id = delivery.sent[-2]["id"]  # the draft, just before the notice
            assert (-100, (forwarded_id,)) in delivery.deleted
            assert (-100, (reply_id,)) in delivery.deleted
            # nothing of the turn is left to reply to
            assert store.lookup(-100, forwarded_id) is None
            assert store.lookup(-100, reply_id) is None
            assert "agent failed" in delivery.sent[-1]["text"]
        finally:
            store.close()

    asyncio.run(scenario())


def test_the_status_message_names_the_music_tool(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_search_music", {"keyword": "你好", "platform": "NetEase"}),
            ("tg_draft_response", {"summary": "s", "details": "d"}),
        ]
    )

    async def scenario():
        _, delivery, store = await run_bridge(
            agent,
            mention(),
            tmp_path,
            delivery_ready=answer_music({"/search": {"text": "🎶 结果", "has_buttons": True}}),
        )
        try:
            assert any("tg_search_music" in edit["text"] for edit in delivery.edits)
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_music_failure_reaches_the_agent_as_a_tool_error(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_search_music", {"keyword": "你好", "platform": "NetEase"}),
            ("tg_draft_response", {"summary": "s", "details": "d"}),
        ]
    )

    async def scenario():
        _, delivery, store = await run_bridge(agent, mention(), tmp_path)  # the bot never answers
        try:
            assert agent.outcomes["call_1"] is False
            assert "said nothing within" in agent.answer_for("call_1")["error"]
            # a failed tool does not end the turn: the draft still went out
            replies = [message for message in delivery.sent if message["chat_id"] == -100]
            assert replies[-1]["text"] == "s\n\nd"
            assert any("❌ tg_search_music failed" in edit["text"] for edit in delivery.edits)
        finally:
            store.close()

    asyncio.run(scenario())


def test_the_music_rate_limit_is_shared_by_the_whole_userbot(tmp_path):
    """One gate per userbot, not one per conversation."""
    agent = ScriptedAgent(
        calls=[
            ("tg_search_music", {"keyword": "你好", "platform": "NetEase"}),
            ("tg_search_music", {"keyword": "晚安", "platform": "Soda"}),
            ("tg_draft_response", {"summary": "s", "details": "d"}),
        ]
    )
    asked: list[float] = []

    def ready(delivery):
        def on_send(chat_id, text):
            if chat_id != MUSIC_BOT:
                return
            asked.append(time.monotonic())
            delivery.inbox.append(bot_message(chat_id=chat_id, text="🎶 结果", has_buttons=True))

        delivery.on_send = on_send

    async def scenario():
        _, delivery, store = await run_bridge(
            agent,
            mention(),
            tmp_path,
            delivery_ready=ready,
            music_gate=Gate(gap=0.03),
        )
        try:
            assert len(asked) == 2
            assert asked[1] - asked[0] >= 0.03  # the second waited for the gate
            assert commands_to_bot(delivery) == ["/search 你好 163", "/search 晚安 qs"]
        finally:
            store.close()

    asyncio.run(scenario())


def test_parsed_content_is_forwarded_and_stays_repliable(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_send_parsed_content", {"url": "https://v.douyin.com/xyz"}),
            ("tg_draft_response", {"summary": "发好了", "details": "看上面。"}),
        ]
    )

    def ready(delivery):
        def on_send(chat_id, text):
            if chat_id != PARSE_BOT:
                return
            delivery.inbox.append(
                bot_message(
                    id=88,
                    chat_id=chat_id,
                    reply_to=delivery.sent[-1]["id"],
                    text="解析结果",
                    has_link=True,
                )
            )

        delivery.on_send = on_send

    async def scenario():
        harness, delivery, store = await run_bridge(
            agent, mention(), tmp_path, delivery_ready=ready
        )
        try:
            assert [
                message["text"] for message in delivery.sent if message["chat_id"] == PARSE_BOT
            ] == ["https://v.douyin.com/xyz"]
            assert delivery.forwarded[-1]["to"] == -100
            assert "sent to the chat" in agent.answer_for("call_1")["result"]
            # the forwarded content is the userbot's message: replying continues here
            forwarded_id = delivery.forwarded[0]["id"]
            assert store.lookup(-100, forwarded_id) == harness.commands_named("fork")[0]["new_id"]
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_parse_that_never_answers_reaches_the_agent_as_an_error(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_send_parsed_content", {"url": "https://v.douyin.com/xyz"}),
            ("tg_draft_response", {"summary": "s", "details": "d"}),
        ]
    )

    async def scenario():
        _, delivery, store = await run_bridge(agent, mention(), tmp_path)  # the bot never replies
        try:
            assert agent.outcomes["call_1"] is False
            assert "said nothing within" in agent.answer_for("call_1")["error"]
            assert delivery.forwarded == []
        finally:
            store.close()

    asyncio.run(scenario())


def test_the_agent_can_read_the_chat_it_is_in(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_view_current_chat", {}),
            ("tg_draft_response", {"summary": "看了", "details": "你们在聊天气。"}),
        ]
    )

    def ready(delivery):
        delivery.chats[-100] = chat_view(
            title="测试群",
            username="testgroup",
            chat_id=-100,
            messages=[chat_message(text="今天天气不错")],
        )

    async def scenario():
        _, _, store = await run_bridge(agent, mention(), tmp_path, delivery_ready=ready)
        try:
            result = agent.answer_for("call_1")["result"]
            assert "the last 1 message(s) in 测试群 (@testgroup), -100" in result
            assert "今天天气不错" in result
            assert agent.outcomes["call_1"] is True
        finally:
            store.close()

    asyncio.run(scenario())


def test_an_unreadable_public_chat_reaches_the_agent_as_an_error(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_view_public_chat", {"username": "@nobody"}),
            ("tg_draft_response", {"summary": "s", "details": "d"}),
        ]
    )

    async def scenario():
        _, _, store = await run_bridge(agent, mention(), tmp_path)
        try:
            assert agent.outcomes["call_1"] is False
            assert "could not read that chat" in agent.answer_for("call_1")["error"]
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_username_that_is_not_one_is_refused_before_any_lookup(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_view_public_chat", {"username": "testgroup"}),
            ("tg_draft_response", {"summary": "s", "details": "d"}),
        ]
    )

    async def scenario():
        _, delivery, store = await run_bridge(agent, mention(), tmp_path)
        try:
            # refused before the lookup: the chat was never asked for, so the fake
            # (which knows no chats here) was never consulted
            assert "must start with @" in agent.answer_for("call_1")["error"]
            assert delivery.chats == {}
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_message_can_be_read_and_forwarded_by_id(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_read_message", {"message_id": 41}),
            ("tg_forward_message", {"message_id": 41}),
            ("tg_draft_response", {"summary": "找到了", "details": "就是上面那条。"}),
        ]
    )

    def ready(delivery):
        delivery.chats[-100] = chat_view(
            chat_id=-100, messages=[chat_message(id=41, text="很久以前的消息")]
        )

    async def scenario():
        harness, delivery, store = await run_bridge(
            agent, mention(), tmp_path, delivery_ready=ready
        )
        try:
            read = agent.answer_for("call_1")["result"]
            assert "[41] " in read and "很久以前的消息" in read
            assert "forwarded into this chat" in agent.answer_for("call_2")["result"]
            assert delivery.forwarded[-1]["to"] == -100
            # the forwarded copy is the userbot's message: replying continues here
            forwarded_id = delivery.forwarded[0]["id"]
            assert store.lookup(-100, forwarded_id) == harness.commands_named("fork")[0]["new_id"]
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_message_id_that_makes_no_sense_is_refused(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_read_message", {"message_id": "not a number"}),
            ("tg_draft_response", {"summary": "s", "details": "d"}),
        ]
    )

    async def scenario():
        _, delivery, store = await run_bridge(agent, mention(), tmp_path)
        try:
            assert agent.outcomes["call_1"] is False
            assert "must be a number" in agent.answer_for("call_1")["error"]
            assert delivery.forwarded == []
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_message_from_another_chat_can_be_forwarded_here(tmp_path):
    """The failure that started this: an id that belongs to another chat."""
    agent = ScriptedAgent(
        calls=[
            ("tg_forward_message", {"message_id": 16193, "from_chat": "@elsewhere"}),
            ("tg_draft_response", {"summary": "转过来了", "details": "看上面。"}),
        ]
    )

    def ready(delivery):
        delivery.chats["@elsewhere"] = chat_view(
            title="别的群",
            username="elsewhere",
            chat_id=-200,
            messages=[chat_message(id=16193, text="那边的一条消息")],
        )

    async def scenario():
        harness, delivery, store = await run_bridge(
            agent, mention(), tmp_path, delivery_ready=ready
        )
        try:
            assert agent.outcomes["call_1"] is True
            assert delivery.forwarded == [
                {
                    "id": delivery.forwarded[0]["id"],
                    "from": "@elsewhere",
                    "message_id": 16193,
                    "to": -100,
                }
            ]
            forwarded_id = delivery.forwarded[0]["id"]
            assert store.lookup(-100, forwarded_id) == harness.commands_named("fork")[0]["new_id"]
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_bad_source_chat_is_refused_before_any_lookup(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_forward_message", {"message_id": 16193, "from_chat": "elsewhere"}),
            ("tg_draft_response", {"summary": "s", "details": "d"}),
        ]
    )

    async def scenario():
        _, delivery, store = await run_bridge(agent, mention(), tmp_path)
        try:
            assert agent.outcomes["call_1"] is False
            assert "must be a chat name" in agent.answer_for("call_1")["error"]
            assert delivery.forwarded == []
        finally:
            store.close()

    asyncio.run(scenario())


def test_the_pictures_of_what_is_asked_travel_with_the_fork(tmp_path):
    agent = ScriptedAgent()
    quoted = Quoted(
        content=Content(text="看这个"),
        sender_name="小红",
        sender_id=9,
        images=["data:image/jpeg;base64,quoted"],
    )

    async def scenario():
        message = reply_to(to=999, text="这是什么？", quoted=quoted)
        message.mentioned = True
        message.images = ["data:image/jpeg;base64,current"]
        harness, _, store = await run_bridge(agent, message, tmp_path)
        try:
            fork = harness.commands_named("fork")[0]
            # the quoted picture first, then the message's own — the order the
            # prompt reads in
            assert fork["images"] == [
                "data:image/jpeg;base64,quoted",
                "data:image/jpeg;base64,current",
            ]
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_harness_that_does_not_know_images_gets_none(tmp_path):
    agent = ScriptedAgent()

    async def scenario():
        message = mention()
        message.images = ["data:image/jpeg;base64,current"]
        harness = await FakeHarness(agent, protocol=2).start()
        client = HHClient(harness.host, harness.port)
        await client.connect()
        store = MappingStore(str(tmp_path / "mappings.db"))
        try:
            await Bridge(client, store, FakeTelegram(), status_interval=0.0).handle(message)
            assert "images" not in harness.commands_named("fork")[0]
        finally:
            await client.close()
            await harness.stop()
            store.close()

    asyncio.run(scenario())


def test_a_read_message_hands_its_pictures_to_the_model(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_read_message", {"message_id": 41}),
            ("tg_draft_response", {"summary": "看了", "details": "是一只猫。"}),
        ]
    )

    def ready(delivery):
        delivery.chats[-100] = chat_view(
            chat_id=-100,
            messages=[chat_message(id=41, text="看这个", images=["data:image/jpeg;base64,cat"])],
        )

    async def scenario():
        _, _, store = await run_bridge(agent, mention(), tmp_path, delivery_ready=ready)
        try:
            answer = agent.answer_for("call_1")
            assert answer["images"] == ["data:image/jpeg;base64,cat"]
            assert "看这个" in answer["result"]
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_failed_read_answers_without_pictures(tmp_path):
    agent = ScriptedAgent(
        calls=[
            ("tg_read_message", {"message_id": 999}),
            ("tg_draft_response", {"summary": "s", "details": "d"}),
        ]
    )

    def ready(delivery):
        delivery.chats[-100] = chat_view(chat_id=-100)

    async def scenario():
        _, _, store = await run_bridge(agent, mention(), tmp_path, delivery_ready=ready)
        try:
            answer = agent.answer_for("call_1")
            assert "images" not in answer and "error" in answer
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_mention_in_a_draft_goes_out_without_pinging_anyone(tmp_path):
    agent = ScriptedAgent(summary="回复 @小明", details="也可以问 **@channel**")

    async def scenario():
        _, delivery, store = await run_bridge(agent, mention(), tmp_path)
        try:
            reply = [one for one in delivery.sent if one["chat_id"] == -100][-1]
            assert reply["text"] == "回复 #小明\n\n也可以问 #channel"
            assert bold_of(reply) == ["#channel"]
        finally:
            store.close()

    asyncio.run(scenario())

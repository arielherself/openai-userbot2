"""The bridge: what a mention and a reply each turn into."""

from __future__ import annotations

import asyncio

from support import FakeHarness, FakeTelegram, bold_of, quotes_in, text_of

from userbot.bridge import Bridge, Identity, Incoming
from userbot.content import Content, Quoted
from userbot.harness import HHClient
from userbot.store import MappingStore


class ScriptedAgent:
    """A harness that replies like the real one would, draft tool call included."""

    def __init__(
        self,
        summary: str = "总结",
        details: str = "正文",
        fail: bool = False,
        draft: bool = True,
        missing_details: bool = False,
        missing_parent: str | None = None,
        quiet: bool = False,
    ) -> None:
        self.summary, self.details = summary, details
        self.fail, self.draft = fail, draft
        self.missing_details = missing_details
        self.missing_parent = missing_parent
        self.quiet = quiet  # never emits a delta: the turn produces nothing
        self.answered = asyncio.Event()
        self.undone = asyncio.Event()
        self.resolved: list[dict] = []

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
                local_tools=[],
                state_namespaces=[],
            )
            await self._done(conn, command)
        elif name == "run":
            asyncio.create_task(self._turn(conn, command))
        elif name == "resolve_tool":
            self.resolved.append(command)
            await conn.emit(
                event="local_tool_answered",
                rid=command["rid"],
                agent_id=command["id"],
                call_id=command["call_id"],
                ok="error" not in command,
                result_chars=0,
            )
            await self._done(conn, command)
            (self.answered if command["call_id"] == "call_1" else self.undone).set()

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
        if self.draft:
            arguments = {"summary": self.summary}
            if not self.missing_details:
                arguments["details"] = self.details
            await conn.emit(
                event="tool_call_requested",
                agent_id=block,
                round=1,
                call_id="call_1",
                name="tg_draft_response",
                raw_arguments="{}",
                known=True,
            )
            await conn.emit(
                event="local_tool_called",
                agent_id=block,
                round=1,
                call_id="call_1",
                name="tg_draft_response",
                kind="call",
                arguments=arguments,
                raw_arguments="{}",
                timeout_ms=120_000,
            )
            await asyncio.wait_for(self.answered.wait(), 3)
            await conn.emit(
                event="tool_call_finished",
                agent_id=block,
                round=1,
                call_id="call_1",
                name="tg_draft_response",
                ok=True,
                error=None,
                result="delivered",
                result_chars=9,
                elapsed_ms=1.0,
            )
        if self.fail:
            if self.draft:
                await conn.emit(
                    event="rollback_started",
                    call_id="call_1",
                    tool="tg_draft_response",
                    index=1,
                    total=1,
                )
                await conn.emit(
                    event="local_tool_rollback",
                    agent_id=block,
                    call_id="call_1:rollback",
                    name="tg_draft_response",
                    kind="rollback",
                    rollback_of="call_1",
                    arguments={},
                    result="delivered",
                    call_ok=True,
                    timeout_ms=120_000,
                )
                await asyncio.wait_for(self.undone.wait(), 3)
                await conn.emit(
                    event="rollback_finished",
                    call_id="call_1",
                    tool="tg_draft_response",
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
                tool_calls=1 if self.draft else 0,
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


async def run_bridge(agent, message, tmp_path, store_ready=None, identity=None):
    """Drive one message through the bridge; returns everything to assert on."""
    harness = await FakeHarness(agent).start()
    client = HHClient(harness.host, harness.port)
    await client.connect()
    delivery = FakeTelegram()
    store = MappingStore(str(tmp_path / "mappings.db"))
    if store_ready:
        store_ready(store)
    bridge = Bridge(client, store, delivery, status_interval=0.0, identity=identity)
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
            assert [tool["name"] for tool in created["local_tools"]] == ["tg_draft_response"]
            assert created["local_tools"][0]["rollback"] is True

            fork = harness.commands_named("fork")[0]
            assert "小明 (@ming, user id 7)" in fork["prompt"]
            assert "测试群 (@testgroup, group id -100)" in fork["prompt"]
            assert "你好 @bot" in fork["prompt"]

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

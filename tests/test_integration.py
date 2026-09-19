"""End to end against the real harness: no Telegram, no network.

The harness's own test-suite is borrowed here — its scripted provider speaks real
HTTP + SSE, and its fixture runs a real `HHServer` on an ephemeral port — so this
exercises the real protocol rather than a stand-in: the local tool parks the turn
for real, the fork really rewinds, and a failed turn really asks for its undo.

Skipped when there is no harness checkout to point at (see HH_TEST_PROJECT).
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import pathlib
import socket
import sys
from datetime import datetime, timedelta

import pytest
from support import FakeTelegram, bold_of, quotes_in, serving, text_of

from userbot.bridge import Bridge, Identity, Incoming
from userbot.content import Content, Quoted
from userbot.harness import HHClient, spawn_server
from userbot.schedule import run_scheduler
from userbot.store import MappingStore

pytestmark = pytest.mark.integration

CHAT = -1001234567890

PROJECT = pathlib.Path(os.environ.get("HH_TEST_PROJECT", "~/headless-harness")).expanduser()


def _load_harness_support():
    """The harness's test infrastructure, under a module name of its own."""
    if not (PROJECT / "src" / "server.py").exists():
        pytest.skip(
            f"no headless-harness checkout at {PROJECT} (set HH_TEST_PROJECT)",
            allow_module_level=True,
        )
    sys.path.insert(0, str(PROJECT / "tests"))
    spec = importlib.util.spec_from_file_location(
        "harness_support", PROJECT / "tests" / "support.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["harness_support"] = module
    spec.loader.exec_module(module)
    return module


harness_support = _load_harness_support()


class Fixture:
    """A scripted provider and a real HHServer, torn down together."""

    def __init__(self, tmp_path):
        self.provider = harness_support.FakeProvider()
        self.server = harness_support.ServerFixture(str(tmp_path), self.provider)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.server.stop()
        self.provider.close()

    def blocks(self) -> dict:
        """Every block the server holds, by id — asked over its own protocol."""
        with self.server.client() as client:
            client.command("list_agents")
            listed = client.wait_event("agents_listed")
        return {block["agent_id"]: block for block in listed["agents"]}


async def drive(fixture, store, conversation, delivery=None) -> FakeTelegram:
    """Run messages through the bridge, against the real server."""
    delivery = FakeTelegram() if delivery is None else delivery
    harness = HHClient(fixture.server.host, fixture.server.port)
    await harness.connect()
    try:
        bridge = Bridge(
            harness,
            store,
            delivery,
            status_interval=0.0,
            identity=Identity(name="小助手", username="mybot", user_id=4242),
        )
        for message in conversation:
            await bridge.handle(message)
    finally:
        await harness.close()
    return delivery


def mention(message_id=50, text="帮我查一下 @bot", media="") -> Incoming:
    return Incoming(
        chat_id=CHAT,
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


def reply_to(message_id=51, to=1000, text="再详细一点", quoted=None) -> Incoming:
    return Incoming(
        chat_id=CHAT,
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


def test_a_draft_round_trip_and_the_reply_that_follows_it(tmp_path):
    with Fixture(tmp_path) as fixture:
        store = MappingStore(str(tmp_path / "mappings.db"))
        try:
            fixture.provider.tool_call(
                "tg_draft_response",
                {"summary": "答案是 42", "details": "细节里还有 **粗体**。"},
            )
            fixture.provider.text("已经回答了。")
            delivery = asyncio.run(drive(fixture, store, [mention()]))

            status, answer = delivery.sent[0], delivery.sent[1]
            assert status["reply_to"] == 50
            assert delivery.deleted == [(CHAT, (status["id"],))]
            assert text_of(answer) == "细节里还有 粗体。"
            assert bold_of(answer) == ["粗体"]
            assert quotes_in(answer)[0].collapsed is True

            first = store.lookup(CHAT, answer["id"])
            assert first is not None
            blocks = fixture.blocks()
            assert blocks[first]["outcome"] == "ok"
            local = blocks[first]["local_tools"]
            names = [item["name"] if isinstance(item, dict) else item for item in local]
            # the server keeps them in name order, whatever order we declared them
            assert sorted(names) == [
                "curl_to_sandbox",
                "git_clone_to_sandbox",
                "tg_add_schedule",
                "tg_download_file_to_sandbox",
                "tg_draft_response",
                "tg_forward_message",
                "tg_read_message",
                "tg_remove_schedule",
                "tg_search_music",
                "tg_send_file",
                "tg_send_file_from_sandbox",
                "tg_send_music",
                "tg_send_parsed_content",
                "tg_view_current_chat",
                "tg_view_public_chat",
                "tg_view_schedule",
            ]

            # replying to the answer joins that conversation instead of starting one
            fixture.provider.tool_call(
                "tg_draft_response", {"summary": "补充", "details": "补充的细节"}
            )
            fixture.provider.text("补完了。")
            follow_up = asyncio.run(drive(fixture, store, [reply_to(to=answer["id"])]))
            assert follow_up.sent[1]["reply_to"] == 51
            second = store.lookup(CHAT, follow_up.sent[1]["id"])
            blocks = fixture.blocks()
            assert blocks[second]["parent"] == first
            # the fork carried the tools, so the continuing turn has them too
            names = [
                item["name"] if isinstance(item, dict) else item
                for item in blocks[second]["local_tools"]
            ]
            assert sorted(names) == [
                "curl_to_sandbox",
                "git_clone_to_sandbox",
                "tg_add_schedule",
                "tg_download_file_to_sandbox",
                "tg_draft_response",
                "tg_forward_message",
                "tg_read_message",
                "tg_remove_schedule",
                "tg_search_music",
                "tg_send_file",
                "tg_send_file_from_sandbox",
                "tg_send_music",
                "tg_send_parsed_content",
                "tg_view_current_chat",
                "tg_view_public_chat",
                "tg_view_schedule",
            ]
            # it carries the whole exchange: the prompt, the tool call, the answer
            assert blocks[second]["context_len"] > blocks[first]["context_len"]
        finally:
            store.close()


def test_a_telegram_file_is_piped_into_a_sandbox_without_being_quoted(tmp_path):
    """The point of the pipe: the bytes reach the sandbox, not the transcript."""
    with Fixture(tmp_path) as fixture:
        store = MappingStore(str(tmp_path / "mappings.db"))
        try:
            payload = b"%PDF-1.4 the file's own bytes"
            fixture.provider.tool_call(
                "tg_download_file_to_sandbox",
                {
                    "chat_id": CHAT,
                    "message_id": 41,
                    "sandbox_id": "sbx-nosuch",
                    "path": "/workspace/report.pdf",
                },
            )
            fixture.provider.tool_call(
                "tg_draft_response", {"summary": "拿到了", "details": "放进沙箱了。"}
            )
            fixture.provider.text("放好了。")

            delivery = FakeTelegram()
            delivery.files[(CHAT, 41)] = payload
            asyncio.run(drive(fixture, store, [mention()], delivery))

            assert delivery.live()[-1]["text"] == "拿到了\n\n放进沙箱了。"
            # every request the provider saw, as one blob to search
            sent = "\n".join(
                json.dumps(request, ensure_ascii=False) for request in fixture.provider.payloads()
            )
            # the model was shown the pipe's own ends: both names, and what the
            # sandbox tool said about the id it was handed
            assert "[tool pipe] tg_download_file_to_sandbox -> nix_add_file" in sent
            assert "Sandbox sbx-nosuch is not live" in sent
            # and the bytes themselves were never part of any request
            assert base64.b64encode(payload).decode("ascii") not in sent
        finally:
            store.close()


def test_a_sandbox_file_is_piped_out_toward_the_chat(tmp_path):
    """The pipe the other way: our call names nix_cat_file, and it does the read."""
    with Fixture(tmp_path) as fixture:
        store = MappingStore(str(tmp_path / "mappings.db"))
        try:
            fixture.provider.tool_call(
                "tg_send_file_from_sandbox",
                {"sandbox_id": "sbx-nosuch", "path": "/workspace/chart.png"},
            )
            fixture.provider.tool_call(
                "tg_draft_response", {"summary": "没发出去", "details": "那个沙箱已经不在了。"}
            )
            fixture.provider.text("看过了。")

            delivery = FakeTelegram()
            asyncio.run(drive(fixture, store, [mention()], delivery))

            assert delivery.live()[-1]["text"] == "没发出去\n\n那个沙箱已经不在了。"
            # the real harness took the call we answered with, resolved
            # tg_send_file as nix_cat_file's target — the arity it demands, the
            # client it hands the file to next — and ran the read itself
            sent = "\n".join(
                json.dumps(request, ensure_ascii=False) for request in fixture.provider.payloads()
            )
            assert "[tool pipe] tg_send_file_from_sandbox -> nix_cat_file" in sent
            assert "Sandbox sbx-nosuch is not live" in sent
            # a file that was never read reached nobody
            assert delivery.files_sent == []
        finally:
            store.close()


def test_a_fetched_url_is_piped_into_a_sandbox_without_being_quoted(tmp_path):
    """The same pipe, driven by the real curl and a real local server."""
    payload = b"%PDF-1.4 fetched over http"
    with Fixture(tmp_path) as fixture, serving(payload) as url:
        store = MappingStore(str(tmp_path / "mappings.db"))
        try:
            fixture.provider.tool_call(
                "curl_to_sandbox",
                {"url": url, "sandbox_id": "sbx-nosuch", "path": "/workspace/report.pdf"},
            )
            fixture.provider.tool_call(
                "tg_draft_response", {"summary": "拿到了", "details": "放进沙箱了。"}
            )
            fixture.provider.text("放好了。")
            delivery = asyncio.run(drive(fixture, store, [mention()]))

            assert delivery.live()[-1]["text"] == "拿到了\n\n放进沙箱了。"
            sent = "\n".join(
                json.dumps(request, ensure_ascii=False) for request in fixture.provider.payloads()
            )
            assert "[tool pipe] curl_to_sandbox -> nix_add_file" in sent
            assert "Sandbox sbx-nosuch is not live" in sent
            # the bytes went to the sandbox, never to the provider
            assert base64.b64encode(payload).decode("ascii") not in sent
        finally:
            store.close()


def test_a_failed_turn_reports_itself_and_takes_the_reply_back(tmp_path):
    with Fixture(tmp_path) as fixture:
        store = MappingStore(str(tmp_path / "mappings.db"))
        try:
            fixture.provider.tool_call(
                "tg_draft_response", {"summary": "先发出去", "details": "这段会被撤回"}
            )
            fixture.provider.error(status=500, body=b"the provider fell over")
            delivery = asyncio.run(drive(fixture, store, [mention()]))

            answer = delivery.sent[1]
            # the failed turn undid its own tool call: the messages went away again
            assert (CHAT, (answer["id"],)) in delivery.deleted
            assert "这段会被撤回" in answer["text"]

            live = [
                message for message in delivery.live() if message["id"] != delivery.sent[0]["id"]
            ]
            failure = live[-1]
            assert "agent failed" in failure["text"]
            assert failure["reply_to"] == 50
            assert store.lookup(CHAT, failure["id"]) is not None
        finally:
            store.close()


def test_the_quoted_message_reaches_the_provider(tmp_path):
    """What the agent is asked is written into the request the provider really gets."""
    with Fixture(tmp_path) as fixture:
        store = MappingStore(str(tmp_path / "mappings.db"))
        try:
            fixture.provider.tool_call(
                "tg_draft_response", {"summary": "看了", "details": "是一份 PDF。"}
            )
            fixture.provider.text("已回复。")
            message = reply_to(
                to=999,
                text="这是什么？",
                quoted=Quoted(
                    content=Content(
                        text="看这个",
                        media=(
                            "[file: report.pdf (application/pdf, 1.2 MB)]\n"
                            "[instant view: https://t.me/iv?url=https%3A%2F%2Fexample.com&rhash=ab]"
                        ),
                    ),
                    sender_name="小红",
                    sender_id=9,
                    sender_username="hong",
                ),
            )
            message.mentioned = True
            asyncio.run(drive(fixture, store, [message]))

            sent = json.dumps(fixture.provider.payloads()[0], ensure_ascii=False)
            assert "on behalf of the userbot 小助手 (@mybot, user id 4242)" in sent
            assert "replying to 小红 (@hong, user id 9)" in sent
            assert "看这个" in sent
            assert "report.pdf" in sent
            assert "instant view" in sent
            assert "这是什么？" in sent
        finally:
            store.close()


def test_a_turn_that_never_says_anything_leaves_no_status_message(tmp_path):
    """The status message waits for the harness to produce something."""
    with Fixture(tmp_path) as fixture:
        store = MappingStore(str(tmp_path / "mappings.db"))
        try:
            fixture.provider.error(status=500, body=b"the provider fell over")
            delivery = asyncio.run(drive(fixture, store, [mention()]))

            assert len(delivery.sent) == 1  # just the failure notice
            assert delivery.sent[0]["reply_to"] == 50
            assert "agent failed" in delivery.sent[0]["text"]
            assert delivery.edits == []
        finally:
            store.close()


def test_a_reply_to_a_message_we_never_sent_is_ignored(tmp_path):
    with Fixture(tmp_path) as fixture:
        store = MappingStore(str(tmp_path / "mappings.db"))
        try:
            delivery = asyncio.run(drive(fixture, store, [reply_to(to=999)]))
            assert delivery.sent == []
            assert fixture.provider.count == 0
        finally:
            store.close()


def local_at(seconds_from_now: float) -> str:
    """A local time the way the model writes one."""
    when = datetime.now().astimezone() + timedelta(seconds=seconds_from_now)
    return when.strftime("%Y-%m-%d %H:%M:%S")


async def clock_until(fixture, store, delivery, ready) -> None:
    """Run the schedule clock against the real server until `ready()` says done."""
    harness = HHClient(fixture.server.host, fixture.server.port)
    await harness.connect()
    tasks: set[asyncio.Task] = set()
    bridge = Bridge(
        harness,
        store,
        delivery,
        status_interval=0.0,
        identity=Identity(name="小助手", username="mybot", user_id=4242),
    )
    clock = asyncio.create_task(run_scheduler(bridge, store, tasks))
    try:
        for _ in range(1000):
            if ready():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the clock never fired the task")
    finally:
        clock.cancel()
        try:
            await clock
        except asyncio.CancelledError:
            pass
        await asyncio.gather(*tasks, return_exceptions=True)
        await harness.close()


def test_a_task_is_scheduled_and_its_time_comes(tmp_path):
    with Fixture(tmp_path) as fixture:
        store = MappingStore(str(tmp_path / "mappings.db"))
        try:
            fixture.provider.tool_call(
                "tg_add_schedule", {"content": "提醒用户喝水", "at": local_at(1)}
            )
            fixture.provider.tool_call(
                "tg_draft_response", {"summary": "好的", "details": "稍后提醒你。"}
            )
            fixture.provider.text("安排好了。")
            delivery = asyncio.run(drive(fixture, store, [mention()]))

            (task,) = store.schedules()
            assert task.content == "提醒用户喝水"
            # its answer belongs where the asking happened
            assert task.chat_id == CHAT and task.message_id == 50
            assert delivery.live()[-1]["text"] == "好的\n\n稍后提醒你。"

            # when its time comes the clock runs it, and the answer goes there
            fixture.provider.tool_call(
                "tg_draft_response", {"summary": "该喝水了", "details": "记得喝水。"}
            )
            fixture.provider.text("提醒完了。")
            answer_text = "该喝水了\n\n记得喝水。"
            asyncio.run(
                clock_until(
                    fixture,
                    store,
                    delivery,
                    lambda: any(message["text"] == answer_text for message in delivery.sent),
                )
            )

            assert store.schedules() == []  # it fired once, and is gone
            answer = [m for m in delivery.sent if m["text"] == answer_text][-1]
            assert answer["chat_id"] == CHAT and answer["reply_to"] == 50
            # what the agent was given is a task, not a message from anyone
            sent = "\n".join(
                json.dumps(request, ensure_ascii=False) for request in fixture.provider.payloads()
            )
            assert "a scheduled task you set earlier is due now" in sent
            assert "task:\\n提醒用户喝水" in sent  # the JSON blob escapes the newline
        finally:
            store.close()


def test_a_failed_turn_takes_a_scheduled_task_back(tmp_path):
    with Fixture(tmp_path) as fixture:
        store = MappingStore(str(tmp_path / "mappings.db"))
        try:
            fixture.provider.tool_call(
                "tg_add_schedule", {"content": "提醒用户喝水", "at": local_at(600)}
            )
            fixture.provider.tool_call(
                "tg_draft_response", {"summary": "先记下", "details": "这段会被撤回"}
            )
            fixture.provider.error(status=500, body=b"the provider fell over")
            delivery = asyncio.run(drive(fixture, store, [mention()]))

            # the failed turn undid its work: the reply went back, and so did the task
            assert (CHAT, (delivery.sent[1]["id"],)) in delivery.deleted
            assert store.schedules() == []
            failure = delivery.live()[-1]
            assert "agent failed" in failure["text"]
        finally:
            store.close()


def test_the_harness_can_be_started_for_us(tmp_path):
    """The path `--no-spawn` turns off: run `src/server.py`, wait for its port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    process = spawn_server("127.0.0.1", port, str(PROJECT), db=str(tmp_path / "harness.db"))

    async def scenario():
        client = HHClient("127.0.0.1", port)
        await client.connect()
        try:
            assert client.hello["protocol"] >= 3  # images ride on fork and resolve_tool
            assert client.hello["store"]["blocks"] == 0
        finally:
            await client.close()

    try:
        asyncio.run(scenario())
    finally:
        process.terminate()
        process.wait(timeout=10)

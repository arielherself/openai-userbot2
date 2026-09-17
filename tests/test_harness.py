"""The harness client: command correlation, routing, and local tools."""

from __future__ import annotations

import asyncio

import pytest
from support import FakeHarness

from userbot.harness import CONNECTION_LOST, HarnessError, HHClient


async def script(harness, conn, command):
    name, rid = command["command"], command["rid"]
    if name == "ping":
        await conn.emit(event="pong", rid=rid, echo=command.get("echo"))
    elif name == "slow":
        await asyncio.sleep(0.05)
        await conn.emit(event="slow_result", rid=rid)
    elif name == "boom":
        await conn.emit(event="error", rid=rid, command=name, code="bad_id", message="no id")
        return
    elif name == "touch":
        await conn.emit(event="agent_touched", agent_id=command["id"])
    await conn.emit(
        event="command_finished",
        rid=rid,
        command=name,
        target=command.get("id"),
        status="ok",
        elapsed_ms=1.0,
    )


def test_a_command_collects_its_own_events():
    async def scenario():
        harness = await FakeHarness(script).start()
        client = HHClient(harness.host, harness.port)
        await client.connect()
        try:
            assert client.hello["defaults"]["model"] == "fake-model"
            found = await client.command(command="ping", echo="hi")
            assert found["pong"]["echo"] == "hi"
            assert found["command_finished"]["status"] == "ok"
        finally:
            await client.close()
            await harness.stop()

    asyncio.run(scenario())


def test_two_commands_over_one_connection_keep_their_events():
    async def scenario():
        harness = await FakeHarness(script).start()
        client = HHClient(harness.host, harness.port)
        await client.connect()
        try:
            slow, ping = await asyncio.gather(
                client.command(command="slow"),
                client.command(command="ping", echo="hello"),
            )
            assert slow["command_finished"]["command"] == "slow"
            assert "slow_result" in slow
            assert ping["pong"]["echo"] == "hello"
        finally:
            await client.close()
            await harness.stop()

    asyncio.run(scenario())


def test_a_refused_command_raises_with_its_code():
    async def scenario():
        harness = await FakeHarness(script).start()
        client = HHClient(harness.host, harness.port)
        await client.connect()
        try:
            with pytest.raises(HarnessError) as caught:
                await client.command(command="boom")
            assert caught.value.code == "bad_id"
            assert "no id" in str(caught.value)
        finally:
            await client.close()
            await harness.stop()

    asyncio.run(scenario())


def test_events_are_routed_by_block_id_too():
    async def scenario():
        harness = await FakeHarness(script).start()
        client = HHClient(harness.host, harness.port)
        await client.connect()
        try:
            subscription = client.subscribe("agent:tg-1")
            await client.command(command="touch", id="tg-1")
            event = await subscription.get(timeout=1)
            assert event["event"] == "agent_touched"
            assert event["agent_id"] == "tg-1"
            subscription.close()
        finally:
            await client.close()
            await harness.stop()

    asyncio.run(scenario())


def test_answering_a_local_tool_call_sends_the_result():
    async def scenario():
        harness = await FakeHarness(script).start()
        client = HHClient(harness.host, harness.port)
        await client.connect()
        try:
            assert await client.resolve_tool("tg-1", "call_9", result="done") is True
            answered = harness.commands_named("resolve_tool")[0]
            assert answered["id"] == "tg-1"
            assert answered["call_id"] == "call_9"
            assert answered["result"] == "done"
            assert await client.resolve_tool("tg-1", "call_9", error="nope") is True
            assert harness.commands_named("resolve_tool")[1]["error"] == "nope"
        finally:
            await client.close()
            await harness.stop()

    asyncio.run(scenario())


def test_a_lost_connection_wakes_every_subscription():
    async def scenario():
        harness = await FakeHarness(script).start()
        client = HHClient(harness.host, harness.port)
        await client.connect()
        subscription = client.subscribe("agent:tg-1")
        await harness.stop()
        event = await subscription.get(timeout=2)
        assert event["event"] == CONNECTION_LOST
        with pytest.raises(HarnessError):
            await client.command(command="ping")
        await client.close()

    asyncio.run(scenario())

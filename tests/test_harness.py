"""The harness client: command correlation, routing, and local tools."""

from __future__ import annotations

import asyncio

import pytest
from support import FakeHarness

from userbot.harness import ADD_FILE_BYTES, CONNECTION_LOST, LINE_LIMIT, HarnessError, HHClient
from userbot.tools import MAX_DOWNLOAD_BYTES


def test_a_full_size_payload_fits_one_command_line():
    """The line limit is derived from what a sandbox takes, not guessed at.

    A file reaches a sandbox as base64 inside one command, so the longest line
    the two sides accept has to hold the biggest file `nix_add_file` takes —
    base64 makes it a third bigger again, plus the JSON around it. The server
    derives its own limit the same way, so the two cannot drift apart; and what
    one download may bring down stays under both.
    """
    assert LINE_LIMIT >= ADD_FILE_BYTES * 4 // 3 + 2 * 1024 * 1024
    assert MAX_DOWNLOAD_BYTES < ADD_FILE_BYTES < LINE_LIMIT


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


def test_a_connection_that_went_away_is_opened_again():
    async def scenario():
        harness = await FakeHarness(script).start()
        client = HHClient(harness.host, harness.port)
        await client.connect()
        try:
            first = await client.command(command="ping", echo="hello")
            assert first["pong"]["echo"] == "hello"
            await harness.drop()
            for _ in range(100):  # the reader notices the hang-up on its own
                if not client.connected:
                    break
                await asyncio.sleep(0.01)
            assert not client.connected
            # the next command opens a fresh link instead of failing on the old one
            again = await client.command(command="ping", echo="again")
            assert again["pong"]["echo"] == "again"
            assert len(harness.connections) == 2
        finally:
            await client.close()
            await harness.stop()

    asyncio.run(scenario())


def test_a_write_that_finds_the_socket_gone_is_made_again():
    async def scenario():
        harness = await FakeHarness(script).start()
        client = HHClient(harness.host, harness.port)
        await client.connect()
        try:
            # the socket looked alive until the write reached it — the way it is
            # when a peer vanishes without the reader having noticed yet
            writes, real = [], client._write

            async def failing_once(data):
                writes.append(data)
                if len(writes) == 1:
                    raise ConnectionResetError("the peer went away")
                await real(data)

            client._write = failing_once
            found = await client.command(command="ping", echo="again")
            assert found["pong"]["echo"] == "again"
            assert len(writes) == 2
            # and it went out over a fresh link, not the one that just failed
            assert len(harness.connections) == 2
        finally:
            await client.close()
            await harness.stop()

    asyncio.run(scenario())

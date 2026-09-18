"""The tool that carries a chat's file into a sandbox: its rules and its answer."""

from __future__ import annotations

import asyncio
import base64

from userbot.sandbox import ADD_FILE, download_to_sandbox, read_arguments, sandbox_path
from userbot.telegram import DownloadedFile


class FakeDelivery:
    """Just the `download` the tool asks for."""

    def __init__(self, file=None, fail: str = "") -> None:
        self.file = file
        self.fail = fail
        self.asked: list[tuple] = []

    async def download(self, chat_id, message_id):
        self.asked.append((chat_id, message_id))
        if self.fail:
            raise RuntimeError(self.fail)
        return self.file


def test_the_destination_is_a_file_under_the_workspace():
    assert sandbox_path("/workspace/report.pdf") == ("/workspace/report.pdf", None)
    assert sandbox_path("/workspace//a/b.bin") == ("/workspace/a/b.bin", None)
    # the workspace itself is a directory, not a file to write
    assert "under /workspace" in sandbox_path("/workspace")[1]
    assert "under /workspace" in sandbox_path("/etc/passwd")[1]
    assert "under /workspace" in sandbox_path("workspace/report.pdf")[1]
    # and nothing may walk out of it
    assert ".." in sandbox_path("/workspace/../etc/passwd")[1]
    assert "." in sandbox_path("/workspace/./report.pdf")[1]


def test_the_arguments_are_read_the_way_the_tools_write_them():
    arguments, complaint = read_arguments(
        {"chat_id": -100, "message_id": 41, "sandbox_id": "sbx-7f", "path": "/workspace//a.txt"}
    )
    assert complaint is None
    assert arguments == {
        "chat_id": -100,
        "message_id": 41,
        "sandbox_id": "sbx-7f",
        "path": "/workspace/a.txt",
    }
    # a chat named like a chat, and an id written as a string, are both fine
    assert (
        read_arguments(
            {
                "chat_id": "@elsewhere",
                "message_id": "41",
                "sandbox_id": "sbx-7f",
                "path": "/workspace/a",
            }
        )[0]["chat_id"]
        == "@elsewhere"
    )


def test_an_argument_that_makes_no_sense_is_refused():
    good = {"chat_id": -100, "message_id": 41, "sandbox_id": "sbx-7f", "path": "/workspace/a"}
    for field, value in (
        ("chat_id", ""),
        ("chat_id", "not a chat"),
        ("message_id", 0),
        ("message_id", "later"),
        ("sandbox_id", ""),
        ("sandbox_id", 7),
        ("path", ""),
        ("path", "/tmp/a"),
    ):
        arguments, complaint = read_arguments({**good, field: value})
        assert complaint and field in complaint, (field, value)
        assert arguments == {}


def test_the_file_is_piped_into_nix_add_file_as_its_bytes():
    async def scenario():
        data = b"%PDF-1.4 nope"
        delivery = FakeDelivery(DownloadedFile(name="report.pdf", data=data))
        answer = await download_to_sandbox(delivery, -100, 41, "sbx-7f", "/workspace/report.pdf")
        assert answer.error == ""
        assert answer.call["name"] == ADD_FILE
        assert answer.call["arguments"] == {
            "sandbox_id": "sbx-7f",
            "path": "/workspace/report.pdf",
            "content_base64": base64.b64encode(data).decode("ascii"),
        }
        # what the pipe records is that the download happened, not the bytes
        assert f"{len(data)} bytes of report.pdf" in answer.result
        assert delivery.asked == [(-100, 41)]

    asyncio.run(scenario())


def test_a_message_with_nothing_in_it_is_an_error_rather_than_a_pipe():
    async def scenario():
        answer = await download_to_sandbox(FakeDelivery(), -100, 41, "sbx-7f", "/workspace/x")
        assert "carries no file" in answer.error
        assert answer.call is None

    asyncio.run(scenario())


def test_a_download_telegram_refuses_is_an_error():
    async def scenario():
        answer = await download_to_sandbox(
            FakeDelivery(fail="telegram said no"), -100, 41, "sbx-7f", "/workspace/x"
        )
        assert "could not download message 41" in answer.error
        assert "telegram said no" in answer.error
        assert answer.call is None

    asyncio.run(scenario())

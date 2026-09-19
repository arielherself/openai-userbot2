"""The tool that carries a chat's file into a sandbox: its rules and its answer."""

from __future__ import annotations

import asyncio
import base64

from userbot.sandbox import (
    ADD_FILE,
    CAT_FILE,
    download_to_sandbox,
    export_arguments,
    file_name,
    read_arguments,
    sandbox_path,
    sandbox_read_path,
    send_arguments,
    send_file,
    send_from_sandbox,
)
from userbot.telegram import DownloadedFile


class FakeDelivery:
    """Just the two calls the tools ask for: a download, and a file out."""

    def __init__(self, file=None, fail: str = "") -> None:
        self.file = file
        self.fail = fail
        self.asked: list[tuple] = []
        self.files: list[dict] = []

    async def download(self, chat_id, message_id):
        self.asked.append((chat_id, message_id))
        if self.fail:
            raise RuntimeError(self.fail)
        return self.file

    async def send_file(self, chat_id, name, data):
        if self.fail:
            raise RuntimeError(self.fail)
        self.files.append({"chat_id": chat_id, "name": name, "data": data})
        return 500


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


# --- files out of a sandbox --------------------------------------------------


def test_the_file_to_read_out_is_a_plain_path_inside_the_sandbox():
    assert sandbox_read_path("/workspace//charts/x.png") == ("/workspace/charts/x.png", None)
    # a read may reach anywhere the sandbox can, not just its workspace
    assert sandbox_read_path("/nix/store/x/template.html") == ("/nix/store/x/template.html", None)
    assert "absolute" in sandbox_read_path("workspace/x.png")[1]
    # and nothing may walk out of the sandbox by naming its way there
    assert ".." in sandbox_read_path("/workspace/../etc/passwd")[1]
    assert "." in sandbox_read_path("/workspace/./x.png")[1]
    assert "name a file" in sandbox_read_path("/")[1]


def test_the_name_a_file_is_sent_under_is_the_end_of_its_path():
    assert file_name("/workspace/charts/x.png") == "x.png"
    assert file_name("x.png") == "x.png"


def test_the_arguments_of_the_outbound_tools_are_read_as_written():
    arguments, complaint = export_arguments({"sandbox_id": "sbx-7f", "path": "/workspace//x.png"})
    assert complaint is None
    assert arguments == {"sandbox_id": "sbx-7f", "path": "/workspace/x.png"}
    for field, value in (("sandbox_id", ""), ("path", ""), ("path", "x.png")):
        arguments, complaint = export_arguments(
            {"sandbox_id": "sbx-7f", "path": "/workspace/x.png", field: value}
        )
        assert complaint and field in complaint, (field, value)
        assert arguments == {}

    arguments, complaint = send_arguments({"path": "/workspace/x.png", "content": "AAEC"})
    assert complaint is None
    assert arguments == {"path": "/workspace/x.png", "content": "AAEC"}
    for field, value in (("path", ""), ("path", "/workspace/"), ("content", "")):
        arguments, complaint = send_arguments(
            {"path": "/workspace/x.png", "content": "AAEC", field: value}
        )
        assert complaint and field in complaint, (field, value)
        assert arguments == {}


def test_a_sandbox_file_is_piped_from_nix_cat_file_into_the_send_tool():
    answer = send_from_sandbox("sbx-7f", "/workspace/chart.png")
    assert answer.error == ""
    assert answer.call == {
        "name": CAT_FILE,
        "arguments": {
            "sandbox_id": "sbx-7f",
            "path": "/workspace/chart.png",
            "tool_name": "tg_send_file",
        },
    }
    # the note is for the pipe's trace: what the model reads is the send's result
    assert "/workspace/chart.png" in answer.result


def test_the_file_is_posted_under_the_name_its_path_ends_with():
    async def scenario():
        data = b"\x89PNG bytes"
        delivery = FakeDelivery()
        answer = await send_file(
            delivery, -100, "/workspace/charts/x.png", base64.b64encode(data).decode("ascii")
        )
        assert answer.error == ""
        assert delivery.files == [{"chat_id": -100, "name": "x.png", "data": data}]
        # the message id a rollback would delete
        assert answer.forwarded == 500
        assert "x.png" in answer.result and f"{len(data)} bytes" in answer.result

    asyncio.run(scenario())


def test_content_that_is_not_base64_is_an_error_rather_than_a_send():
    async def scenario():
        delivery = FakeDelivery()
        answer = await send_file(delivery, -100, "/workspace/x.png", "not base64!")
        assert "base64" in answer.error
        assert delivery.files == []

    asyncio.run(scenario())


def test_a_file_telegram_refuses_is_an_error():
    async def scenario():
        delivery = FakeDelivery(fail="telegram said no")
        answer = await send_file(delivery, -100, "/workspace/x.png", "AAEC")
        assert "could not send x.png" in answer.error
        assert "telegram said no" in answer.error
        assert answer.forwarded is None

    asyncio.run(scenario())

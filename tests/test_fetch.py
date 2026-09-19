"""The tools that fetch a repository or a URL into a sandbox: their rules and pipe."""

from __future__ import annotations

import asyncio
import base64
import io
import subprocess
import tarfile
import time
from pathlib import Path

import pytest
from support import FakeTransfer, serving

from userbot.fetch import (
    CommandTransfer,
    TransferError,
    _run_under_cap,
    clone_arguments,
    clone_to_sandbox,
    curl_to_sandbox,
    fetch_arguments,
)
from userbot.sandbox import ADD_FILE
from userbot.tools import MAX_DOWNLOAD_BYTES


def test_a_url_is_only_fetched_over_http():
    for url in (
        "file:///etc/passwd",
        "ftp://example.test/x",
        "git@github.com:psf/requests.git",
        "example.test/x",
    ):
        arguments, complaint = fetch_arguments(
            {"url": url, "sandbox_id": "sbx-1", "path": "/workspace/x"}
        )
        assert complaint and "http(s)" in complaint, url
        assert arguments == {}
    # and the clone tool judges a URL the same way, before git ever runs
    assert "http(s)" in clone_arguments({"url": "file:///tmp/repo", "sandbox_id": "sbx-1"})[1]


def test_a_download_is_named_after_the_url_unless_it_is_told_otherwise():
    arguments, complaint = clone_arguments(
        {"url": "https://github.com/psf/requests.git", "sandbox_id": "sbx-1"}
    )
    assert complaint is None
    assert arguments["path"] == "/workspace/requests.tar.gz"
    assert arguments["depth"] == 1 and arguments["branch"] == ""

    fetched, complaint = fetch_arguments(
        {"url": "https://example.test/a/report.pdf", "sandbox_id": "sbx-1"}
    )
    assert complaint is None and fetched["path"] == "/workspace/report.pdf"
    # a URL with no name at all has to be told where to put it
    assert (
        "must be given"
        in fetch_arguments({"url": "https://example.test/", "sandbox_id": "sbx-1"})[1]
    )
    # and a name that would unpack somewhere else is not one
    hostile = clone_arguments(
        {"url": "https://example.test/a/..%2F..%2Fetc.git", "sandbox_id": "sbx-1"}
    )[0]
    assert hostile["path"] == "/workspace/.._.._etc.tar.gz"


def test_the_archive_has_to_be_named_like_one():
    base = {"url": "https://example.test/r.git", "sandbox_id": "sbx-1"}
    assert "end in .tar.gz" in clone_arguments({**base, "path": "/workspace/repo"})[1]
    assert "under /workspace" in clone_arguments({**base, "path": "/tmp/repo.tar.gz"})[1]
    assert clone_arguments({**base, "path": "/workspace/repo.tgz"})[1] is None
    # the file a URL serves is named freely
    assert (
        fetch_arguments(
            {"url": "https://example.test/a.pdf", "sandbox_id": "sbx-1", "path": "/workspace/x"}
        )[1]
        is None
    )


def test_an_argument_that_makes_no_sense_is_refused():
    good = {"url": "https://example.test/x", "sandbox_id": "sbx-1", "path": "/workspace/x"}
    for field, value in (
        ("url", ""),
        ("url", "nope"),
        ("sandbox_id", ""),
        ("sandbox_id", 7),
        ("path", "/etc/passwd"),
        ("path", "/workspace/../etc/x"),
    ):
        arguments, complaint = fetch_arguments({**good, field: value})
        assert complaint and field in complaint, (field, value)
        assert arguments == {}
    # how deep a history, how much of it: numbers only, and no negatives
    archive = {**good, "path": "/workspace/x.tar.gz"}
    for value in ("later", -1, True):
        assert "depth" in clone_arguments({**archive, "depth": value})[1]
    assert clone_arguments({**archive, "depth": "5"})[0]["depth"] == 5
    assert clone_arguments({**archive, "depth": 0})[0]["depth"] == 0
    assert clone_arguments({**archive, "branch": 7})[1] is not None
    assert clone_arguments({**archive, "branch": "v2.0"})[0]["branch"] == "v2.0"


def test_a_clone_is_piped_into_nix_add_file_as_one_archive():
    async def scenario():
        transfer = FakeTransfer(file=b"print('hello')\n")
        answer = await clone_to_sandbox(
            transfer,
            "https://example.test/acme/toolkit.git",
            "sbx-7f",
            "/workspace/toolkit.tar.gz",
            "",
            1,
        )
        assert answer.error == ""
        assert answer.call["name"] == ADD_FILE
        arguments = answer.call["arguments"]
        assert arguments["sandbox_id"] == "sbx-7f"
        assert arguments["path"] == "/workspace/toolkit.tar.gz"
        archive = base64.b64decode(arguments["content_base64"])
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as packed:
            assert packed.getnames() == ["toolkit", "toolkit/.git", "toolkit/README.md"]
            assert packed.extractfile("toolkit/README.md").read() == b"print('hello')\n"
        assert f"{len(archive)} bytes" in answer.result
        assert transfer.cloned == [("https://example.test/acme/toolkit.git", "", 1)]

    asyncio.run(scenario())


def test_a_fetch_is_piped_into_nix_add_file_as_its_bytes():
    async def scenario():
        data = b"%PDF-1.4 fetched"
        answer = await curl_to_sandbox(
            FakeTransfer(file=data),
            "https://example.test/report.pdf",
            "sbx-7f",
            "/workspace/report.pdf",
        )
        assert answer.error == ""
        assert answer.call == {
            "name": ADD_FILE,
            "arguments": {
                "sandbox_id": "sbx-7f",
                "path": "/workspace/report.pdf",
                "content_base64": base64.b64encode(data).decode("ascii"),
            },
        }
        assert f"{len(data)} bytes" in answer.result

    asyncio.run(scenario())


def test_a_transfer_that_fails_is_an_error_rather_than_a_pipe():
    async def scenario():
        failing = FakeTransfer(fail="the network said no")
        cloned = await clone_to_sandbox(
            failing, "https://example.test/r.git", "sbx-7f", "/workspace/r.tar.gz", "", 1
        )
        assert "could not clone https://example.test/r.git" in cloned.error
        assert "the network said no" in cloned.error and cloned.call is None
        fetched = await curl_to_sandbox(failing, "https://example.test/x", "sbx-7f", "/workspace/x")
        assert "could not fetch https://example.test/x" in fetched.error
        assert "the network said no" in fetched.error and fetched.call is None
        # a reply with nothing in it is not a file to hand over
        empty = await curl_to_sandbox(
            FakeTransfer(file=b""), "https://example.test/x", "sbx-7f", "/workspace/x"
        )
        assert "served nothing" in empty.error and empty.call is None

    asyncio.run(scenario())


# --- the real thing, against a real curl, a real git and a local server -------


def fetch_with(url: str, into: Path, cap: int = MAX_DOWNLOAD_BYTES, budget: float = 30.0) -> Path:
    """One real fetch, with the caps a test wants."""
    return asyncio.run(CommandTransfer(max_bytes=cap, budget=budget).fetch(url, into))


def test_the_real_curl_leaves_a_reply_under_the_cap_alone(tmp_path):
    body = b"hello from the other side" * 8
    with serving(body) as url:
        saved = fetch_with(url, tmp_path)
        assert saved.read_bytes() == body


def test_the_real_curl_refuses_a_reply_that_declares_itself_too_big(tmp_path):
    with serving(b"x" * 4096) as url:
        with pytest.raises(TransferError) as refused:
            fetch_with(url, tmp_path, cap=1024)
        assert "failed" in str(refused.value)


def test_the_real_curl_stops_a_reply_that_grows_past_the_cap(tmp_path):
    """No declared size, so nothing can be refused up front — it stops mid-reply."""
    with serving(b"x" * (1 << 20), chunks=2, delay=10.0, declare=False) as url:
        started = time.monotonic()
        with pytest.raises(TransferError) as stopped:
            fetch_with(url, tmp_path, cap=1 << 16, budget=60.0)
        assert "65536 bytes" in str(stopped.value)
        assert time.monotonic() - started < 5  # it did not sit out the server's pause


def test_the_watch_stops_a_download_that_oversteps_while_it_runs(tmp_path):
    """What stops a clone: `git` has no size limit of its own, so the watch is it."""
    watched = tmp_path / "download"
    growing = f"for _ in $(seq 1 20); do head -c 1048576 /dev/zero >> {watched}; sleep 1; done"

    async def scenario():
        started = time.monotonic()
        with pytest.raises(TransferError) as stopped:
            await _run_under_cap(
                ["bash", "-c", growing], tmp_path, watched, 1 << 16, 60.0, "the download"
            )
        assert "passed 65536 bytes, and was stopped there" in str(stopped.value)
        # the command was killed rather than waited out: this only returns if the
        # process group really went away, and it is nowhere near its own 20 seconds
        assert time.monotonic() - started < 10

    asyncio.run(scenario())


def test_a_reply_that_never_comes_is_given_up_on(tmp_path):
    with serving(b"x", stall=30.0) as url:
        started = time.monotonic()
        with pytest.raises(TransferError) as stopped:
            fetch_with(url, tmp_path, budget=0.6)
        assert "did not finish within" in str(stopped.value)
        assert time.monotonic() - started < 10


def commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=test@example.test",
            "-c",
            "user.name=Test",
            "commit",
            "-q",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
    )


def repository(tmp_path: Path) -> Path:
    """A real repository with two commits, so a depth means something."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "README.md").write_text("hello\n")
    commit(repo, "first")
    (repo / "more.txt").write_text("more\n")
    commit(repo, "second")
    return repo


def test_the_real_git_clone_brings_as_much_history_as_it_was_asked_for(tmp_path):
    repo = repository(tmp_path)

    async def scenario():
        shallow = tmp_path / "shallow"
        shallow.mkdir()
        checkout = await CommandTransfer().clone(repo.as_uri(), "", 1, shallow)
        assert (checkout / "README.md").read_text() == "hello\n"
        # the tip only, which is what the default is for
        assert (checkout / ".git" / "shallow").exists()

        whole = tmp_path / "whole"
        whole.mkdir()
        full = await CommandTransfer().clone(repo.as_uri(), "", 0, whole)
        assert (full / "more.txt").exists()
        assert not (full / ".git" / "shallow").exists()  # depth 0 is the whole history

        named = tmp_path / "named"
        named.mkdir()
        tagged = await CommandTransfer().clone(repo.as_uri(), "main", 1, named)
        assert (tagged / "README.md").exists()

    asyncio.run(scenario())


def test_a_clone_that_passes_the_cap_is_stopped(tmp_path):
    repo = repository(tmp_path)

    async def scenario():
        into = tmp_path / "scratch"
        into.mkdir()
        with pytest.raises(TransferError) as stopped:
            await CommandTransfer(max_bytes=1, budget=30.0).clone(repo.as_uri(), "", 0, into)
        assert "1 bytes" in str(stopped.value)

    asyncio.run(scenario())


def test_a_real_checkout_arrives_as_one_archive_with_its_git_directory(tmp_path):
    repo = repository(tmp_path)

    async def scenario():
        answer = await clone_to_sandbox(
            CommandTransfer(),
            repo.as_uri(),
            "sbx-7f",
            "/workspace/repo.tar.gz",
            "",
            1,
        )
        assert answer.error == ""
        archive = base64.b64decode(answer.call["arguments"]["content_base64"])
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as packed:
            names = packed.getnames()
            assert "repo/README.md" in names and "repo/.git/config" in names
            assert packed.extractfile("repo/README.md").read() == b"hello\n"

    asyncio.run(scenario())

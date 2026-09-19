"""The tools that fetch a repository or a URL into a sandbox.

A sandbox has no network, so anything that has to be downloaded is downloaded
here, on the userbot's side, and handed over the way every file reaches a sandbox:
a tool pipe into the harness's own `nix_add_file` (see `sandbox.py`). Two things
arrive this way — a git clone, packed into one tar.gz because `nix_add_file` takes
files and a checkout is a directory, and whatever a URL serves, fetched with
`curl`. Neither may bring more than `MAX_DOWNLOAD_BYTES` down in one call: a
download is measured while it runs and the command is killed the moment it
crosses that, so a runaway clone costs this machine a moment, not its disk.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import tarfile
import tempfile
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit

from .harness import ADD_FILE_BYTES
from .sandbox import WORKSPACE, add_file_pipe, sandbox_path
from .tools import MAX_DOWNLOAD_BYTES, Answer, text_argument

# One call's whole budget, the download included. The harness is told to wait
# longer than this for our answer (`music.LOCAL_TIMEOUT`), so we are the ones who
# give up, and the model is told why.
TRANSFER_BUDGET = 600.0
# A fetch that has not connected in this long is not going to: silence is a
# failure, slowness is not.
CONNECT_TIMEOUT = 30.0
# How often a running download is measured.
SIZE_POLL = 0.2
# What a downloaded file may be called, and what a tar member may be named.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")

CLONE_TOOL = {
    "name": "git_clone_to_sandbox",
    "description": (
        "Clone a git repository into a live sandbox, as one archive you unpack "
        "yourself. The clone is made here, on the userbot's side — a sandbox has "
        "no network — and what lands is a tar.gz holding the checkout together "
        "with its `.git`, so git works inside the sandbox too.\n"
        "`url`: the repository — an `https://` or `http://` URL, like "
        "https://github.com/psf/requests.git; nothing else is fetched.\n"
        "`sandbox_id`: the id nix_spawn_sandbox returned; the sandbox has to be "
        "live, and nix_sandbox_status says whether it still is.\n"
        "`path`: where the archive goes — an absolute path under /workspace "
        "ending in `.tar.gz`, like /workspace/requests.tar.gz; its directory is "
        "created for you. Leave it out to take /workspace/<repository>.tar.gz.\n"
        "`depth`: how much history to bring. The default, 1, is the tip commit "
        "only — small, and enough to read, build and run the code. 0 brings the "
        "whole history, often tens of megabytes more, so ask for it when the "
        "history is what is wanted (git log, git blame, an old revision); any "
        "other number is that many commits back.\n"
        "`branch`: a branch or tag to clone instead of the repository's own "
        "default, which with `depth` 1 is the only one that arrives.\n"
        "No single call may bring more than 150 MB down: the clone is watched "
        "while it runs and stopped the moment it crosses that, and the archive "
        "has to fit the harness's own per-file limit besides. Either way you get "
        "an error rather than half a checkout.\n"
        "Returns what the sandbox wrote. A fresh sandbox has no `tar` of its own: "
        "install `gnutar` and `gzip` with nix_add_dependency — one package per "
        "call — and `git` too if the `.git` inside the archive is to be used, "
        "then unpack it with nix_exec: `tar xzf <path> -C /workspace` leaves a "
        "directory named after the repository."
    ),
    "params": [
        {
            "name": "url",
            "type": "string",
            "description": "the repository to clone, as an `https://` URL",
        },
        {
            "name": "sandbox_id",
            "type": "string",
            "description": "the id nix_spawn_sandbox returned",
        },
        {
            "name": "path",
            "type": "string",
            "description": (
                "where the tar.gz goes: under /workspace, e.g. "
                "/workspace/requests.tar.gz; omit to take the repository's name"
            ),
        },
        {
            "name": "depth",
            "type": "integer",
            "description": (
                "commits of history to bring: 1 is the tip only, 0 is all of it; omit for 1"
            ),
        },
        {
            "name": "branch",
            "type": "string",
            "description": "a branch or tag to clone; omit for the repository's default branch",
        },
    ],
    # The archive really lands in the sandbox, and nothing can take it back.
    "external_effects": True,
}

CURL_TOOL = {
    "name": "curl_to_sandbox",
    "description": (
        "Fetch whatever a URL serves into a live sandbox — a PDF, a picture, an "
        "archive, a script, a release binary. The request is made here, on the "
        "userbot's side, because a sandbox has no network, and the bytes go "
        "straight into the sandbox at the path you choose, never through you, so "
        "a file costs nothing to hand over.\n"
        "`url`: what to fetch — an `https://` or `http://` URL; nothing else is "
        "fetched. Redirects are followed.\n"
        "`sandbox_id`: the id nix_spawn_sandbox returned; the sandbox has to be "
        "live, and nix_sandbox_status says whether it still is.\n"
        "`path`: where it goes — an absolute path under /workspace, like "
        "/workspace/data.csv; its directory is created for you, and a file that "
        "starts with `#!` is made executable. Leave it out to take "
        "/workspace/<the URL's last path segment>.\n"
        "A reply is stopped the moment it passes 150 MB, and comes back as an "
        "error rather than a half-written file.\n"
        "Returns what the sandbox wrote, so the file is then there to read or "
        "run with nix_exec."
    ),
    "params": [
        {"name": "url", "type": "string", "description": "what to fetch, as an `https://` URL"},
        {
            "name": "sandbox_id",
            "type": "string",
            "description": "the id nix_spawn_sandbox returned",
        },
        {
            "name": "path",
            "type": "string",
            "description": (
                "where it goes: under /workspace, e.g. /workspace/data.csv; "
                "omit to take the URL's last path segment"
            ),
        },
    ],
    # The file really lands in the sandbox, and nothing can take it back.
    "external_effects": True,
}

TOOLS = [CLONE_TOOL, CURL_TOOL]


class TransferError(Exception):
    """A download that could not be finished — refused, failed, or too big."""


class Transfer(Protocol):
    """How the two tools reach the outside world, behind an interface.

    The real one runs `git` and `curl`; a test hands in its own, so neither the
    suite nor a review needs a network to see what the tools do with the bytes.
    """

    async def clone(self, url: str, branch: str, depth: int, into: Path) -> Path:
        """Clone `url` under `into`, and return the checkout."""

    async def fetch(self, url: str, into: Path) -> Path:
        """Download `url` into `into`, and return the file."""


def _optional_text(arguments: dict, field: str) -> tuple[str, str | None]:
    """An optional string a tool was given: empty when it was left out."""
    value = arguments.get(field)
    if value is None or value == "":
        return "", None
    if not isinstance(value, str):
        return "", f"`{field}` must be a string"
    return value.strip(), None


def _depth_argument(arguments: dict) -> tuple[int, str | None]:
    """How much history to bring: the tip only, unless it said otherwise."""
    value = arguments.get("depth")
    if value is None or value == "":
        return 1, None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0, "`depth` must be a number of commits"
    try:
        depth = int(value)
    except ValueError:
        return 0, "`depth` must be a number of commits"
    if depth < 0:
        return 0, "`depth` must not be negative"
    return depth, None


def _url_argument(arguments: dict) -> tuple[str, str | None]:
    """The URL a tool was pointed at, or what is wrong with it."""
    url, complaint = text_argument(arguments, "url")
    if complaint is not None:
        return "", complaint
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return (
            "",
            (
                "`url` must be an http(s) URL: this fetches over the network, never "
                "from this machine's filesystem"
            ),
        )
    return url, None


def _name_from_url(url: str, suffix: str = "") -> str | None:
    """What to call a download: the URL's last path segment, or nothing.

    A repository URL usually ends in `.git`, which is not part of the name, and a
    clone's archive ends in `.tar.gz` whatever the repository is called. What is
    left is stripped to characters that are safe in a path *and* in a tar member,
    so a clever URL cannot name a file that unpacks somewhere else.
    """
    last = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    last = last.removesuffix(".git")
    last = _SAFE_NAME.sub("_", last)
    if not last.strip("."):
        return None
    return f"{last}{suffix}"


def _destination(arguments: dict, url: str, suffix: str = "") -> tuple[str, str | None]:
    """Where in the sandbox a download goes, or what is wrong with the path."""
    path, complaint = _optional_text(arguments, "path")
    if complaint is not None:
        return "", complaint
    if not path:
        named = _name_from_url(url, suffix)
        if named is None:
            return "", "`path` must be given: this URL has no file name to take one from"
        path = f"{WORKSPACE}/{named}"
    path, complaint = sandbox_path(path)
    if complaint is not None:
        return "", complaint
    if suffix and not path.endswith((".tar.gz", ".tgz")):
        return "", (
            "`path` must end in .tar.gz: the checkout arrives as one archive, not a directory"
        )
    return path, None


def clone_arguments(arguments: dict) -> tuple[dict, str | None]:
    """The arguments `git_clone_to_sandbox` was given, cleaned, or what is wrong."""
    url, complaint = _url_argument(arguments)
    if complaint is not None:
        return {}, complaint
    sandbox_id, complaint = text_argument(arguments, "sandbox_id")
    if complaint is not None:
        return {}, complaint
    path, complaint = _destination(arguments, url, suffix=".tar.gz")
    if complaint is not None:
        return {}, complaint
    branch, complaint = _optional_text(arguments, "branch")
    if complaint is not None:
        return {}, complaint
    depth, complaint = _depth_argument(arguments)
    if complaint is not None:
        return {}, complaint
    return {
        "url": url,
        "sandbox_id": sandbox_id,
        "path": path,
        "branch": branch,
        "depth": depth,
    }, None


def fetch_arguments(arguments: dict) -> tuple[dict, str | None]:
    """The arguments `curl_to_sandbox` was given, cleaned, or what is wrong."""
    url, complaint = _url_argument(arguments)
    if complaint is not None:
        return {}, complaint
    sandbox_id, complaint = text_argument(arguments, "sandbox_id")
    if complaint is not None:
        return {}, complaint
    path, complaint = _destination(arguments, url)
    if complaint is not None:
        return {}, complaint
    return {"url": url, "sandbox_id": sandbox_id, "path": path}, None


def _over_cap(path: Path, cap: int) -> bool:
    """Whether what is at `path` — a file, or a tree — has grown past `cap`.

    The total is abandoned the moment it crosses: this runs while a download is
    under way, and the only question it is asked is whether that download has
    already gone too far.
    """
    if path.is_file():
        return path.stat().st_size > cap
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            if total > cap:
                return True
    return False


def _stop(process) -> None:
    """Kill a download and whatever it started — `git` runs helpers of its own."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _tail(raw: bytes, limit: int = 400) -> str:
    """The end of what a command said, as one line for a tool error."""
    return " ".join(raw.decode("utf-8", "replace").split())[-limit:]


async def _run_under_cap(
    argv: list[str], cwd: Path, watch: Path, cap: int, budget: float, what: str
) -> None:
    """Run a command that downloads into `watch`, and stop it if it oversteps.

    `watch` is measured every `SIZE_POLL` seconds — a clone's directory, or a
    file a fetch is writing — and once it is past `cap` the command's whole
    process group is killed, so nothing keeps downloading behind the error.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise TransferError(f"{argv[0]} is not installed on this machine") from None
    said = asyncio.create_task(process.stderr.read())
    deadline = asyncio.get_running_loop().time() + budget
    stopped = ""
    try:
        while True:
            try:
                await asyncio.wait_for(process.wait(), timeout=SIZE_POLL)
                break
            except asyncio.TimeoutError:
                pass
            if _over_cap(watch, cap):
                stopped = f"{what} passed {cap} bytes, and was stopped there"
                break
            if asyncio.get_running_loop().time() > deadline:
                stopped = f"{what} did not finish within {budget:.0f}s, and was stopped"
                break
        if stopped:
            _stop(process)
            await process.wait()
            raise TransferError(stopped)
        if _over_cap(watch, cap):
            # a download can finish inside one poll: the size it came to still counts
            raise TransferError(f"{what} came to more than {cap} bytes")
        if process.returncode != 0:
            reason = _tail(await said) or f"it exited with {process.returncode}"
            raise TransferError(f"{what} failed: {reason}")
    finally:
        said.cancel()


class CommandTransfer:
    """The real thing: `git clone` and `curl`, watched while they run.

    Both write into a scratch directory the caller owns and disappears with it,
    and neither is allowed past `max_bytes` — the clone's checkout is what is
    measured, `.git` included, because that is where the download lands.
    """

    def __init__(
        self, max_bytes: int = MAX_DOWNLOAD_BYTES, budget: float = TRANSFER_BUDGET
    ) -> None:
        self.max_bytes = max_bytes
        self.budget = budget

    async def clone(self, url: str, branch: str, depth: int, into: Path) -> Path:
        checkout = into / "repo"
        argv = ["git", "clone", "--quiet"]
        if branch:
            argv += ["--branch", branch]
        if depth > 0:
            # Only the tip, then: a full history is the bigger download by far,
            # and it is asked for deliberately (depth 0).
            argv += ["--depth", str(depth), "--single-branch"]
        argv += ["--", url, str(checkout)]
        await _run_under_cap(
            argv, into, checkout, self.max_bytes, self.budget, f"the clone of {url}"
        )
        return checkout

    async def fetch(self, url: str, into: Path) -> Path:
        target = into / "download"
        argv = [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            # A redirect is how a download turns into something else entirely, so
            # the scheme is pinned on both legs rather than trusted.
            "--proto",
            "=http,https",
            "--proto-redir",
            "=http,https",
            # The first line of defence: a reply that declares itself too big is
            # never read. What is chunked past the cap is caught by the watch.
            "--max-filesize",
            str(self.max_bytes),
            "--connect-timeout",
            str(int(CONNECT_TIMEOUT)),
            "--max-time",
            str(int(self.budget)),
            "--output",
            str(target),
            "--",
            url,
        ]
        await _run_under_cap(
            argv, into, target, self.max_bytes, self.budget, f"the download of {url}"
        )
        if not target.exists():
            raise TransferError(f"curl wrote nothing for {url}")
        return target


def _pack(checkout: Path, target: Path, top: str) -> None:
    """Pack a checkout into one tar.gz, giving up if it cannot be handed over.

    The archive holds `.git` as well, so the sandbox has a repository and not
    just a pile of files; the cap is the harness's own per-file limit, and the
    packing stops the moment it is crossed rather than growing a file that could
    never be piped.
    """
    with tarfile.open(target, "w:gz") as archive:
        archive.add(checkout, arcname=top, recursive=False)
        for path in sorted(checkout.rglob("*")):
            if target.stat().st_size > ADD_FILE_BYTES:
                raise TransferError(
                    f"the checkout is too big to hand over: an archive of it passed "
                    f"{ADD_FILE_BYTES} bytes"
                )
            archive.add(path, arcname=f"{top}/{path.relative_to(checkout)}", recursive=False)


async def clone_to_sandbox(
    transfer: Transfer, url: str, sandbox_id: str, path: str, branch: str, depth: int
) -> Answer:
    """Clone a repository here, and hand the archive to `nix_add_file` as a pipe."""
    with tempfile.TemporaryDirectory(prefix="userbot-clone-") as scratch:
        into = Path(scratch)
        try:
            checkout = await transfer.clone(url, branch, depth, into)
            archive = into / "checkout.tar.gz"
            _pack(checkout, archive, _name_from_url(url) or "repo")
        except Exception as error:
            return Answer(error=f"could not clone {url}: {error}")
        archive_bytes = archive.read_bytes()
    return Answer(
        result=f"cloned {url} into {len(archive_bytes)} bytes of tar.gz",
        call=add_file_pipe(sandbox_id, path, archive_bytes),
    )


async def curl_to_sandbox(transfer: Transfer, url: str, sandbox_id: str, path: str) -> Answer:
    """Fetch a URL here, and hand its bytes to `nix_add_file` as a pipe."""
    with tempfile.TemporaryDirectory(prefix="userbot-curl-") as scratch:
        try:
            saved = await transfer.fetch(url, Path(scratch))
        except Exception as error:
            return Answer(error=f"could not fetch {url}: {error}")
        data = saved.read_bytes()
    if not data:
        return Answer(error=f"{url} served nothing")
    return Answer(
        result=f"fetched {len(data)} bytes from {url}",
        call=add_file_pipe(sandbox_id, path, data),
    )

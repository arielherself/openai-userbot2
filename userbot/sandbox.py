"""The sandbox tools' shared half, and the two that carry a chat's files.

Everything that puts something into a sandbox — a message's file, a clone, a URL —
ends the same way: a tool pipe into the harness's own `nix_add_file`, whose
arguments this module builds (`add_file_pipe`) and whose destination rules it
enforces (`sandbox_path`). The bytes never travel through the model: the file is
fetched here and handed straight over as the next step of the pipe, so the
transcript only ever sees the tool names and what the sandbox wrote — never the
base64 payload that carried them.

A file comes back out the same way, over the same wire. `tg_send_file_from_sandbox`
hands a sandbox path to the harness's `nix_cat_file` (`cat_file_pipe`), which
reads the file and gives its bytes to `tg_send_file`, the local tool that posts
them into the chat — so the model is shown the pipe's ends and what the chat got,
never the file itself.
"""

from __future__ import annotations

import base64

from .telegram import Delivery
from .tools import Answer, chat_argument, message_id_argument, text_argument

# Where a sandbox keeps what survives from one command to the next.
WORKSPACE = "/workspace"

# The harness's own tool for writing bytes into a sandbox; our answer pipes into it.
ADD_FILE = "nix_add_file"
# Its opposite number: reading a file out of one, on its way to `tg_send_file`.
CAT_FILE = "nix_cat_file"

TOOL = {
    "name": "tg_download_file_to_sandbox",
    "description": (
        "Download the file a Telegram message carries into a live sandbox, at a "
        "path you choose. It is written there as its own bytes — a PDF, a "
        "picture, an archive, anything — and never passes through you, so a "
        "binary file costs nothing to hand over.\n"
        "`chat_id`: the chat the message is in — a public name like `@telegram`, "
        "or the chat id the view tools print.\n"
        "`message_id`: the id of the message carrying the file — the numbers "
        "tg_view_current_chat and tg_view_public_chat print in brackets.\n"
        "`sandbox_id`: the id nix_spawn_sandbox returned; the sandbox has to be "
        "live, and nix_sandbox_status says whether it still is.\n"
        "`path`: where the file goes in the sandbox — an absolute path under "
        "/workspace, like /workspace/report.pdf. Its directory is created for "
        "you, and a file that starts with `#!` is made executable.\n"
        "The file may be up to 150 MB; a bigger one is refused rather than "
        "half handed over.\n"
        "Returns what the sandbox wrote, so the file is then there to read, run "
        "or unpack with nix_exec."
    ),
    "params": [
        {
            "name": "chat_id",
            "type": "string",
            "description": "the message's chat: `@name`, or the chat id the view tools printed",
        },
        {
            "name": "message_id",
            "type": "integer",
            "description": "the id of the message carrying the file",
        },
        {
            "name": "sandbox_id",
            "type": "string",
            "description": "the id nix_spawn_sandbox returned",
        },
        {
            "name": "path",
            "type": "string",
            "description": "where to write it in the sandbox: under /workspace, e.g. /workspace/report.pdf",
        },
    ],
    # The file really lands in the sandbox, and nothing can take it back.
    "external_effects": True,
}

SEND_FROM_SANDBOX_TOOL = {
    "name": "tg_send_file_from_sandbox",
    "description": (
        "Send a file that is in a live sandbox into this chat, where it arrives "
        "as a document named after the last part of its path.\n"
        "The file never passes through you: this call hands its path to the "
        "harness's nix_cat_file, which reads it out of the sandbox and gives the "
        "bytes to tg_send_file, which posts them here — so a picture, an archive "
        "or a binary of any size costs nothing to send. What you are shown is "
        "tg_send_file's own result.\n"
        "`sandbox_id`: the id nix_spawn_sandbox returned; the sandbox has to be "
        "live, and nix_sandbox_status says whether it still is.\n"
        "`path`: the file to send — an absolute path in the sandbox, usually "
        "under /workspace, like /workspace/chart.png.\n"
        "A path that is missing, one the sandbox will not read, and a file over "
        "200 MiB all come back as errors instead of being sent."
    ),
    "params": [
        {
            "name": "sandbox_id",
            "type": "string",
            "description": "the id nix_spawn_sandbox returned",
        },
        {
            "name": "path",
            "type": "string",
            "description": "the file to send: an absolute path in the sandbox, e.g. /workspace/chart.png",
        },
    ],
    # Nothing is read or sent here: the read is the harness's, and whatever the
    # piped tg_send_file posts is recorded as that call's own effect.
}

SEND_FILE_TOOL = {
    "name": "tg_send_file",
    "description": (
        "Post a file into this chat as a document named after the last part of "
        "`path`.\n"
        "This is the last step of the pipe tg_send_file_from_sandbox starts: "
        "nix_cat_file reads a sandbox file out and hands it here base64-encoded, "
        "so the file is posted without a byte of it entering this conversation. "
        "Sending what is in a sandbox is what that tool is for — call this one "
        "directly only for bytes you are already holding.\n"
        "`path`: where the file came from; only its last segment is used, as the "
        "name the chat sees.\n"
        "`content`: the file's bytes, base64-encoded (standard alphabet).\n"
        "Returns a note saying the file was sent; a Telegram refusal is an error. "
        "Nothing is piped after this call."
    ),
    "params": [
        {
            "name": "path",
            "type": "string",
            "description": "the file's path; its last segment is the name the chat sees",
        },
        {
            "name": "content",
            "type": "string",
            "description": "the file's bytes, base64-encoded",
        },
    ],
    # The file really was posted, so a turn that fails afterwards takes it back.
    "rollback": True,
    "external_effects": True,
}


def sandbox_path(path: str) -> tuple[str, str | None]:
    """The destination inside the sandbox, normalized, or what is wrong with it.

    A sandbox writes files only through its writable /workspace, and a path that
    walks out of it is refused here rather than at the far end.
    """
    if not path.startswith("/"):
        return "", f"`path` must be an absolute path under {WORKSPACE}, like {WORKSPACE}/report.pdf"
    parts = [part for part in path.split("/") if part]
    if any(part in (".", "..") for part in parts):
        return "", "`path` must not contain `.` or `..`"
    normalized = "/" + "/".join(parts)
    if not normalized.startswith(f"{WORKSPACE}/"):
        return "", f"`path` must be under {WORKSPACE}, like {WORKSPACE}/report.pdf"
    return normalized, None


def sandbox_read_path(path: str) -> tuple[str, str | None]:
    """The file inside a sandbox a tool asked for, normalized, or what is wrong.

    A read may reach anywhere the sandbox can, but it may not name a place by
    walking to it: a path that is not absolute, or that carries `.` or `..`, is
    refused here rather than at the far end of a pipe.
    """
    if not path.startswith("/"):
        return "", f"`path` must be an absolute path in the sandbox, like {WORKSPACE}/chart.png"
    parts = [part for part in path.split("/") if part]
    if any(part in (".", "..") for part in parts):
        return "", "`path` must not contain `.` or `..`"
    if not parts:
        return "", f"`path` must name a file, like {WORKSPACE}/chart.png"
    return "/" + "/".join(parts), None


def file_name(path: str) -> str:
    """What a file is called: the last segment of its path."""
    return path.rsplit("/", 1)[-1]


def export_arguments(arguments: dict) -> tuple[dict, str | None]:
    """The two arguments `tg_send_file_from_sandbox` was given, cleaned, or what is wrong."""
    sandbox_id, complaint = text_argument(arguments, "sandbox_id")
    if complaint is not None:
        return {}, complaint
    path, complaint = text_argument(arguments, "path")
    if complaint is not None:
        return {}, complaint
    path, complaint = sandbox_read_path(path)
    if complaint is not None:
        return {}, complaint
    return {"sandbox_id": sandbox_id, "path": path}, None


def send_arguments(arguments: dict) -> tuple[dict, str | None]:
    """The two arguments `tg_send_file` was given, cleaned, or what is wrong."""
    path, complaint = text_argument(arguments, "path")
    if complaint is not None:
        return {}, complaint
    if path.endswith("/"):
        return {}, "`path` must name a file, not a directory"
    content, complaint = text_argument(arguments, "content")
    if complaint is not None:
        return {}, complaint
    return {"path": path, "content": content}, None


def read_arguments(arguments: dict) -> tuple[dict, str | None]:
    """The four arguments the tool was given, cleaned, or what is wrong with them."""
    chat_id, complaint = chat_argument(arguments, "chat_id")
    if complaint is None and chat_id is None:
        complaint = "`chat_id` must be a chat name like `@telegram` or a chat id"
    if complaint is not None:
        return {}, complaint
    message_id, complaint = message_id_argument(arguments)
    if complaint is not None:
        return {}, complaint
    sandbox_id, complaint = text_argument(arguments, "sandbox_id")
    if complaint is not None:
        return {}, complaint
    path, complaint = text_argument(arguments, "path")
    if complaint is not None:
        return {}, complaint
    path, complaint = sandbox_path(path)
    if complaint is not None:
        return {}, complaint
    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "sandbox_id": sandbox_id,
        "path": path,
    }, None


def add_file_pipe(sandbox_id: str, path: str, data: bytes) -> dict:
    """The next step of a pipe: `nix_add_file` writing these bytes at `path`."""
    return {
        "name": ADD_FILE,
        "arguments": {
            "sandbox_id": sandbox_id,
            "path": path,
            "content_base64": base64.b64encode(data).decode("ascii"),
        },
    }


async def download_to_sandbox(
    delivery: Delivery, chat_id, message_id: int, sandbox_id: str, path: str
) -> Answer:
    """Fetch a message's file, and hand its bytes to `nix_add_file` as a pipe.

    The answer names the sandbox tool rather than reporting anything itself: the
    model is told what the sandbox wrote, and the download stays in the pipe's
    trace along with the base64 it carried.
    """
    where = f"chat {chat_id}"
    try:
        downloaded = await delivery.download(chat_id, message_id)
    except Exception as error:
        return Answer(error=f"could not download message {message_id} in {where}: {error}")
    if downloaded is None:
        return Answer(error=f"message {message_id} in {where} carries no file")
    return Answer(
        result=(
            f"downloaded {len(downloaded.data)} bytes"
            + (f" of {downloaded.name}" if downloaded.name else "")
            + f" from message {message_id}"
        ),
        call=add_file_pipe(sandbox_id, path, downloaded.data),
    )


def cat_file_pipe(sandbox_id: str, path: str) -> dict:
    """The next step of a pipe: `nix_cat_file` reading `path` out of a sandbox.

    The harness runs it, and the file goes on to `tg_send_file` — the name is
    settled here so neither the model nor its own call has to name the last step
    of the chain the file travels.
    """
    return {
        "name": CAT_FILE,
        "arguments": {
            "sandbox_id": sandbox_id,
            "path": path,
            "tool_name": SEND_FILE_TOOL["name"],
        },
    }


def send_from_sandbox(sandbox_id: str, path: str) -> Answer:
    """Point `nix_cat_file` at a sandbox file for `tg_send_file` to post.

    Nothing is read here: the answer names the harness's read as the next step,
    and what the model finally sees is `tg_send_file`'s own result, with this
    note left in the pipe's trace.
    """
    return Answer(
        result=f"handing {path} out of sandbox {sandbox_id} to {SEND_FILE_TOOL['name']}",
        call=cat_file_pipe(sandbox_id, path),
    )


async def send_file(delivery: Delivery, chat_id, path: str, content: str) -> Answer:
    """Post a file into the chat, under the name its path ends with.

    The bytes arrive base64-encoded because that is the one faithful form a
    tool's arguments can carry across the wire — `nix_cat_file` sends them that
    way — so they are decoded here, before anything reaches Telegram.
    """
    try:
        data = base64.b64decode(content, validate=True)
    except ValueError as error:  # binascii.Error is one of these
        return Answer(error=f"`content` is not valid base64: {error}")
    name = file_name(path)
    try:
        message_id = await delivery.send_file(chat_id, name, data)
    except Exception as error:
        return Answer(error=f"could not send {name}: {error}")
    return Answer(
        result=f"sent {name} ({len(data)} bytes) into the chat",
        forwarded=message_id,
    )

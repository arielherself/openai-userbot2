"""The tool that puts a Telegram file into a sandbox.

The bytes never travel through the model: the file is downloaded here and handed
straight to the harness's own `nix_add_file` as the next step of a tool pipe, so
the transcript only ever sees the two tool names and what the sandbox wrote —
never the base64 payload that carried them.
"""

from __future__ import annotations

import base64

from .telegram import Delivery
from .tools import Answer, chat_argument, message_id_argument, text_argument

# Where a sandbox keeps what survives from one command to the next.
WORKSPACE = "/workspace"

# The harness's own tool for writing bytes into a sandbox; our answer pipes into it.
ADD_FILE = "nix_add_file"

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
        call={
            "name": ADD_FILE,
            "arguments": {
                "sandbox_id": sandbox_id,
                "path": path,
                "content_base64": base64.b64encode(downloaded.data).decode("ascii"),
            },
        },
    )

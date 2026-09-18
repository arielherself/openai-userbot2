"""The little vocabulary the local tools share.

Each tool has its own rules, but they all answer the model the same way — with a
result, or with why there is none — and they all read their arguments the same
way.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Answer:
    """What a tool tells the model: a result, or why there is none."""

    result: str = ""
    error: str = ""
    # What a rollback would have to delete: what the tool put into the chat.
    forwarded: int | None = None
    # Pictures that go with the result, as `data:` URIs.
    images: list[str] = field(default_factory=list)
    # The tool to run next, `{"name": …, "arguments": …}`, instead of reporting
    # this result: the harness runs it and the model sees that tool's output.
    # `result` then says what this step did, for the pipe's trace alone.
    call: dict | None = None


def text_argument(arguments: dict, field: str) -> tuple[str, str | None]:
    """A required string a tool was given, or what is wrong with it."""
    value = arguments.get(field)
    if not isinstance(value, str) or not value.strip():
        return "", f"`{field}` must be a non-empty string"
    return value.strip(), None


def chat_argument(arguments: dict, field: str = "from_chat") -> tuple[object | None, str | None]:
    """The chat a tool was pointed at, or None when it meant the current one.

    A chat is named the way Telegram names it: a public `@name`, or the numeric id
    the reading tools print in their header.
    """
    value = arguments.get(field)
    if value is None or value == "":
        return None, None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None, f"`{field}` must be a chat name like `@telegram` or a chat id"
    if isinstance(value, int):
        return value, None
    named = value.strip()
    if not named:
        return None, None
    if named.startswith("@"):
        return named, None
    try:
        return int(named), None
    except ValueError:
        return None, f"`{field}` must be a chat name like `@telegram` or a chat id"


def message_id_argument(arguments: dict) -> tuple[int, str | None]:
    """The message id a tool was given, however it was written, or what is wrong."""
    value = arguments.get("message_id")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0, "`message_id` must be a number"
    try:
        message_id = int(value)
    except ValueError:
        return 0, "`message_id` must be a number"
    if message_id <= 0:
        return 0, "`message_id` must be a positive number"
    return message_id, None

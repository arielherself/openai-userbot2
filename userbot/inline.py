"""The tools that reach another account's inline menu.

An inline menu is what a bot offers when its `@name` is typed into a chat: a
list of things it can send. `tg_open_inline_menu` asks a bot for that list —
with a query after its name, or with nothing typed — and hands the agent what it
holds, numbered. `tg_send_inline_result` picks one of those entries and sends it
into this chat, from this account, attributed to the bot.

Unlike the bots the relay tools talk to, an inline menu is not opened by sending
a message and waiting for an answer: one request fetches it, and sending an
entry is a pick *from that fetch* — Telegram keeps the fetched results only for a
while, and the pick has to name the query they came from. So the menu is held
here between the two calls, under an id the agent carries from the first tool to
the second. A menu that was never opened, or whose time has run out, cannot be
picked from; opening it again gets a fresh one.
"""

from __future__ import annotations

import logging
import os
import time

from .telegram import Delivery, InlineMenu
from .tools import Answer

log = logging.getLogger(__name__)

# How long a fetched menu may be picked from, and how many are held at once. The
# pick comes moments after the fetch, so these bounds only have to outlast a
# turn — Telegram's own patience with the fetched results is shorter anyway.
MENU_TTL = 10 * 60.0
MAX_MENUS = 20

OPEN_TOOL = {
    "name": "tg_open_inline_menu",
    "description": (
        "Open a bot's inline menu and get back what it offers, numbered — the "
        "same list a bot shows when its @name is typed into a chat.\n"
        "`bot`: the bot to ask, like `@like` or `@gif`. For films and TV shows "
        "there is `@pteebot`, whose menu searches for their resources.\n"
        "`query`: what to type after its name; leave it out for the menu the bot "
        "shows when nothing is typed.\n"
        "Pass the menu id this prints and one of the numbers to "
        "tg_send_inline_result to send that entry into this chat."
    ),
    "params": [
        {"name": "bot", "type": "string", "description": "the bot to ask, like `@like`"},
        {
            "name": "query",
            "type": "string",
            "description": "what to type after the bot's name; omit for the menu shown with nothing typed",
        },
    ],
}

SEND_TOOL = {
    "name": "tg_send_inline_result",
    "description": (
        "Send one entry of an inline menu into this chat, as if it were picked "
        "by hand: the message goes out from this account, attributed to the bot.\n"
        "`menu`: the menu id tg_open_inline_menu printed, like "
        "`menu-1a2b3c4d5e6f`.\n"
        "`index`: which entry, counting from 1 in the order the menu printed "
        "them.\n"
        "The menu has to be fresh: a fetched menu is only pickable for a while, "
        "and one that is gone has to be opened again. Returns a confirmation "
        "once the entry has been sent."
    ),
    "params": [
        {
            "name": "menu",
            "type": "string",
            "description": "the menu id tg_open_inline_menu printed",
        },
        {
            "name": "index",
            "type": "integer",
            "description": "which entry to send, counting from 1 as the menu listed it",
        },
    ],
    # The message really was sent, so a turn that fails afterwards takes it back.
    "rollback": True,
    "external_effects": True,
}

TOOLS = [OPEN_TOOL, SEND_TOOL]


def new_menu_id() -> str:
    """A menu id, short enough to read back and unlikely to repeat."""
    return f"menu-{os.urandom(6).hex()}"


class Menus:
    """The menus that were opened, kept for the pick that follows.

    Sending an entry is not another query: Telegram holds the fetched results
    only for a while, and the pick has to name the query they came from, so the
    menu is kept here between the two calls and let go once it is too old to
    pick from anyway.
    """

    def __init__(self, ttl: float = MENU_TTL, limit: int = MAX_MENUS) -> None:
        self.ttl = ttl
        self.limit = limit
        self._menus: dict[str, tuple[float, InlineMenu]] = {}

    def keep(self, menu: InlineMenu) -> str:
        """Hold a freshly fetched menu, answering with the id it is picked by."""
        self._forget_old()
        menu_id = new_menu_id()
        self._menus[menu_id] = (time.monotonic() + self.ttl, menu)
        while len(self._menus) > self.limit:
            oldest = min(self._menus, key=lambda key: self._menus[key][0])
            del self._menus[oldest]
        return menu_id

    def get(self, menu_id: str) -> InlineMenu | None:
        """The menu that id names, while it is still there to pick from."""
        self._forget_old()
        found = self._menus.get(menu_id)
        return None if found is None else found[1]

    def _forget_old(self) -> None:
        now = time.monotonic()
        for menu_id in [key for key, (expires, _) in self._menus.items() if expires <= now]:
            del self._menus[menu_id]


def open_arguments(arguments: dict) -> tuple[str, str, str | None]:
    """The bot and the text its menu was asked for, or what is wrong with them."""
    bot = arguments.get("bot")
    named = bot.strip().lstrip("@") if isinstance(bot, str) else ""
    if not named:
        return "", "", "`bot` must be a bot's name like `@like`"
    query = arguments.get("query")
    if query is None:
        query = ""
    if not isinstance(query, str):
        return "", "", "`query` must be a string; omit it for nothing typed"
    return named, query.strip(), None


def pick_arguments(arguments: dict) -> tuple[str, int, str | None]:
    """The menu and the entry a pick named, or what is wrong with them."""
    menu = arguments.get("menu")
    if not isinstance(menu, str) or not menu.strip():
        return "", 0, "`menu` must be the id tg_open_inline_menu printed"
    value = arguments.get("index")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return "", 0, "`index` must be the number of an entry, counting from 1"
    try:
        index = int(value)
    except ValueError:
        return "", 0, "`index` must be the number of an entry, counting from 1"
    if index < 1:
        return "", 0, "`index` counts from 1: the first entry the menu printed"
    return menu.strip(), index, None


def _asked(query: str) -> str:
    return f', asking for "{query}"' if query else ", with nothing typed"


def _count(entries: int) -> str:
    return f"{entries} entry" if entries == 1 else f"{entries} entries"


def _entry_line(entry) -> str:
    """One entry as the menu prints it: its title, its kind, then the bot's words."""
    title = entry.title.strip() or "(untitled)"
    kind = f" [{entry.type}]" if entry.type else ""
    said = f" — {entry.description.strip()}" if entry.description.strip() else ""
    return f"{title}{kind}{said}"


def render(menu_id: str, menu: InlineMenu) -> str:
    """The menu as the model reads it, numbered for the pick that follows."""
    lines = [f"menu {menu_id} (@{menu.bot}{_asked(menu.query)}), {_count(len(menu.entries))}:"]
    for number, entry in enumerate(menu.entries, start=1):
        lines.append(f"{number}. {_entry_line(entry)}")
    return "\n".join(lines)


async def open_menu(delivery: Delivery, menus: Menus, bot: str, query: str = "") -> Answer:
    """Ask a bot for its inline menu, and keep it for the pick that follows."""
    try:
        menu = await delivery.inline_query(bot, query)
    except Exception as error:
        return Answer(error=f"could not ask @{bot} for its menu: {error}")
    if not menu.entries:
        return Answer(result=f"@{bot} offers no entries{_asked(query)}")
    menu_id = menus.keep(menu)
    log.info("menu %s of @%s holds %d entries", menu_id, bot, len(menu.entries))
    return Answer(result=render(menu_id, menu))


async def send_result(
    delivery: Delivery, menus: Menus, menu_id: str, index: int, to_chat: int
) -> Answer:
    """Send one entry of a menu that was opened earlier into `to_chat`."""
    menu = menus.get(menu_id)
    if menu is None:
        return Answer(
            error=(
                f"there is no menu {menu_id}: it was never opened, or it is too "
                "old to pick from; run tg_open_inline_menu again"
            )
        )
    if index > len(menu.entries):
        return Answer(
            error=(
                f"the menu {menu_id} holds {_count(len(menu.entries))}; "
                f"there is no entry {index}"
            )
        )
    entry = menu.entries[index - 1]
    try:
        sent = await delivery.send_inline(menu, index - 1, to_chat)
    except Exception as error:
        return Answer(error=f"could not send that entry: {error}")
    log.info("sent entry %d of menu %s into chat %s", index, menu_id, to_chat)
    title = entry.title.strip() or "untitled"
    return Answer(
        result=f'entry {index} of menu {menu_id} ("{title}") was sent to the chat',
        forwarded=sent,
    )

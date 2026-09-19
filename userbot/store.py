"""The mapping between Telegram messages and harness agent blocks, and the tasks
waiting for their time.

Every message this userbot sends can be replied to, and a reply must continue the
conversation that produced it. So each sent message is recorded against the agent
block that answered it: `(chat_id, message_id) -> agent_id`, plus one root block
per chat, which is where a fresh conversation forks from.

A scheduled task is a row of its own: what to do, when, and the chat and message
its answer belongs to. Tasks are few and short-lived — one fires and is gone — so
they are kept apart from the message mappings, whose size budget never touches
them.

The file has a size budget. SQLite reuses the pages freed by a delete, so keeping
the *live* pages under the budget is what keeps the file from growing without
bound; the oldest mappings go first, because the newest are the ones a reply can
still land on.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 8 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    agent_id   TEXT    NOT NULL,
    created_at REAL    NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS messages_age ON messages (created_at);

CREATE TABLE IF NOT EXISTS roots (
    chat_id    INTEGER PRIMARY KEY,
    agent_id   TEXT    NOT NULL,
    created_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id         TEXT    PRIMARY KEY,
    content    TEXT    NOT NULL,
    due_at     REAL    NOT NULL,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    agent_id   TEXT    NOT NULL,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS schedules_due ON schedules (due_at);
"""


@dataclass(frozen=True)
class Schedule:
    """One task waiting for its time: what to do, when, and where it answers."""

    id: str
    content: str
    due_at: float
    chat_id: int
    message_id: int
    # The block that set the task, so it fires in that conversation.
    agent_id: str


class MappingStore:
    def __init__(self, path: str, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.evicted = 0
        self.con = sqlite3.connect(path)
        # Incremental auto-vacuum hands freed pages back to the filesystem, so
        # the file itself (not just its live pages) stays near the budget. An
        # existing database keeps its mode until a VACUUM rewrites it.
        if self.con.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            self.con.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self.con.execute("VACUUM")
        self.con.executescript(SCHEMA)
        self.con.commit()
        self._page_size = self.con.execute("PRAGMA page_size").fetchone()[0]

    # --- messages --------------------------------------------------------
    def lookup(self, chat_id: int, message_id: int) -> str | None:
        row = self.con.execute(
            "SELECT agent_id FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        return row[0] if row else None

    def record(self, chat_id: int, message_id: int, agent_id: str) -> None:
        self.record_many([(chat_id, message_id, agent_id)])

    def record_many(self, rows) -> None:
        rows = [
            (chat_id, message_id, agent_id, time.time()) for chat_id, message_id, agent_id in rows
        ]
        if not rows:
            return
        self.con.executemany(
            "INSERT OR REPLACE INTO messages "
            "(chat_id, message_id, agent_id, created_at) VALUES (?, ?, ?, ?)",
            rows,
        )
        self.con.commit()
        self._enforce_budget()

    def forget(self, chat_id: int, message_ids) -> None:
        message_ids = list(message_ids)
        if not message_ids:
            return
        self.con.executemany(
            "DELETE FROM messages WHERE chat_id = ? AND message_id = ?",
            [(chat_id, message_id) for message_id in message_ids],
        )
        self.con.commit()

    def count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    # --- per-chat conversation roots -------------------------------------
    def get_root(self, chat_id: int) -> str | None:
        row = self.con.execute(
            "SELECT agent_id FROM roots WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row[0] if row else None

    def set_root(self, chat_id: int, agent_id: str) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO roots (chat_id, agent_id, created_at) VALUES (?, ?, ?)",
            (chat_id, agent_id, time.time()),
        )
        self.con.commit()

    def forget_root(self, chat_id: int) -> None:
        self.con.execute("DELETE FROM roots WHERE chat_id = ?", (chat_id,))
        self.con.commit()

    # --- scheduled tasks --------------------------------------------------
    def add_schedule(self, task: Schedule) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO schedules "
            "(id, content, due_at, chat_id, message_id, agent_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task.id,
                task.content,
                task.due_at,
                task.chat_id,
                task.message_id,
                task.agent_id,
                time.time(),
            ),
        )
        self.con.commit()

    def schedules(self) -> list[Schedule]:
        """Every waiting task, the soonest first."""
        rows = self.con.execute(
            "SELECT id, content, due_at, chat_id, message_id, agent_id FROM schedules "
            "ORDER BY due_at, id"
        ).fetchall()
        return [Schedule(*row) for row in rows]

    def due_schedules(self, now: float) -> list[Schedule]:
        """The tasks whose time has come, the soonest first."""
        rows = self.con.execute(
            "SELECT id, content, due_at, chat_id, message_id, agent_id FROM schedules "
            "WHERE due_at <= ? ORDER BY due_at, id",
            (now,),
        ).fetchall()
        return [Schedule(*row) for row in rows]

    def next_due(self) -> float | None:
        """When the soonest task fires, or None when nothing is waiting."""
        row = self.con.execute("SELECT MIN(due_at) FROM schedules").fetchone()
        return row[0] if row and row[0] is not None else None

    def remove_schedule(self, task_id: str) -> bool:
        """Forget one task; True when it was there to forget."""
        cursor = self.con.execute("DELETE FROM schedules WHERE id = ?", (task_id,))
        self.con.commit()
        return cursor.rowcount > 0

    def count_schedules(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]

    # --- the size budget --------------------------------------------------
    def live_bytes(self) -> int:
        """What the data occupies, ignoring pages the file could reuse."""
        pages = self.con.execute("PRAGMA page_count").fetchone()[0]
        free = self.con.execute("PRAGMA freelist_count").fetchone()[0]
        return max(0, pages - free) * self._page_size

    def _enforce_budget(self) -> None:
        if self.max_bytes <= 0:
            return
        dropped = 0
        while self.live_bytes() > self.max_bytes:
            cursor = self.con.execute(
                "DELETE FROM messages WHERE rowid IN "
                "(SELECT rowid FROM messages ORDER BY created_at, rowid LIMIT ?)",
                (self._BATCH,),
            )
            self.con.commit()
            if cursor.rowcount == 0:
                break  # nothing left to drop; only the schema's own pages remain
            dropped += cursor.rowcount
            self.con.execute(f"PRAGMA incremental_vacuum({self._BATCH})")
        if dropped:
            self.evicted += dropped
            log.info(
                "mapping store over budget (%d bytes, limit %d): dropped %d oldest mapping(s)",
                self.live_bytes(),
                self.max_bytes,
                dropped,
            )

    _BATCH = 100

    def close(self) -> None:
        self.con.close()

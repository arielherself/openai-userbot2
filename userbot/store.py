"""The mapping between Telegram messages and harness agent blocks.

Every message this userbot sends can be replied to, and a reply must continue the
conversation that produced it. So each sent message is recorded against the agent
block that answered it: `(chat_id, message_id) -> agent_id`, plus one root block
per chat, which is where a fresh conversation forks from.

The file has a size budget. SQLite reuses the pages freed by a delete, so keeping
the *live* pages under the budget is what keeps the file from growing without
bound; the oldest mappings go first, because the newest are the ones a reply can
still land on.
"""

from __future__ import annotations

import logging
import sqlite3
import time

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
"""


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

"""SQLite access layer."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from database.migrations import apply_migrations
from database.models import Chat, ChatType, Kind, MediaItem

log = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not open.")
        return self._conn

    def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None = autocommit; transactions are explicit via transaction().
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            version = apply_migrations(conn)
        except Exception:
            conn.close()
            raise
        self._conn = conn
        log.info("Database ready (schema v%s)", version)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic block: commit on success, roll back on any exception."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- chats ---------------------------------------------------------------

    def register_chat(
        self,
        *,
        chat_id: int,
        title: str,
        chat_type: ChatType,
        username: str | None,
        folder_path: str,
    ) -> tuple[Chat, bool]:
        """Insert a chat, or refresh title/type/username if it is already registered.

        `folder_path` only applies to new rows: an existing chat keeps its original
        folder even if the chat is later renamed. Returns (chat, created).
        """
        now = _utc_now()
        with self.transaction():
            created = self.get_chat(chat_id) is None
            self.conn.execute(
                """
                INSERT INTO chats (chat_id, title, type, username, folder_path, registered_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    type = excluded.type,
                    username = excluded.username,
                    updated_at = excluded.updated_at
                """,
                (chat_id, title, chat_type.value, username, folder_path, now, now),
            )
            chat = self.get_chat(chat_id)
        if chat is None:  # cannot happen inside one transaction; guards the return type
            raise RuntimeError("Chat vanished after insert.")
        log.info("Chat %s %s (%s)", chat_id, "registered" if created else "updated", chat_type.value)
        return chat, created

    def get_chat(self, chat_id: int) -> Chat | None:
        row = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return Chat.from_row(row) if row else None

    def list_chats(self) -> list[Chat]:
        rows = self.conn.execute("SELECT * FROM chats ORDER BY title COLLATE NOCASE, chat_id").fetchall()
        return [Chat.from_row(r) for r in rows]

    # -- downloads -----------------------------------------------------------

    def completed_downloads(self, chat_id: int) -> dict[int, str]:
        """Map message_id -> relative file path for every finished download in a chat."""
        rows = self.conn.execute(
            "SELECT message_id, path FROM downloads WHERE chat_id = ? AND status = 'completed'",
            (chat_id,),
        ).fetchall()
        return {row["message_id"]: row["path"] for row in rows}

    def record_download(
        self,
        *,
        chat_id: int,
        message_id: int,
        status: str,
        path: str | None = None,
        size: int | None = None,
        error: str | None = None,
    ) -> None:
        """Save the outcome of one download immediately, so a crash never loses progress."""
        with self.transaction():
            self.conn.execute(
                """
                INSERT INTO downloads (chat_id, message_id, status, path, size, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    status = excluded.status,
                    path = excluded.path,
                    size = excluded.size,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (chat_id, message_id, status, path, size, error, _utc_now()),
            )

    # -- media index -----------------------------------------------------------

    def get_items(self, chat_id: int, kinds: Iterable[Kind]) -> list[MediaItem]:
        """Every indexed item of the given kinds in a chat, newest first."""
        values = [kind.value for kind in kinds]
        if not values:
            return []
        marks = ",".join("?" * len(values))
        rows = self.conn.execute(
            f"SELECT * FROM media WHERE chat_id = ? AND kind IN ({marks}) ORDER BY message_id DESC",
            (chat_id, *values),
        ).fetchall()
        return [MediaItem.from_row(row) for row in rows]

    def has_index(self, chat_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM scan_progress WHERE chat_id = ? LIMIT 1", (chat_id,)
        ).fetchone()
        return row is not None

    def get_checkpoint(self, chat_id: int, scan_key: str) -> int:
        """Newest message already covered by this search filter (0 if never scanned)."""
        row = self.conn.execute(
            "SELECT last_message_id FROM scan_progress WHERE chat_id = ? AND scan_key = ?",
            (chat_id, scan_key),
        ).fetchone()
        return row["last_message_id"] if row else 0

    def save_scan(self, chat_id: int, scan_key: str, items: list[MediaItem], last_message_id: int) -> None:
        """Store newly found items and advance this filter's checkpoint in one atomic step."""
        now = _utc_now()
        with self.transaction():
            self.conn.executemany(
                """
                INSERT INTO media (chat_id, message_id, kind, grp, filename, title, performer,
                                   size, duration, width, height, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    kind = excluded.kind,
                    grp = excluded.grp,
                    filename = excluded.filename,
                    title = excluded.title,
                    performer = excluded.performer,
                    size = excluded.size,
                    duration = excluded.duration,
                    width = excluded.width,
                    height = excluded.height,
                    fingerprint = excluded.fingerprint
                """,
                [
                    (chat_id, i.message_id, i.kind.value, i.group, i.filename, i.title, i.performer,
                     i.size, i.duration, i.width, i.height, i.fingerprint)
                    for i in items
                ],
            )
            self.conn.execute(
                """
                INSERT INTO scan_progress (chat_id, scan_key, last_message_id, scanned_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, scan_key) DO UPDATE SET
                    last_message_id = MAX(scan_progress.last_message_id, excluded.last_message_id),
                    scanned_at = excluded.scanned_at
                """,
                (chat_id, scan_key, last_message_id, now),
            )

    def forget_index(self, chat_id: int) -> None:
        """Drop a chat's index so the next scan starts from scratch."""
        with self.transaction():
            self.conn.execute("DELETE FROM media WHERE chat_id = ?", (chat_id,))
            self.conn.execute("DELETE FROM scan_progress WHERE chat_id = ?", (chat_id,))

    def remove_item(self, chat_id: int, message_id: int) -> None:
        with self.transaction():
            self.conn.execute(
                "DELETE FROM media WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
            )

"""Schema migrations, tracked with SQLite's PRAGMA user_version.

To change the schema, append a new (version, sql) entry. Never edit an entry
that has already shipped. Each migration runs in its own transaction.
"""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE chats (
            chat_id       INTEGER PRIMARY KEY,
            title         TEXT NOT NULL,
            type          TEXT NOT NULL CHECK (type IN ('private', 'bot', 'group', 'channel')),
            username      TEXT,
            folder_path   TEXT NOT NULL UNIQUE,
            registered_at TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE downloads (
            chat_id    INTEGER NOT NULL REFERENCES chats(chat_id),
            message_id INTEGER NOT NULL,
            status     TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
            path       TEXT,     -- relative to the downloads directory
            size       INTEGER,
            error      TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        );
        """,
    ),
    (
        3,
        """
        -- Index of every music track found, so a chat never has to be scanned from scratch again.
        CREATE TABLE tracks (
            chat_id    INTEGER NOT NULL REFERENCES chats(chat_id),
            message_id INTEGER NOT NULL,
            filename   TEXT NOT NULL,
            title      TEXT,
            performer  TEXT,
            size       INTEGER,
            duration   INTEGER,
            PRIMARY KEY (chat_id, message_id)
        );
        -- Newest music message already indexed; the next scan only looks past it.
        CREATE TABLE scan_state (
            chat_id         INTEGER PRIMARY KEY REFERENCES chats(chat_id),
            last_message_id INTEGER NOT NULL,
            scanned_at      TEXT NOT NULL
        );
        """,
    ),
    (
        4,
        """
        -- Every kind of media, not just music. `grp` is the Files sub-folder; `fingerprint` is the
        -- file-info key used to skip duplicates (NULL for documents, which are never deduplicated).
        CREATE TABLE media (
            chat_id     INTEGER NOT NULL REFERENCES chats(chat_id),
            message_id  INTEGER NOT NULL,
            kind        TEXT NOT NULL,
            grp         TEXT,
            filename    TEXT NOT NULL,
            title       TEXT,
            performer   TEXT,
            size        INTEGER,
            duration    INTEGER,
            width       INTEGER,
            height      INTEGER,
            fingerprint TEXT,
            PRIMARY KEY (chat_id, message_id)
        );
        CREATE INDEX media_by_kind ON media (chat_id, kind);

        -- One checkpoint per Telegram search filter ("music", "video", "photo", ...).
        CREATE TABLE scan_progress (
            chat_id         INTEGER NOT NULL REFERENCES chats(chat_id),
            scan_key        TEXT NOT NULL,
            last_message_id INTEGER NOT NULL,
            scanned_at      TEXT NOT NULL,
            PRIMARY KEY (chat_id, scan_key)
        );

        -- Keep everything the music-only version had already indexed.
        INSERT INTO media (chat_id, message_id, kind, grp, filename, title, performer, size, duration, fingerprint)
            SELECT chat_id, message_id, 'music', NULL, filename, title, performer, size, duration,
                   CASE WHEN size IS NULL THEN NULL
                        ELSE 'music|' || size || '|' || COALESCE(duration, '') || '|x|' || lower(filename) END
            FROM tracks;
        INSERT INTO scan_progress (chat_id, scan_key, last_message_id, scanned_at)
            SELECT chat_id, 'music', last_message_id, scanned_at FROM scan_state;
        DROP TABLE tracks;
        DROP TABLE scan_state;
        """,
    ),
]

LATEST_VERSION = MIGRATIONS[-1][0]


class MigrationError(Exception):
    """The database cannot be brought to the schema this version expects."""


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring the database up to date and return its schema version."""
    version = current_version(conn)
    if version > LATEST_VERSION:
        raise MigrationError(
            f"Database schema v{version} is newer than this program supports "
            f"(v{LATEST_VERSION}). Update the program instead of downgrading the database."
        )
    for target, sql in MIGRATIONS:
        if target <= version:
            continue
        log.info("Applying database migration v%s", target)
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {target};\nCOMMIT;")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"Migration v{target} failed: {exc}") from exc
        version = target
    return version

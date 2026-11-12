"""Plain data models shared between the database, scanner and downloader."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum


class ChatType(str, Enum):
    PRIVATE = "private"
    BOT = "bot"
    GROUP = "group"      # basic groups and supergroups
    CHANNEL = "channel"  # broadcast channels


class Kind(str, Enum):
    """What Telegram itself counts a piece of media as."""

    MUSIC = "music"
    VIDEOS = "videos"
    VIDEO_MESSAGES = "video_messages"  # round "video notes"
    GIFS = "gifs"
    VOICE = "voice"
    IMAGES = "images"                  # photos and image files together
    FILES = "files"                    # documents that are none of the above
    STICKERS = "stickers"


# Sub-folders of the Files folder.
GROUP_VIDEO_FILES = "Video files"
GROUP_DOCUMENTS = "Documents"
GROUP_ARCHIVES = "Archives"
GROUP_AUDIO = "Audio files"
GROUP_APPS = "Apps"
GROUP_OTHER = "Other"

# Items that get their own progress bar; everything else shares a one-line counter.
BAR_KINDS = {Kind.MUSIC, Kind.VIDEOS, Kind.VIDEO_MESSAGES}
BIG_FILE_BYTES = 25 * 1024 * 1024  # anything this large gets a bar so it never looks frozen


@dataclass(frozen=True)
class Chat:
    chat_id: int         # Telethon "marked" peer ID: users > 0, groups < 0, channels -100...
    title: str
    type: ChatType
    username: str | None
    folder_path: str     # relative to the downloads directory, so the archive stays relocatable
    registered_at: str   # ISO-8601, UTC
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Chat:
        return cls(
            chat_id=row["chat_id"],
            title=row["title"],
            type=ChatType(row["type"]),
            username=row["username"],
            folder_path=row["folder_path"],
            registered_at=row["registered_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class MediaItem:
    message_id: int
    kind: Kind
    group: str | None       # sub-folder inside the kind's folder (Files only)
    filename: str           # original Telegram file name, or a generated one (made filesystem-safe)
    title: str | None       # music metadata from Telegram
    performer: str | None
    size: int | None        # bytes
    duration: int | None    # seconds
    width: int | None
    height: int | None
    fingerprint: str | None  # file-info key for duplicate detection; None = never treated as a duplicate

    @property
    def display_name(self) -> str:
        if self.kind == Kind.MUSIC and self.title:
            return f"{self.performer} - {self.title}" if self.performer else self.title
        return self.filename

    @property
    def duration_text(self) -> str:
        if not self.duration:
            return ""
        minutes, seconds = divmod(int(self.duration), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"

    @property
    def show_bar(self) -> bool:
        """Big media gets a progress bar of its own; small files are only counted."""
        return (
            self.kind in BAR_KINDS
            or self.group == GROUP_VIDEO_FILES
            or (self.size or 0) >= BIG_FILE_BYTES
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> MediaItem:
        return cls(
            message_id=row["message_id"],
            kind=Kind(row["kind"]),
            group=row["grp"],
            filename=row["filename"],
            title=row["title"],
            performer=row["performer"],
            size=row["size"],
            duration=row["duration"],
            width=row["width"],
            height=row["height"],
            fingerprint=row["fingerprint"],
        )

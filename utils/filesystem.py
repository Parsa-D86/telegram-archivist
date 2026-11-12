"""Filesystem helpers: safe names, directory creation, permissions."""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_component(name: str, max_length: int = 60, fallback: str = "chat") -> str:
    """Make `name` safe as a single path component on Windows, macOS and Linux."""
    cleaned = unicodedata.normalize("NFC", name)
    cleaned = _ILLEGAL_CHARS.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    cleaned = cleaned[:max_length].rstrip(" .")
    return cleaned or fallback


def chat_folder_name(title: str, chat_id: int) -> str:
    """One folder per chat. The ID suffix keeps same-titled chats apart and survives renames."""
    return f"{sanitize_component(title)} [{chat_id}]"


def restrict_permissions(path: Path, *, is_dir: bool = False) -> None:
    """Best-effort owner-only permissions (effectively a no-op on Windows)."""
    try:
        os.chmod(path, 0o700 if is_dir else 0o600)
    except OSError as exc:
        log.debug("Could not restrict permissions on %s: %s", path, exc)


def ensure_dir(path: Path, *, private: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if private:
        restrict_permissions(path, is_dir=True)
    return path


# -- file names (as opposed to the chat folder names above) -------------------

_FILENAME_ILLEGAL_POSIX = re.compile(r"[/\x00-\x1f\x7f]")
_FILENAME_ILLEGAL_WINDOWS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# Linux limits a name to 255 bytes; leave room for " (99)" and the ".part" suffix.
MAX_FILENAME_BYTES = 240


def safe_filename(name: str, fallback: str = "audio") -> str:
    """Keep the original file name, changing only what the filesystem cannot store."""
    pattern = _FILENAME_ILLEGAL_WINDOWS if os.name == "nt" else _FILENAME_ILLEGAL_POSIX
    cleaned = pattern.sub("_", name).strip().lstrip(".")
    if not cleaned:
        return fallback
    stem, ext = os.path.splitext(cleaned)
    if len(ext.encode("utf-8")) > 20:  # not a real extension
        stem, ext = cleaned, ""
    while stem and len((stem + ext).encode("utf-8")) > MAX_FILENAME_BYTES:
        stem = stem[:-1]
    result = (stem + ext).strip()
    return result or fallback


def unique_path(directory: Path, filename: str, reserved: set[Path] | None = None) -> Path:
    """directory/filename, or 'name (2).ext', 'name (3).ext'... if that name is taken.

    `reserved` holds names already promised to downloads in progress, so two parallel
    downloads of same-named files never pick the same path.
    """
    taken = reserved if reserved is not None else set()

    def free(path: Path) -> bool:
        return path not in taken and not path.exists()

    candidate = directory / filename
    if free(candidate):
        return candidate
    stem, ext = os.path.splitext(filename)
    number = 2
    while True:
        candidate = directory / f"{stem} ({number}){ext}"
        if free(candidate):
            return candidate
        number += 1

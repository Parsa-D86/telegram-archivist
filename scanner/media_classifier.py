"""Decide what a message's media is, using what Telegram itself says about it.

Telegram marks documents with attributes (audio, video, round video, animated,
sticker, voice). Those attributes decide the kind, checked from the most
specific to the least. Only documents with none of them are plain "files", and
those are sorted into sub-folders by type (video files, documents, archives...).
Photos and image files are one kind: images.
"""
from __future__ import annotations

import os

from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

from database.models import (
    GROUP_APPS,
    GROUP_ARCHIVES,
    GROUP_AUDIO,
    GROUP_DOCUMENTS,
    GROUP_OTHER,
    GROUP_VIDEO_FILES,
    Kind,
    MediaItem,
)
from utils.filesystem import safe_filename

_MIME_EXTENSIONS = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "audio/aac": ".aac", "audio/flac": ".flac", "audio/x-flac": ".flac", "audio/ogg": ".ogg",
    "audio/opus": ".opus", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-matroska": ".mkv", "video/x-msvideo": ".avi",
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "image/heic": ".heic", "image/heif": ".heif", "image/bmp": ".bmp", "image/tiff": ".tiff",
    "image/avif": ".avif", "image/svg+xml": ".svg",
    "application/x-tgsticker": ".tgs", "application/pdf": ".pdf", "application/zip": ".zip",
}
_DEFAULT_EXTENSION = {
    Kind.MUSIC: ".mp3", Kind.VIDEOS: ".mp4", Kind.VIDEO_MESSAGES: ".mp4", Kind.GIFS: ".mp4",
    Kind.VOICE: ".ogg", Kind.IMAGES: ".jpg", Kind.FILES: ".bin", Kind.STICKERS: ".webp",
}
_PREFIX = {
    Kind.MUSIC: "audio", Kind.VIDEOS: "video", Kind.VIDEO_MESSAGES: "video_message", Kind.GIFS: "gif",
    Kind.VOICE: "voice", Kind.IMAGES: "image", Kind.FILES: "file", Kind.STICKERS: "sticker",
}
# These never have a meaningful name of their own, so they always get date + message id names.
_ALWAYS_GENERATED = {Kind.VOICE, Kind.VIDEO_MESSAGES, Kind.STICKERS}

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif", ".svg", ".ico"}
_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts", ".ogv", ".vob"}
_AUDIO_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".opus", ".aac", ".wma", ".aiff", ".ape", ".mka"}
_ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".iso", ".cab", ".lz", ".z"}
_APP_EXTS = {".apk", ".xapk", ".exe", ".msi", ".deb", ".rpm", ".dmg", ".appimage", ".pkg", ".jar"}
_DOC_EXTS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".txt", ".rtf",
    ".epub", ".mobi", ".azw3", ".fb2", ".djvu", ".csv", ".json", ".xml", ".md", ".html", ".htm", ".log",
}
_ARCHIVE_MIMES = ("zip", "rar", "7z", "x-tar", "gzip", "x-bzip", "x-xz", "zstd")


def _clean(text: str | None) -> str | None:
    text = (text or "").strip()
    return text or None


def _extension(file, kind: Kind) -> str:
    mime = (file.mime_type or "").lower()
    return _MIME_EXTENSIONS.get(mime) or file.ext or _DEFAULT_EXTENSION[kind]


def _file_group(name: str, mime: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if mime.startswith("video/") or ext in _VIDEO_EXTS:
        return GROUP_VIDEO_FILES
    if mime.startswith("audio/") or ext in _AUDIO_EXTS:
        return GROUP_AUDIO
    if ext in _ARCHIVE_EXTS or (mime.startswith("application/") and any(m in mime for m in _ARCHIVE_MIMES)):
        return GROUP_ARCHIVES
    if ext in _APP_EXTS or "android.package" in mime or "x-msdownload" in mime:
        return GROUP_APPS
    if ext in _DOC_EXTS or mime.startswith("text/") or mime == "application/pdf" or "officedocument" in mime \
            or "msword" in mime or "opendocument" in mime or mime in ("application/epub+zip", "application/json"):
        return GROUP_DOCUMENTS
    return GROUP_OTHER


def _document_kind(message, name: str, mime: str) -> Kind:
    # Order matters: a GIF or round video also carries a video attribute.
    if message.sticker:
        return Kind.STICKERS
    if message.gif:
        return Kind.GIFS
    if message.video_note:
        return Kind.VIDEO_MESSAGES
    if message.voice:
        return Kind.VOICE
    if message.audio:
        return Kind.MUSIC
    if message.video:
        return Kind.VIDEOS
    ext = os.path.splitext(name)[1].lower()
    if mime == "image/gif" or ext == ".gif":
        return Kind.GIFS
    if mime.startswith("image/") or ext in _IMAGE_EXTS:
        return Kind.IMAGES
    return Kind.FILES


def _fingerprint(kind: Kind, size: int | None, duration: int | None, width: int | None,
                 height: int | None, original_name: str) -> str | None:
    """File-info key: two items with the same key are treated as the same file. Documents are exempt."""
    if kind == Kind.FILES or not size:
        return None
    return "|".join((
        kind.value, str(size), str(duration or ""), f"{width or ''}x{height or ''}", original_name.lower()
    ))


def classify_message(message) -> MediaItem | None:
    """Return the MediaItem for a message's media, or None if it carries nothing archivable."""
    media = message.media
    is_photo = isinstance(media, MessageMediaPhoto)
    if is_photo:
        if message.photo is None:
            return None
    elif isinstance(media, MessageMediaDocument):
        if message.document is None:
            return None
    else:
        return None
    file = message.file
    if file is None:
        return None

    original = "" if is_photo else (file.name or "").strip()
    mime = (file.mime_type or "").lower()
    kind = Kind.IMAGES if is_photo else _document_kind(message, original, mime)

    title = _clean(file.title) if kind == Kind.MUSIC else None
    performer = _clean(file.performer) if kind == Kind.MUSIC else None
    extension = _extension(file, kind)

    if is_photo or kind in _ALWAYS_GENERATED or not original:
        if kind == Kind.MUSIC:
            base = " - ".join(part for part in (performer, title) if part) or f"audio {message.id}"
        else:
            stamp = message.date.strftime("%Y-%m-%d_%H-%M-%S")
            base = f"{_PREFIX[kind]}_{stamp}_{message.id}"
        filename, named = safe_filename(base + extension), False
    else:
        name = original if os.path.splitext(original)[1] else original + extension
        filename, named = safe_filename(name), True

    group = _file_group(filename, mime) if kind == Kind.FILES else None
    size = file.size
    duration = int(file.duration) if file.duration else None
    width, height = file.width, file.height
    return MediaItem(
        message_id=message.id,
        kind=kind,
        group=group,
        filename=filename,
        title=title,
        performer=performer,
        size=size,
        duration=duration,
        width=width,
        height=height,
        fingerprint=_fingerprint(kind, size, duration, width, height, original if named else ""),
    )

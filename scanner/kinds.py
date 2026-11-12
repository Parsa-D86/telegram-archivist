"""What the user can archive: labels, folders, which Telegram search filters find it."""
from __future__ import annotations

from dataclasses import dataclass

from database.models import Kind


@dataclass(frozen=True)
class KindInfo:
    label: str
    hint: str
    folder: str                # folder inside the chat's folder
    scans: tuple[str, ...]     # Telegram search filters that surface this kind (see history_scanner)


KINDS: dict[Kind, KindInfo] = {
    Kind.MUSIC: KindInfo("Music", "audio Telegram marks as music", "Music", ("music",)),
    Kind.VIDEOS: KindInfo("Videos", "videos as Telegram counts them", "Videos", ("video",)),
    Kind.VIDEO_MESSAGES: KindInfo("Video messages", "round video notes", "Video messages", ("round",)),
    Kind.GIFS: KindInfo("GIFs", "animated GIFs", "GIFs", ("gif",)),
    Kind.VOICE: KindInfo("Voice messages", "voice notes", "Voice messages", ("voice",)),
    Kind.IMAGES: KindInfo("Images", "photos and image files", "Images", ("photo", "document")),
    Kind.FILES: KindInfo(
        "Files", "documents, archives, video files, ... (sorted into sub-folders)", "Files", ("document",)
    ),
    Kind.STICKERS: KindInfo(
        "Stickers", "slow: needs a full scan of the chat history", "Stickers", ("all",)
    ),
}

MENU_ORDER: list[Kind] = [
    Kind.MUSIC, Kind.VIDEOS, Kind.VIDEO_MESSAGES, Kind.GIFS,
    Kind.VOICE, Kind.IMAGES, Kind.FILES, Kind.STICKERS,
]
DOWNLOAD_ORDER: list[Kind] = MENU_ORDER  # lighter classes first would also work; keep the menu order


def scan_keys_for(kinds: list[Kind]) -> list[str]:
    """The search filters needed to find these kinds, each listed once, in a stable order."""
    keys: list[str] = []
    for kind in kinds:
        for key in KINDS[kind].scans:
            if key not in keys:
                keys.append(key)
    return keys


def parse_kind_selection(text: str) -> list[Kind]:
    """'1,3,5', '2-4', '1 6 7' or 'a' (everything except stickers) -> kinds in menu order.

    Raises ValueError with a message for the user when the text is not understood.
    """
    cleaned = text.strip().lower().replace(",", " ")
    if not cleaned:
        raise ValueError("Pick at least one number from the list.")
    if cleaned in ("a", "all"):
        return [kind for kind in MENU_ORDER if kind != Kind.STICKERS]

    chosen: set[int] = set()
    for token in cleaned.split():
        low, dash, high = token.partition("-")
        try:
            start = int(low)
            end = int(high) if dash else start
        except ValueError:
            raise ValueError(f"'{token}' is not a number from the list.") from None
        if not (1 <= start <= len(MENU_ORDER) and 1 <= end <= len(MENU_ORDER)) or start > end:
            raise ValueError(f"'{token}' is outside the list (1-{len(MENU_ORDER)}).")
        chosen.update(range(start, end + 1))
    return [MENU_ORDER[number - 1] for number in sorted(chosen)]

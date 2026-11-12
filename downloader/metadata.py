"""Carry Telegram's track metadata into the downloaded file.

Tags already embedded in the file are never touched. Telegram's title and
artist are written only into fields that are empty, so tracks whose tags were
stripped still end up with the right name and artist.
"""
from __future__ import annotations

import logging
from pathlib import Path

import mutagen

log = logging.getLogger(__name__)


def fill_missing_tags(path: Path, title: str | None, performer: str | None) -> bool:
    """Write Telegram's title/artist into empty tag fields. Returns True if the file changed.

    Best effort: formats mutagen cannot tag are left exactly as downloaded.
    """
    wanted = {key: value for key, value in (("title", title), ("artist", performer)) if value}
    if not wanted:
        return False
    try:
        audio = mutagen.File(str(path), easy=True)
        if audio is None:  # not a format mutagen recognizes
            return False
        if audio.tags is None:
            audio.add_tags()
        changed = False
        for key, value in wanted.items():
            if not audio.tags.get(key):
                audio.tags[key] = [value]
                changed = True
        if changed:
            audio.save()
        return changed
    except Exception as exc:  # never fail a finished download over tagging
        log.info("Could not write tags to %s: %s", path.name, exc)
        return False

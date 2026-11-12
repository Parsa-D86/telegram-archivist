"""Quiet, file-info based duplicate removal.

Two items are duplicates when their fingerprints match: same kind, size,
duration, dimensions and original file name, as Telegram reports them. Nothing
is downloaded or hashed. Documents (Files) have no fingerprint and are never
treated as duplicates. The index itself is untouched; this only trims the queue.
"""
from __future__ import annotations

from database.models import MediaItem


def plan_queue(items: list[MediaItem], done_ids: set[int]) -> tuple[list[MediaItem], int]:
    """Return (items still to download, newest first; number of duplicates skipped).

    The oldest copy of a file is the one kept. If a copy was already downloaded
    (even in an earlier run), every other copy of it is skipped.
    """
    seen = {item.fingerprint for item in items if item.message_id in done_ids and item.fingerprint}
    queue: list[MediaItem] = []
    duplicates = 0
    for item in sorted((i for i in items if i.message_id not in done_ids), key=lambda i: i.message_id):
        if item.fingerprint is not None:
            if item.fingerprint in seen:
                duplicates += 1
                continue
            seen.add(item.fingerprint)
        queue.append(item)
    queue.sort(key=lambda i: i.message_id, reverse=True)
    return queue, duplicates

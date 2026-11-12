"""Find the media in a chat, as fast as Telegram allows.

For each class the user picked, Telegram's own search filters (the same ones
behind the "Music", "Media", "Files", ... tabs of a chat) return only matching
messages, and only those newer than the newest one indexed last time. Every item
found is classified and saved in the database index, so repeat scans are quick.

Stickers have no search filter, so asking for them scans the whole history.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from telethon import TelegramClient, errors
from telethon.tl.types import (
    InputMessagesFilterDocument,
    InputMessagesFilterGif,
    InputMessagesFilterMusic,
    InputMessagesFilterPhotos,
    InputMessagesFilterRoundVideo,
    InputMessagesFilterVideo,
    InputMessagesFilterVoice,
)

from database.database import Database
from database.models import Kind, MediaItem
from scanner.kinds import scan_keys_for
from scanner.media_classifier import classify_message
from telegram_client.chat_resolver import flood_message, rpc_message
from telegram_client.client import TRANSIENT_ERRORS, connection_reset_guard, ensure_connected

SCAN_FILTERS = {
    "music": InputMessagesFilterMusic,
    "video": InputMessagesFilterVideo,
    "round": InputMessagesFilterRoundVideo,
    "gif": InputMessagesFilterGif,
    "voice": InputMessagesFilterVoice,
    "photo": InputMessagesFilterPhotos,
    "document": InputMessagesFilterDocument,
    "all": None,  # no filter: every message (needed for stickers)
}

PROGRESS_EVERY = 50
SCAN_ATTEMPTS = 4
WAIT_BETWEEN_REQUESTS = 0.3  # seconds; gentle on Telegram's rate limits


class ScanError(Exception):
    """Scanning failed; the message is safe to show to the user."""


async def scan_selected(
    client: TelegramClient,
    entity: object,
    chat_id: int,
    db: Database,
    kinds: list[Kind],
    *,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Scan for the given kinds, save what is found, and return how many new items turned up."""
    new_total = 0
    for key in scan_keys_for(kinds):
        checkpoint = db.get_checkpoint(chat_id, key)
        base = new_total

        def report(found: int, _base: int = base) -> None:
            if on_progress is not None:
                on_progress(_base + found)

        items = await _scan_key(client, entity, key, checkpoint, report)
        db.save_scan(chat_id, key, items, max([checkpoint, *(i.message_id for i in items)]))
        new_total += len(items)
    return new_total


async def _scan_key(
    client: TelegramClient,
    entity: object,
    key: str,
    min_id: int,
    on_progress: Callable[[int], None],
) -> list[MediaItem]:
    options: dict = {"limit": None, "min_id": min_id, "wait_time": WAIT_BETWEEN_REQUESTS}
    if SCAN_FILTERS[key] is not None:
        options["filter"] = SCAN_FILTERS[key]

    delay = 3.0
    for attempt in range(1, SCAN_ATTEMPTS + 1):
        found: dict[int, MediaItem] = {}
        scanned = 0
        try:
            await ensure_connected(client)
            with connection_reset_guard():
                async for message in client.iter_messages(entity, **options):
                    scanned += 1
                    item = classify_message(message)
                    if item is not None:
                        found[item.message_id] = item
                    if scanned % PROGRESS_EVERY == 0:
                        on_progress(len(found))
            on_progress(len(found))
            return sorted(found.values(), key=lambda i: i.message_id, reverse=True)
        except errors.FloodWaitError as exc:
            raise ScanError(flood_message(exc)) from exc
        except errors.RPCError as exc:
            if getattr(exc, "code", 0) < 500:
                raise ScanError(rpc_message(exc)) from exc
            failure: BaseException = exc  # Telegram-side hiccup: retry
        except TRANSIENT_ERRORS as exc:
            failure = exc
        if attempt == SCAN_ATTEMPTS:
            raise ScanError("the connection to Telegram kept failing; try again in a moment") from failure
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)
    raise ScanError("scan did not finish")  # unreachable; keeps type checkers happy

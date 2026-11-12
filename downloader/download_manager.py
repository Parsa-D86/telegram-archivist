Warning: truncated output (original token count: 5465)
Total output lines: 549

"""Downloader for every kind of media.

Big media (music, videos, video messages, video files, anything over 25 MB) is
downloaded one file at a time with 6 requests in flight, each file with its own
progress bar and a finished line that stays on screen. Small files (images,
GIFs, documents, voice messages...) share a single "downloaded (45 out of 144)"
line, and a few of them are fetched side by side. The total number of Telegram
requests in flight never exceeds PARALLEL_REQUESTS.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from rich.cells import cell_len, set_cell_size
from rich.console import Console
from rich.filesize import decimal
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Column
from rich.text import Text
from telethon import TelegramClient, errors

from database.database import Database
from database.models import Chat, Kind, MediaItem
from downloader.metadata import fill_missing_tags
from downloader.parallel import CHUNK_SIZE, IncompleteDownload, download_chunks
from scanner.kinds import KINDS
from telegram_client.client import TRANSIENT_ERRORS, connection_reset_guard, ensure_connected
from utils.filesystem import ensure_dir, unique_path

log = logging.getLogger(__name__)

T = TypeVar("T")

PARALLEL_REQUESTS = 6       # Telegram requests in flight at once, deliberately fixed
SMALL_FILE_WORKERS = 6      # small files fetched side by side (still within the request cap above)
NAME_WIDTH = 44
BAR_WIDTH = 30
REQUEST_ATTEMPTS = 6        # tries per request before the file is given up on
REQUEST_TIMEOUT = 60.0      # seconds a single request may take before it counts as stalled
WHOLE_FILE_TIMEOUT = 180.0  # photos are fetched whole by Telethon
FILE_ATTEMPTS = 2           # tries per file (each with fresh request retries)
RETRY_PASS_DELAY = 5        # seconds before failed files get a second chance
RESET_COOLDOWN = 60.0       # minimum seconds between forced connection refreshes
RESET_DRAIN_TIMEOUT = 70.0  # how long a refresh waits for in-flight requests to finish

# Errors meaning "the file reference went stale; fetch the message again".
_STALE_REFERENCE_ERRORS = tuple(
    getattr(errors, name)
    for name in ("FileReferenceExpiredError", "FilerefUpgradeNeededError")
    if hasattr(errors, name)
)


class DiskFullError(Exception):
    """No space left on the device. Finished downloads are kept; the run stops."""


class TrackUnavailable(Exception):
    """The message was deleted, or no longer carries media."""


@dataclass
class DownloadSummary:
    completed: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    seconds: float = 0.0

    def add(self, other: DownloadSummary) -> None:
        self.completed += other.completed
        self.failed += other.failed
        self.bytes_downloaded += other.bytes_downloaded
        self.seconds += other.seconds

    @property
    def average_speed(self) -> int:
        return int(self.bytes_downloaded / self.seconds) if self.seconds > 0 else 0


@dataclass
class _Run:
    """State of one kind's download."""

    chat: Chat
    entity: object
    base: Path
    label: str
    summary: DownloadSummary
    numbers: dict[int, int]       # message_id -> position among the bar items
    bar_total: int
    counter: int | None = None    # progress task of the one-line counter
    counter_total: int = 0
    counter_done: int = 0


class _Source:
    """Where an item lives. The message is re-fetched whenever its file reference goes stale."""

    def __init__(self, entity: object, message_id: int) -> None:
        self.entity = entity
        self.message_id = message_id
        self.message = None


class _Gate:
    """Lets in-flight requests finish and holds new ones back while connections are refreshed."""

    def __init__(self) -> None:
        self._open = asyncio.Event()
        self._open.set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._inflight = 0

    async def __aenter__(self) -> None:
        await self._open.wait()
        self._inflight += 1
        self._idle.clear()

    async def __aexit__(self, *_exc) -> None:
        self._inflight -= 1
        if self._inflight == 0:
            self._idle.set()

    async def pause_and_drain(self, timeout: float) -> None:
        self._open.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._idle.wait(), timeout)

    def resume(self) -> None:
        self._open.set()


# -- progress columns: a task is either a "bar" (one big file) or a "counter" (small files) ------

class _BarOnly(ProgressColumn):
    """Wraps a column and leaves it blank on the one-line counter."""

    def __init__(self, inner: ProgressColumn) -> None:
        super().__init__()
        self._inner = inner

    def render(self, task):
        return Text("") if task.fields.get("mode") == "counter" else self._inner.render(task)


class _BarOrCounter(ProgressColumn):
    """The progress bar for a file, or the "downloaded (x out of y)" text for the counter."""

    def __init__(self) -> None:
        super().__init__(table_column=Column(width=BAR_WIDTH, no_wrap=True, overflow="ellipsis"))
        self._bar = BarColumn(bar_width=BAR_WIDTH)

    def render(self, task):
        if task.fields.get("mode") == "counter":
            return Text(task.fields.get("text", ""), no_wrap=True, overflow="ellipsis")
        return self._bar.render(task)


# -- console lines -------------------------------------------------------------------

def _fit(label: str, width: int = NAME_WIDTH) -> str:
    """Pad or cut to exactly `width` terminal cells (handles wide characters)."""
    if cell_len(label) > width:
        label = set_cell_size(label, width - 1) + "…"
    return set_cell_size(label, width)


def _finished_line(label: str, size: int) -> Text:
    line = Text()
    line.append("✓", style="bold green")
    line.append(" " + _fit(label) + " ")
    line.append("━" * BAR_WIDTH, style="green")
    line.append(" 100% ")
    line.append(decimal(size))
    return line


def _problem_line(mark: str, style: str, label: str, reason: str) -> Text:
    line = Text()
    line.append(mark, style=f"bold {style}")
    line.append(" " + _fit(label) + " ")
    line.append(reason, style=style)
    return line


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, errors.FloodWaitError):
        return f"rate-limited by Telegram ({exc.seconds}s)"
    if isinstance(exc, errors.RPCError):
        return type(exc).__name__
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out"
    return str(exc) or type(exc).__name__


def _counter_text(done: int, total: int) -> str:
    return f"downloaded ({done} out of {total})"


class DownloadManager:
    def __init__(self, client: TelegramClient, db: Database, downloads_root: Path, console: Console) -> None:
        self._client = client
        self._db = db
        self._root = downloads_root
        self._console = console
        self._slots = asyncio.Semaphore(PARALLEL_REQUESTS)  # requests in flight, all files together
        self._gate = _Gate()
        self._reset_lock = asyncio.Lock()
        self._last_reset = float("-inf")
        self._flood_until = 0.0
        self._reserved: set[Path] = set()  # file names promised to downloads in progress

    # -- public ----------------------------------------------------------------------

    async def download_kind(
        self, chat: Chat, entity: object, kind: Kind, items: list[MediaItem]
    ) -> DownloadSummary:
        """Download every item of one kind into the chat's folder for that kind."""
        info = KINDS[kind]
        big = [item for item in items if item.show_bar]
        small = [item for item in items if not item.show_bar]
        run = _Run(
            chat=chat,
            entity=entity,
            base=ensure_dir(self._root / chat.folder_path / info.folder),
            label=info.label,
            summary=DownloadSummary(),
            numbers={item.message_id: n for n, item in enumerate(big, start=1)},
            bar_total=len(big),
        )
        started = time.monotonic()

        with self._progress() as progress:
            if small:
                run.counter_total = len(small)
                run.counter = progress.add_task(
                    info.label, total=len(small), mode="counter", text=_counter_text(0, len(small))
                )
            retry = await self._run_pass(progress, run, small, SMALL_FILE_WORKERS, final=False)
            retry += await self._run_pass(progress, run, big, 1, final=False)
            if retry:
                progress.console.print(
                    Text(f"Retrying {len(retry)} failed item(s) in {RETRY_PASS_DELAY}s...", style="yellow")
                )
                await asyncio.sleep(RETRY_PASS_DELAY)
                await self._ensure_connection()
                await self._run_pass(progress, run, [i for i in retry if not i.show_bar], SMALL_FILE_WORKERS, final=True)
                await self._run_pass(progress, run, [i for i in retry if i.show_bar], 1, final=True)
            if run.counter is not None:
                progress.remove_task(run.counter)  # the summary line printed by the caller replaces it

        run.summary.seconds = time.monotonic() - started
        return run.summary

    # -- passes and workers ----------------------------------------------------------

    def _progress(self) -> Progress:
        return Progress(
            SpinnerColumn(),
            TextColumn(
                "{task.description}",
                markup=False,  # file names…465 tokens truncated…son(exc)
                    log.info(
                        "Download failed (chat %s, message %s): %s",
                        run.chat.chat_id, item.message_id, reason, exc_info=True,
                    )
                    if final:
                        self._db.record_download(
                            chat_id=run.chat.chat_id, message_id=item.message_id, status="failed", error=reason
                        )
                        progress.console.print(_problem_line("✗", "red", label, reason))
                        run.summary.failed += 1
                    else:
                        retry.append(item)
                        if item.show_bar:
                            progress.console.print(_problem_line("↻", "yellow", label, f"{reason} (will retry)"))
                    continue

                if item.kind == Kind.MUSIC:
                    await asyncio.to_thread(fill_missing_tags, path, item.title, item.performer)
                self._db.record_download(
                    chat_id=run.chat.chat_id,
                    message_id=item.message_id,
                    status="completed",
                    path=str(path.relative_to(self._root)),
                    size=size,
                )
                run.summary.completed += 1
                run.summary.bytes_downloaded += size
                if item.show_bar:
                    progress.console.print(_finished_line(label, size))
                elif run.counter is not None:
                    run.counter_done += 1
                    progress.update(
                        run.counter,
                        completed=run.counter_done,
                        text=_counter_text(run.counter_done, run.counter_total),
                    )

        tasks = [asyncio.create_task(worker()) for _ in range(max(1, workers))]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if fatal:
            raise fatal[0]
        return retry

    async def _download_item(
        self, progress: Progress, run: _Run, item: MediaItem, label: str
    ) -> tuple[Path, int]:
        for attempt in range(1, FILE_ATTEMPTS + 1):
            try:
                return await self._download_once(progress, run, item, label)
            except (IncompleteDownload, *TRANSIENT_ERRORS):
                if attempt == FILE_ATTEMPTS:
                    raise
                await asyncio.sleep(3)
                await self._ensure_connection()
        raise RuntimeError("unreachable")

    async def _download_once(
        self, progress: Progress, run: _Run, item: MediaItem, label: str
    ) -> tuple[Path, int]:
        source = _Source(run.entity, item.message_id)
        await self._refresh_source(source)

        folder = ensure_dir(run.base / item.group) if item.group else run.base
        final = unique_path(folder, item.filename, self._reserved)
        self._reserved.add(final)
        part = final.with_name(final.name + ".part")  # incomplete files never carry the final name
        task = progress.add_task(label, total=item.size, mode="bar") if item.show_bar else None
        done = False
        try:
            document = source.message.document
            expected = (document.size if document is not None else None) or None
            if document is not None and expected:
                await download_chunks(
                    partial(self._fetch_chunk, source),
                    expected,
                    part,
                    workers=PARALLEL_REQUESTS,
                    on_bytes=(lambda count: progress.advance(task, count)) if task is not None else None,
                )
            else:  # photos (and documents of unknown size): Telethon fetches the whole file
                await self._download_whole(source, part, progress, task, document is not None)
                expected = None
            size = part.stat().st_size
            if size == 0 or (expected and size != expected):
                raise IncompleteDownload(f"received {size} of {expected} bytes")
            os.replace(part, final)
            done = True
            return final, size
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise DiskFullError("The disk is full.") from exc
            raise
        finally:
            if task is not None:
                progress.remove_task(task)
            part.unlink(missing_ok=True)
            if not done:
                self._reserved.discard(final)

    async def _download_whole(
        self, source: _Source, part: Path, progress: Progress, task: int | None, is_document: bool
    ) -> None:
        def on_progress(received: int, total: int) -> None:
            if task is not None:
                progress.update(task, completed=received, total=total or None)

        result = await self._retrying(
            lambda: self._client.download_media(source.message, file=str(part), progress_callback=on_progress),
            source=source,
            timeout=None if is_document else WHOLE_FILE_TIMEOUT,
        )
        if not result:
            raise IncompleteDownload("Telegram returned no data")
        if Path(result) != part:
            os.replace(result, part)

    # -- requests ---------------------------------------------------------------------

    async def _refresh_source(self, source: _Source) -> None:
        """(Re)fetch the message so its media carries a fresh file reference."""
        message = await self._retrying(
            lambda: self._client.get_messages(source.entity, ids=source.message_id), use_slot=False
        )
        if message is None or message.media is None:
            raise TrackUnavailable("the message was deleted or has no media")
        source.message = message

    async def _fetch_chunk(self, source: _Source, offset: int, length: int) -> bytes:
        async def request() -> bytes:
            document = source.message.document
            iterator = self._client.iter_download(
                document,
                offset=offset,
                limit=1,
                request_size=CHUNK_SIZE,
                file_size=document.size,
            )
            parts: list[bytes] = []
            try:
                async for part in iterator:
                    parts.append(part)
            finally:
                close = getattr(iterator, "close", None)
                if close is not None:
                    await close()  # hands the connection back to Telethon
            return b"".join(parts)

        return await self._retrying(request, source=source)

    async def _retrying(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        source: _Source | None = None,
        use_slot: bool = True,
        timeout: float | None = REQUEST_TIMEOUT,
    ) -> T:
        """Run one request, riding out timeouts, dropped connections, FloodWaits and stale file references."""
        delay = 2.0
        failures = 0
        for attempt in range(1, REQUEST_ATTEMPTS + 1):
            last = attempt == REQUEST_ATTEMPTS
            pause = self._flood_until - time.monotonic()
            if pause > 0:  # a FloodWait is in force for everyone
                await asyncio.sleep(pause)
            try:
                with connection_reset_guard():
                    async with self._gate:
                        async with self._slots if use_slot else contextlib.nullcontext():
                            return await asyncio.wait_for(operation(), timeout)
            except errors.FloodWaitError as exc:
                if last:
                    raise
                wait = exc.seconds + 1
                if self._flood_until < time.monotonic():
                    self._console.print(Text(f"  ! Telegram asked us to wait {wait}s, pausing...", style="yellow"))
                self._flood_until = max(self._flood_until, time.monotonic() + wait)
            except _STALE_REFERENCE_ERRORS:
                if last or source is None:
                    raise
                await self._refresh_source(source)
            except errors.RPCError as exc:
                if getattr(exc, "code", 0) < 500 or last:
                    raise
                await asyncio.sleep(delay)  # Telegram-side hiccup
                delay = min(delay * 2, 30.0)
            except TRANSIENT_ERRORS:
                if last:
                    raise
                failures += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
                if failures >= 2:
                    await self._refresh_connection("Connection problems")
        raise RuntimeError("unreachable")

    # -- connection health ---------------------------------------------------------------

    async def _ensure_connection(self) -> None:
        try:
            await ensure_connected(self._client)
        except TRANSIENT_ERRORS:
            log.info("Could not reconnect yet; the next attempt will try again")

    async def _refresh_connection(self, reason: str) -> None:
        """Drop and re-open the Telegram connections once requests keep failing or stalling."""
        async with self._reset_lock:
            now = time.monotonic()
            if now - self._last_reset < RESET_COOLDOWN:
                return  # another worker just did it
            self._last_reset = now
            self._console.print(Text(f"  ! {reason}; refreshing the Telegram connection...", style="yellow"))
            await self._gate.pause_and_drain(RESET_DRAIN_TIMEOUT)
            try:
                await self._client.disconnect()
                await ensure_connected(self._client)
            except TRANSIENT_ERRORS:
                log.info("Reconnect after refresh failed; retries continue", exc_info=True)
            finally:
                self._gate.resume()

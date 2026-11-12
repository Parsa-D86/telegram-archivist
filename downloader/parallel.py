"""Pipelined chunk download.

Telethon's built-in download asks for one 128 KB piece, waits for the answer,
then asks for the next. Over a proxy with a round trip of 0.3 s that caps the
speed near 400 KB/s, and when latency rises the speed collapses with it. Here
the file is split into 512 KB chunks (Telegram's maximum) and several are
requested at the same time, so the speed depends on bandwidth, not on latency.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

CHUNK_SIZE = 512 * 1024  # Telegram's maximum request size


class IncompleteDownload(Exception):
    """The data Telegram delivered does not match the expected size."""


async def download_chunks(
    fetch_chunk: Callable[[int, int], Awaitable[bytes]],
    size: int,
    destination: Path,
    *,
    workers: int,
    on_bytes: Callable[[int], None] | None = None,
) -> None:
    """Fetch `size` bytes with up to `workers` chunk requests in flight and write them to `destination`.

    fetch_chunk(offset, length) must return exactly `length` bytes. Chunks may arrive in any
    order; each is written at its own offset. The first failure cancels the rest and is re-raised.
    """
    chunk_count = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    if chunk_count == 0:
        destination.write_bytes(b"")
        return
    pending_indexes = iter(range(chunk_count))  # shared: each worker takes the next chunk

    with open(destination, "wb") as handle:
        handle.truncate(size)

        async def worker() -> None:
            for index in pending_indexes:
                offset = index * CHUNK_SIZE
                length = min(CHUNK_SIZE, size - offset)
                data = await fetch_chunk(offset, length)
                if len(data) != length:
                    raise IncompleteDownload(f"chunk at {offset}: got {len(data)} of {length} bytes")
                handle.seek(offset)
                handle.write(data)
                if on_bytes is not None:
                    on_bytes(length)

        tasks = [asyncio.create_task(worker()) for _ in range(min(max(workers, 1), chunk_count))]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

"""Telethon client construction, connection handling and sign-in."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import Callable

from telethon import TelegramClient
from telethon import utils as tl_utils

from utils.config import Credentials, ProxySettings
from utils.filesystem import restrict_permissions

log = logging.getLogger(__name__)

# Network-level failures worth retrying. Proxy errors from python-socks derive from OSError.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (OSError, asyncio.TimeoutError)
CONNECT_ATTEMPTS = 4


@contextlib.contextmanager
def connection_reset_guard():
    """Turn a stray CancelledError into a ConnectionError so the request is retried.

    When Telethon tears its connection down (for example because its internal updates loop
    crashed), every request in flight is cancelled and the caller sees a bare CancelledError,
    which looks exactly like the user quitting. A real cancellation (Ctrl+C, shutdown) is
    told apart by the task's own cancelling counter and still propagates.
    """
    try:
        yield
    except asyncio.CancelledError:
        cancelling = getattr(asyncio.current_task(), "cancelling", None)
        if cancelling is None or cancelling() > 0:
            raise
        raise ConnectionError("the Telegram connection was reset") from None


def disable_update_loop(client: TelegramClient) -> None:
    """Stop Telethon's background updates loop.

    This app never uses live updates, and the loop has crashed once with "Called reset_deadline
    on an entry for which we do not have state" (a Telethon 1.45.0 bug), which disconnects the
    whole client mid-request. Cancelling it after each connect avoids that entirely.
    """
    handle = getattr(client, "_updates_handle", None)
    if handle is not None and not handle.done():
        handle.cancel()


def session_file(session_path: Path) -> Path:
    """The on-disk Telethon session. Treat it like a password: it is a logged-in session."""
    return Path(f"{session_path}.session")


def clear_update_state(session_path: Path) -> None:
    """Forget the update states stored in the session.

    This app never listens for updates, but states saved by earlier runs make Telethon
    fetch the "difference" of every channel in your chat list in the background, which
    is hundreds of extra requests over the proxy competing with the downloads.
    """
    path = session_file(session_path)
    if not path.exists():
        return
    conn = sqlite3.connect(path, timeout=5)
    try:
        conn.execute("DELETE FROM update_state")
        conn.commit()
    except sqlite3.Error as exc:  # table missing or session busy: harmless
        log.info("Could not clear stored update state: %s", exc)
    finally:
        conn.close()


def build_client(credentials: Credentials, proxy: ProxySettings | None, session_path: Path) -> TelegramClient:
    """Create the client. Call this from inside a running event loop."""
    if proxy:
        log.info("Routing Telegram traffic through %s proxy %s:%s", proxy.protocol, proxy.host, proxy.port)
    return TelegramClient(
        str(session_path),
        credentials.api_id,
        credentials.api_hash,
        # Telethon sleeps through FloodWaits up to this many seconds and re-raises longer ones.
        flood_sleep_threshold=60,
        # We only read history on demand; skip the live update stream.
        receive_updates=False,
        # Patient with a slow or flaky proxy.
        timeout=20,
        connection_retries=6,
        request_retries=6,
        retry_delay=2,
        # All Telegram traffic (login, history, downloads) goes through the proxy when one is set.
        proxy=proxy.for_telethon() if proxy else None,
    )


async def ensure_connected(client: TelegramClient, attempts: int = 5) -> None:
    """Make sure the client is connected, reconnecting with growing pauses if it is not."""
    delay = 2.0
    for attempt in range(1, attempts + 1):
        if client.is_connected():
            return
        try:
            await client.connect()
            disable_update_loop(client)
            return
        except TRANSIENT_ERRORS:
            if attempt == attempts:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 20.0)


async def connect_with_retries(client: TelegramClient, notify: Callable[[str], None]) -> None:
    """First connection of the run, with a visible retry message instead of an instant failure."""
    delay = 3.0
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            await client.connect()
            disable_update_loop(client)
            return
        except TRANSIENT_ERRORS:
            if attempt == CONNECT_ATTEMPTS:
                raise
            notify(f"Could not reach Telegram (attempt {attempt}/{CONNECT_ATTEMPTS}); retrying in {int(delay)}s...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


async def sign_in(client: TelegramClient, session_path: Path, notify: Callable[[str], None]) -> str:
    """Connect and log in; returns the account's display name.

    The first login for an API ID asks in the terminal for the phone number, the login
    code and (if enabled) the 2FA password. Later runs reuse the saved session.
    """
    await connect_with_retries(client, notify)
    with connection_reset_guard():
        await client.start()
    disable_update_loop(client)
    restrict_permissions(session_file(session_path))
    me = await client.get_me()
    log.info("Signed in to Telegram")
    return tl_utils.get_display_name(me) or "your account"

"""Telegram Archivist.

Interactive: asks for your API credentials, an optional proxy and your login, then
which chat to look into and which kinds of media to archive (music, videos, video
messages, GIFs, voice messages, images, files, stickers). It scans the chat, shows
what it found, and downloads it into a folder per chat, sorted by kind.

Scans are incremental: everything found is indexed in the database, so a chat you
come back to only has its new messages scanned.

Usage:  python main.py
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.filesize import decimal
from rich.table import Table
from rich.text import Text

from database.database import Database
from database.migrations import MigrationError
from database.models import Chat, Kind, MediaItem
from downloader.download_manager import DiskFullError, DownloadManager, DownloadSummary
from scanner.dedupe import plan_queue
from scanner.history_scanner import ScanError, scan_selected
from scanner.kinds import DOWNLOAD_ORDER, KINDS, MENU_ORDER, parse_kind_selection
from telegram_client.chat_resolver import ChatInfo, ChatResolver, ChatResolverError
from telegram_client.client import TRANSIENT_ERRORS, build_client, clear_update_state, sign_in
from utils.config import DEFAULT_PROXY_PORT, AppPaths, Credentials, ProxySettings
from utils.filesystem import chat_folder_name, ensure_dir
from utils.logger import setup_logging
from utils.profiles import ProfileStore
from utils.prompts import StopRun, ask, ask_choice, ask_secret, ask_yes_no

PROJECT_ROOT = Path(__file__).resolve().parent

console = Console()
log = logging.getLogger("archivist")


@dataclass
class Plan:
    """What would be downloaded for one kind."""

    kind: Kind
    found: int
    done: int
    duplicates: int
    queue: list[MediaItem]

    @property
    def queue_bytes(self) -> int:
        return sum(item.size or 0 for item in self.queue)


# -- terminal helpers ----------------------------------------------------------

def say(message: str, style: str | None = None) -> None:
    """Print plain text (never parsed as markup, so names with [brackets] are safe)."""
    console.print(Text(message, style=style or ""))


def format_elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


# -- startup: credentials, proxy, login -----------------------------------------------

def collect_credentials() -> Credentials:
    say("\nTelegram API credentials", "bold")
    say("Get them at https://my.telegram.org -> 'API development tools'.", "dim")
    while True:
        api_id = ask("API ID")
        api_hash = ask_secret("API hash (typing is hidden)")
        try:
            return Credentials.parse(api_id, api_hash)
        except ValueError as exc:
            say(str(exc), "red")


def collect_proxy_details() -> ProxySettings:
    protocols = {"1": "socks5", "2": "socks4", "3": "http"}
    while True:
        choice = ask_choice("Protocol", {"1": "SOCKS5", "2": "SOCKS4", "3": "HTTP"})
        protocol = protocols[choice]
        host = ask("Proxy host or IP")
        port = ask("Proxy port", default=str(DEFAULT_PROXY_PORT[protocol]))
        username = ask("Username (leave empty if none)")
        password = ask_secret("Password (typing is hidden)") if username else ""
        try:
            return ProxySettings.parse(protocol, host, port, username, password)
        except ValueError as exc:
            say(str(exc), "red")


def collect_proxy() -> ProxySettings | None:
    if not ask_yes_no("\nConnect through a proxy?", default=False):
        return None
    return collect_proxy_details()


def _pick_number(answer: str, count: int) -> int | None:
    return int(answer) - 1 if answer.isdigit() and 1 <= int(answer) <= count else None


def choose_account(store: ProfileStore) -> Credentials:
    """Offer the saved accounts (most recent first); fall back to typing new credentials."""
    while True:
        accounts = store.accounts_by_recent()
        if not accounts:
            return collect_credentials()
        say("\nSaved accounts", "bold")
        for number, account in enumerate(accounts, start=1):
            say(f"  {number}) {account.label}  (API ID {account.api_id})")
        say("  n) Use new credentials")
        say("  d) Forget a saved account")
        answer = ask("Choose", default="1").lower()
        if answer == "n":
            return collect_credentials()
        if answer == "d":
            index = _pick_number(ask("Number of the account to forget"), len(accounts))
            if index is None:
                say("That is not a number from the list.", "red")
            else:
                store.forget_account(accounts[index].api_id)
                say("Forgotten.", "dim")
            continue
        index = _pick_number(answer, len(accounts))
        if index is not None:
            return accounts[index].credentials()
        say("Please choose a number from the list, n or d.", "red")


def choose_proxy(store: ProfileStore) -> ProxySettings | None:
    """Offer the saved proxies (most recent first), 'no proxy', or a new one."""
    while True:
        proxies = store.proxies_by_recent()
        if not proxies:
            return collect_proxy()
        say("\nSaved proxies", "bold")
        say("  0) No proxy")
        for number, proxy in enumerate(proxies, start=1):
            say(f"  {number}) {proxy.label}")
        say("  n) Use a new proxy")
        say("  d) Forget a saved proxy")
        default = "0"
        for number, proxy in enumerate(proxies, start=1):
            if proxy.key == store.last_proxy:
                default = str(number)  # what worked last time
        answer = ask("Choose", default=default).lower()
        if answer == "0":
            return None
        if answer == "n":
            return collect_proxy_details()
        if answer == "d":
            index = _pick_number(ask("Number of the proxy to forget"), len(proxies))
            if index is None:
                say("That is not a number from the list.", "red")
            else:
                store.forget_proxy(proxies[index].key)
                say("Forgotten.", "dim")
            continue
        index = _pick_number(answer, len(proxies))
        if index is not None:
            return proxies[index].settings()
        say("Please choose a number from the list, 0, n or d.", "red")


async def login(paths: AppPaths, store: ProfileStore):
    """Pick or enter credentials and proxy, then connect and log in, offering fixes when that fails."""
    credentials = choose_account(store)
    proxy = choose_proxy(store)
    while True:
        session_path = paths.session_path(credentials.api_id)
        clear_update_state(session_path)
        client = build_client(credentials, proxy, session_path)
        via = f" through {proxy.describe()}" if proxy else ""
        say(f"\nConnecting to Telegram{via}...")
        say("The first login for these credentials asks for your phone number and the code Telegram sends.", "dim")
        try:
            name = await sign_in(client, session_path, notify=lambda message: say(message, "yellow"))
            store.remember(credentials, name, proxy)  # saved only now that it is known to work
            return client, name
        except Exception as exc:
            with contextlib.suppress(Exception):
                await client.disconnect()
            if isinstance(exc, StopRun):
                raise
            problem = type(exc).__name__
            if problem == "ApiIdInvalidError":
                say("Telegram rejected this API ID / hash. Please enter them again.", "red")
                credentials = collect_credentials()
                continue
            log.info("Login did not succeed: %s", problem, exc_info=True)
            if isinstance(exc, TRANSIENT_ERRORS):
                hint = " Is the proxy running and the address right?" if proxy else " Check your internet connection."
                say(f"Could not reach Telegram{via} ({problem}).{hint}", "red")
            else:
                say(f"Login did not succeed ({problem}: {exc}).", "red")
            action = ask_choice(
                "What now?", {"r": "retry", "c": "change proxy", "k": "change account", "q": "quit"}
            )
            if action == "q":
                raise StopRun from None
            if action == "c":
                proxy = choose_proxy(store)
            elif action == "k":
                credentials = choose_account(store)


# -- choosing the chat and what to archive ------------------------------------------------

async def choose_chat(resolver: ChatResolver) -> ChatInfo:
    say("\nWhich chat do you want to archive?", "bold")
    say("  - a numeric chat ID, a @username, or a t.me link")
    say("  - 1 for your Saved Messages")
    say("Your account must have joined the chat, or otherwise have access to it.", "yellow")
    while True:
        text = ask("Chat (q to quit)")
        if text.lower() in ("q", "quit", "exit"):
            raise StopRun
        if not text:
            continue
        try:
            with console.status("Looking up the chat..."):
                info = await resolver.resolve("me" if text == "1" else text)
        except ChatResolverError as exc:
            say(f"Could not use that chat: {exc}", "red")
            continue
        except Exception as exc:
            log.info("Chat lookup failed", exc_info=True)
            say(f"Could not look that up ({type(exc).__name__}). Please try again.", "red")
            continue
        say(f"Found: {info.title} ({info.type.value})", "green")
        return info


def choose_kinds() -> list[Kind]:
    say("\nWhat do you want to archive?", "bold")
    for number, kind in enumerate(MENU_ORDER, start=1):
        info = KINDS[kind]
        say(f"  {number}) {info.label:<15} {info.hint}")
    say("Enter numbers like 1,3,5 or 2-4, or 'a' for everything except stickers.", "dim")
    while True:
        try:
            return parse_kind_selection(ask("Your choice"))
        except ValueError as exc:
            say(str(exc), "red")


# -- planning and presenting what was found ---------------------------------------------

def downloaded_message_ids(db: Database, chat: Chat, downloads_root: Path) -> set[int]:
    """Messages already downloaded whose files are still on disk."""
    return {
        message_id
        for message_id, relative_path in db.completed_downloads(chat.chat_id).items()
        if relative_path and (downloads_root / relative_path).is_file()
    }


def build_plans(db: Database, chat: Chat, kinds: list[Kind], downloads_root: Path) -> list[Plan]:
    done_ids = downloaded_message_ids(db, chat, downloads_root)
    plans = []
    for kind in sorted(kinds, key=DOWNLOAD_ORDER.index):
        items = db.get_items(chat.chat_id, [kind])
        queue, duplicates = plan_queue(items, done_ids)
        done = sum(1 for item in items if item.message_id in done_ids)
        plans.append(Plan(kind, len(items), done, duplicates, queue))
    return plans


def print_summary(plans: list[Plan]) -> None:
    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("Type")
    for title in ("Found", "Downloaded", "Duplicates", "To download", "Size"):
        table.add_column(title, justify="right")
    for plan in plans:
        table.add_row(
            KINDS[plan.kind].label,
            str(plan.found),
            str(plan.done),
            str(plan.duplicates),
            str(len(plan.queue)),
            decimal(plan.queue_bytes) if plan.queue else "-",
        )
    console.print(table)


def print_item_lists(plans: list[Plan]) -> None:
    for plan in plans:
        if not plan.queue:
            continue
        say(f"\n{KINDS[plan.kind].label} ({len(plan.queue)})", "bold")
        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("#", justify="right", style="dim")
        table.add_column("Name", no_wrap=True, overflow="ellipsis", max_width=70)
        if plan.kind == Kind.FILES:
            table.add_column("Folder")
        table.add_column("Time", justify="right")
        table.add_column("Size", justify="right")
        for number, item in enumerate(plan.queue, start=1):
            row = [str(number), Text(item.display_name)]
            if plan.kind == Kind.FILES:
                row.append(item.group or "")
            row += [item.duration_text, decimal(item.size) if item.size else "?"]
            table.add_row(*row)
        console.print(table)


def confirm_download(plans: list[Plan]) -> bool:
    count = sum(len(plan.queue) for plan in plans)
    size = decimal(sum(plan.queue_bytes for plan in plans))
    while True:
        choice = ask_choice(f"\nDownload {count} items ({size})?", {"y": "yes", "n": "no", "l": "list them first"})
        if choice == "l":
            print_item_lists(plans)
        else:
            return choice == "y"


# -- archiving one chat ---------------------------------------------------------------

async def archive_chat(
    info: ChatInfo, client, db: Database, manager: DownloadManager, paths: AppPaths
) -> None:
    chat, _created = db.register_chat(
        chat_id=info.chat_id,
        title=info.title,
        chat_type=info.type,
        username=info.username,
        folder_path=chat_folder_name(info.title, info.chat_id),
    )
    kinds = choose_kinds()
    if db.has_index(chat.chat_id) and ask_yes_no(
        "\nThis chat was scanned before. Rescan everything from scratch?", default=False
    ):
        db.forget_index(chat.chat_id)

    with console.status("Scanning...") as status:
        def on_progress(found: int) -> None:
            status.update(Text(f"Scanning: {found:,} new items found"))

        try:
            new_count = await scan_selected(client, info.entity, chat.chat_id, db, kinds, on_progress=on_progress)
        except ScanError as exc:
            say(f"Scan failed: {exc}", "red")
            return

    plans = build_plans(db, chat, kinds, paths.downloads_dir)
    say(f"\nScan finished: {new_count} new items since the last scan.", "bold")
    print_summary(plans)
    if not any(plan.queue for plan in plans):
        say("\nNothing to download.")
        return
    if not confirm_download(plans):
        say("Skipped.")
        return

    totals = DownloadSummary()
    for plan in plans:
        if not plan.queue:
            continue
        console.rule(Text(KINDS[plan.kind].label, style="bold"))
        summary = await manager.download_kind(chat, info.entity, plan.kind, plan.queue)
        totals.add(summary)
        speed = f", averaging {decimal(summary.average_speed)}/s" if summary.average_speed else ""
        say(
            f"{KINDS[plan.kind].label}: {summary.completed} downloaded ({decimal(summary.bytes_downloaded)}) "
            f"in {format_elapsed(summary.seconds)}{speed}" + (f", {summary.failed} failed" if summary.failed else "") + ".",
            "green" if not summary.failed else "yellow",
        )

    console.rule()
    say(
        f"Done: {totals.completed} downloaded ({decimal(totals.bytes_downloaded)}), {totals.failed} failed.",
        "bold",
    )
    say(f"Saved in: {paths.downloads_dir / chat.folder_path}")
    if totals.failed:
        say("Failed items are retried the next time you archive this chat.")


# -- startup / shutdown ------------------------------------------------------------

async def amain() -> int:
    # asyncio.run() (3.11+) turns the first Ctrl+C into a task cancellation, which a blocking
    # input() never notices. Restore the default handler so Ctrl+C raises immediately.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    paths = AppPaths(PROJECT_ROOT)
    log_file = setup_logging(paths.logs_dir, "INFO", console)
    ensure_dir(paths.data_dir, private=True)  # holds the database and the login sessions

    db = Database(paths.database_path)
    try:
        db.open()
    except (sqlite3.Error, MigrationError, OSError) as exc:
        log.exception("Could not open the database")
        print(f"Could not open the database: {exc}", file=sys.stderr)
        return 1

    client = None
    try:
        say("Telegram Archivist", "bold")
        client, name = await login(paths, ProfileStore(paths.data_dir / "profiles.json"))
        say(f"Signed in as {name}.", "green")

        resolver = ChatResolver(client, status=log.info)
        manager = DownloadManager(client, db, paths.downloads_dir, console)
        while True:
            info = await choose_chat(resolver)
            try:
                await archive_chat(info, client, db, manager, paths)
            except (StopRun, DiskFullError):
                raise
            except Exception:
                log.exception("Unexpected error while archiving a chat")
                say(f"Something went wrong with this chat; details are in {log_file}.", "red")
            if not ask_yes_no("\nArchive another chat?", default=False):
                break
        return 0
    except StopRun:
        say("\nGoodbye.")
        return 0
    except DiskFullError:
        say("The disk is full. Stopping; everything already downloaded is kept.", "bold red")
        return 1
    except Exception as exc:
        log.exception("Fatal error")
        print(f"Fatal error ({type(exc).__name__}): {exc}\nDetails: {log_file}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
        db.close()


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        print("\nInterrupted. Finished downloads are kept; unfinished files were removed.")
        return 130


if __name__ == "__main__":
    sys.exit(main())

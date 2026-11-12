"""Resolve user-supplied references to Telegram chats and verify access.

Numeric IDs only resolve when the entity is in Telethon's session cache. That
cache is filled whenever dialogs are fetched, so when an ID is not found we
refresh the dialog list once and retry.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Callable

from telethon import TelegramClient, errors
from telethon.tl import functions
from telethon.tl import types as tl_types
from telethon import utils as tl_utils
from telethon.tl.types import Channel, ChannelForbidden, Chat, ChatForbidden, User

from database.models import ChatType
from telegram_client.client import TRANSIENT_ERRORS, connection_reset_guard, ensure_connected
from telegram_client.references import InvalidReference, parse_reference

log = logging.getLogger(__name__)

MAX_DIALOG_PAGES = 60  # 6,000 chats: far more than any account has


class ChatResolverError(Exception):
    """Base class. The message is written for the person at the terminal."""


class ChatNotFound(ChatResolverError):
    pass


class ChatNotAccessible(ChatResolverError):
    pass


@dataclass(frozen=True)
class ChatInfo:
    chat_id: int  # Telethon "marked" peer ID (same convention as the Bot API)
    title: str
    type: ChatType
    username: str | None = None
    approx_messages: int | None = None  # informational only, never stored
    entity: object | None = field(default=None, repr=False, compare=False)  # live Telethon entity


def describe_entity(entity: object) -> ChatInfo | None:
    """Classify a Telethon entity; None for anything we do not archive."""
    if isinstance(entity, User):
        chat_type = ChatType.BOT if entity.bot else ChatType.PRIVATE
    elif isinstance(entity, Channel):
        chat_type = ChatType.GROUP if entity.megagroup else ChatType.CHANNEL
    elif isinstance(entity, Chat):
        chat_type = ChatType.GROUP
    else:
        return None

    username = getattr(entity, "username", None)
    if isinstance(entity, User) and getattr(entity, "is_self", False):
        title = "Saved Messages"
    else:
        title = tl_utils.get_display_name(entity) or (f"@{username}" if username else f"chat {entity.id}")
    return ChatInfo(
        chat_id=tl_utils.get_peer_id(entity),
        title=title,
        type=chat_type,
        username=username,
        entity=entity,
    )


def _humanize(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} seconds"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    return f"{seconds / 3600:.1f} hours"


def flood_message(exc: errors.FloodWaitError) -> str:
    return f"Telegram is rate-limiting this account. Try again in about {_humanize(exc.seconds)}."


_RPC_HINTS = {
    "ChannelPrivateError": "That channel or group is private, or this account was removed from it.",
    "InviteHashExpiredError": "That invite link has expired.",
    "InviteHashInvalidError": "That invite link is not valid.",
    "UsernameInvalidError": "That username is not valid.",
    "UserBannedInChannelError": "This account is banned from that chat.",
}


def rpc_message(exc: errors.RPCError) -> str:
    return _RPC_HINTS.get(type(exc).__name__, f"Telegram refused the request ({type(exc).__name__}).")


class ChatResolver:
    def __init__(self, client: TelegramClient, status: Callable[[str], None] | None = None) -> None:
        self._client = client
        self._status = status or (lambda _message: None)
        self._cache_warm = False

    async def resolve(self, reference: int | str) -> ChatInfo:
        """Resolve a reference, classify it and confirm the account can read its history."""
        try:
            parsed = parse_reference(str(reference))
        except InvalidReference as exc:
            raise ChatResolverError(str(exc)) from exc

        last_error: BaseException | None = None
        for _attempt in range(2):  # one retry after a dropped connection
            try:
                await ensure_connected(self._client)
                with connection_reset_guard():
                    return await self._resolve_once(parsed)
            except TRANSIENT_ERRORS as exc:
                last_error = exc
                log.info("Chat lookup hit a connection problem: %r", exc)
        raise ChatResolverError(
            "Could not reach Telegram. Check your connection (and proxy) and try again."
        ) from last_error

    async def _resolve_once(self, parsed: int | str) -> ChatInfo:
        entity = await self._lookup(parsed)
        if isinstance(entity, (ChatForbidden, ChannelForbidden)):
            raise ChatNotAccessible("This account no longer has access to that chat (removed or banned).")
        info = describe_entity(entity)
        if info is None:
            raise ChatNotFound("That reference is not a user, bot, group or channel.")

        total = await self._probe_history(entity)
        return replace(info, approx_messages=total)

    async def _lookup(self, ref: int | str):
        try:
            return await self._client.get_entity(ref)
        except ValueError as exc:
            if isinstance(ref, int) and not self._cache_warm:
                self._status("Searching your chat list for that ID...")
                await self._warm_cache()
                try:
                    return await self._client.get_entity(ref)
                except ValueError:
                    pass
            raise ChatNotFound(self._not_found_message(exc, ref)) from exc
        except errors.FloodWaitError as exc:
            raise ChatResolverError(flood_message(exc)) from exc
        except errors.RPCError as exc:
            raise ChatNotAccessible(rpc_message(exc)) from exc

    @staticmethod
    def _not_found_message(exc: ValueError, ref: int | str) -> str:
        if "not part of" in str(exc):
            return ("That invite link is for a chat this account has not joined. "
                    "Join it in Telegram first, then try again.")
        if isinstance(ref, int):
            return "No chat with that ID is visible to this account (it must appear in your chat list)."
        return "No chat found for that username or link."

    async def _warm_cache(self) -> None:
        """Load the dialog list so Telethon caches every user, group and channel in it.

        Telethon's own iter_dialogs() crashes on dialog kinds newer than the library knows
        (it raised "'DialogCommunity' object has no attribute 'peer'"), so this pages through
        the raw API and only relies on the users and chats Telethon caches from each response.
        """
        log.info("Refreshing the dialog list to fill the entity cache")
        offset_date, offset_id, offset_peer = None, 0, tl_types.InputPeerEmpty()
        try:
            for _page in range(MAX_DIALOG_PAGES):
                result = await self._client(
                    functions.messages.GetDialogsRequest(
                        offset_date=offset_date,
                        offset_id=offset_id,
                        offset_peer=offset_peer,
                        limit=100,
                        hash=0,
                    )
                )
                dialogs = getattr(result, "dialogs", None) or []
                if not dialogs or not isinstance(result, tl_types.messages.DialogsSlice):
                    break  # everything arrived in this response
                last = next(
                    (d for d in reversed(dialogs)
                     if getattr(d, "peer", None) is not None and getattr(d, "top_message", None)),
                    None,
                )
                if last is None:
                    break
                wanted = (tl_utils.get_peer_id(last.peer), last.top_message)
                message = next(
                    (m for m in getattr(result, "messages", [])
                     if getattr(m, "peer_id", None) is not None
                     and (tl_utils.get_peer_id(m.peer_id), m.id) == wanted),
                    None,
                )
                if message is None:
                    break
                if message.id == offset_id and message.date == offset_date:
                    break  # no progress: never loop forever
                offset_date, offset_id = message.date, message.id
                offset_peer = await self._client.get_input_entity(last.peer)
        except errors.FloodWaitError as exc:
            raise ChatResolverError(flood_message(exc)) from exc
        except errors.RPCError as exc:
            raise ChatResolverError(rpc_message(exc)) from exc
        except (AttributeError, TypeError, ValueError) as exc:
            log.info("Chat list loading stopped early: %r", exc)
        self._cache_warm = True

    async def _probe_history(self, entity: object) -> int | None:
        """Read the newest message: proves the account can read this chat's history."""
        try:
            newest = await self._client.get_messages(entity, limit=1)
        except errors.FloodWaitError as exc:
            raise ChatResolverError(flood_message(exc)) from exc
        except errors.RPCError as exc:
            raise ChatNotAccessible(rpc_message(exc)) from exc
        return getattr(newest, "total", None)

"""Turn whatever the user types into something Telethon can resolve.

Accepted forms:
  @name, name                    public username
  t.me/name, t.me/name/123       public link or message link
  t.me/c/<id>/<msg>              private channel message link
  t.me/+HASH, t.me/joinchat/HASH invite link (only works if already joined)
  tg://resolve?domain=name       deep links
  tg://join?invite=HASH
  123, -123, -100123...          numeric IDs (users, basic groups, channels)
  me                             Saved Messages

No Telethon imports here, so this stays easy to test.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs

_NUMERIC_ID = re.compile(r"-?\d+")
_TME_LINK = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(?P<path>.+)", re.IGNORECASE
)
_TG_LINK = re.compile(r"tg://(?P<action>[a-z]+)\?(?P<query>.+)", re.IGNORECASE)
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")

_HINT = "Enter a @username, a t.me link, or a numeric chat ID."


class InvalidReference(ValueError):
    """The text is not a recognizable chat reference."""


def parse_reference(raw: str) -> int | str:
    """Return an int (numeric ID) or a str (username or invite link) for Telethon."""
    text = raw.strip().strip("<>").strip()
    if not text:
        raise InvalidReference("Nothing was entered.")

    # Must be checked first: Telethon would treat a digit-only *string* as a phone number.
    if _NUMERIC_ID.fullmatch(text):
        return int(text)

    if text.lower() in ("me", "self"):
        return "me"

    deep = _TG_LINK.fullmatch(text)
    if deep:
        params = parse_qs(deep["query"])
        action = deep["action"].lower()
        if action == "resolve" and params.get("domain"):
            return _username(params["domain"][0])
        if action == "join" and params.get("invite"):
            return f"https://t.me/+{params['invite'][0]}"
        raise InvalidReference("That tg:// link does not point to a chat.")

    link = _TME_LINK.fullmatch(text)
    if link:
        path = re.split(r"[?#]", link["path"], maxsplit=1)[0]
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise InvalidReference("That link does not point to a chat.")
        head = parts[0]
        if head.startswith("+") and len(head) > 1:
            return f"https://t.me/{head}"
        if head.lower() == "joinchat" and len(parts) > 1:
            return f"https://t.me/joinchat/{parts[1]}"
        if head.lower() == "c" and len(parts) > 1 and parts[1].isdigit():
            return int(f"-100{parts[1]}")  # link IDs omit the -100 channel prefix
        if head.lower() == "s" and len(parts) > 1:
            head = parts[1]  # t.me/s/name is the web preview of a public channel
        return _username(head)

    return _username(text)


def _username(candidate: str) -> str:
    name = candidate.strip().lstrip("@")
    if not _USERNAME.fullmatch(name):
        raise InvalidReference(_HINT)
    return name

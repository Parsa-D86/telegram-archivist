"""Console logging: short, de-duplicated, human-readable lines.

Everything is printed through the same rich Console as the progress bars and
spinners, so a log line can never tear or duplicate a live display (writing to
stderr behind rich's back is what made the scan line repeat). Full detail still
goes to the log file.
"""
from __future__ import annotations

import logging
import re
import time
from typing import NamedTuple

REPEAT_WINDOW = 15.0  # seconds during which an identical line is not shown again

_SLEEP_RE = re.compile(r"Sleeping for (\d+)s")
_DROP_MARKERS = (
    "Connection closed while",
    "Server closed the connection",
    "Closing current connection to begin reconnect",
    "at connecting failed",
)


class Line(NamedTuple):
    text: str
    style: str
    kind: str = "info"  # "drop", "restored" or "info"


def friendly(record: logging.LogRecord) -> Line | None:
    """Translate a log record into a console line, or None to keep it in the file only."""
    message = record.getMessage()
    if record.name.startswith("telethon"):
        if "flood wait" in message:
            found = _SLEEP_RE.search(message)
            wait = f" {found.group(1)}s" if found else ""
            return Line(f"Telegram rate limit, waiting{wait}...", "yellow")
        if any(marker in message for marker in _DROP_MARKERS):
            return Line("Connection to Telegram dropped, reconnecting...", "yellow", "drop")
        if message.startswith("Connection to") and message.endswith("complete!"):
            return Line("Reconnected to Telegram.", "green", "restored")
        if "internal issues" in message:
            return Line("Telegram is busy, retrying...", "yellow")
        return None  # other library chatter stays in the log file
    if record.levelno >= logging.ERROR:
        return Line(message, "red")
    if record.levelno >= logging.WARNING:
        return Line(message, "yellow")
    return None


class ConsoleHandler(logging.Handler):
    def __init__(self, console) -> None:
        super().__init__(level=logging.INFO)
        self._console = console
        self._recent: dict[str, float] = {}
        self._dropped = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = friendly(record)
            if line is None:
                return
            if line.kind == "restored":
                if not self._dropped:  # ordinary connects (new data center, startup) are not news
                    return
                self._dropped = False
            elif line.kind == "drop":
                self._dropped = True

            now = time.monotonic()
            if now - self._recent.get(line.text, float("-inf")) < REPEAT_WINDOW:
                return
            self._recent[line.text] = now

            from rich.text import Text

            mark = "✓" if line.kind == "restored" else "!"
            self._console.print(Text(f"  {mark} {line.text}", style=line.style))
        except Exception:
            self.handleError(record)

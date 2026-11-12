"""Timestamped logging: full detail in Logs/, short friendly lines on the console."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from utils.console_log import ConsoleHandler

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_configured = False


def setup_logging(log_dir: Path, level: str = "INFO", console=None) -> Path:
    """Configure root logging once and return the log file path.

    Pass the rich Console used for the UI so log lines are printed through it
    (never straight to stderr, which would tear progress bars and spinners).
    """
    global _configured
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "archivist.log"
    if _configured:
        return log_file

    root = logging.getLogger()
    root.setLevel(getattr(logging, level))

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)

    if console is not None:
        root.addHandler(ConsoleHandler(console))
    else:
        fallback = logging.StreamHandler(sys.stderr)
        fallback.setLevel(logging.WARNING)
        fallback.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(fallback)

    # Telethon's DEBUG output includes raw requests (phone numbers, login codes),
    # so never let it go below INFO even when the app runs at DEBUG.
    logging.getLogger("telethon").setLevel(max(root.level, logging.INFO))
    # Per-download and per-update chatter would drown the file log.
    logging.getLogger("telethon.client.downloads").setLevel(logging.WARNING)
    logging.getLogger("telethon.client.updates").setLevel(logging.WARNING)

    _configured = True
    return log_file

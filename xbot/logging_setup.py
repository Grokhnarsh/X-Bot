"""Logging: lesbar auf der Konsole, rotierend in der Datei."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LoggingSettings

CONSOLE_FORMAT = "%(asctime)s  %(levelname)-7s  %(message)s"
FILE_FORMAT = "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(settings: LoggingSettings, *, verbose: bool = False, quiet: bool = False) -> None:
    level = logging.DEBUG if verbose else getattr(logging, settings.level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if not quiet:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(logging.Formatter(CONSOLE_FORMAT, TIME_FORMAT))
        root.addHandler(console)

    if settings.file:
        path = Path(settings.file)
        try:
            if path.parent and str(path.parent) not in ("", "."):
                path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path,
                maxBytes=settings.max_bytes,
                backupCount=settings.backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.DEBUG if verbose else level)
            file_handler.setFormatter(logging.Formatter(FILE_FORMAT, TIME_FORMAT))
            root.addHandler(file_handler)
        except OSError as exc:  # z. B. schreibgeschuetztes Verzeichnis
            logging.getLogger(__name__).warning("Logdatei '%s' nicht nutzbar: %s", path, exc)

    # Die Bibliotheken sollen nicht ins Protokoll rauschen.
    for noisy in ("tweepy", "urllib3", "httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

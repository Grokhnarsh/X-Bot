"""Kleine Helfer, die Seiten und Schnittstelle gemeinsam nutzen."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from flask import current_app

from ..config import Config
from .runner import BotRunner
from .settings_io import ConfigFile, EnvFile


def runner() -> BotRunner:
    return current_app.extensions["xbot_runner"]


def config_file() -> ConfigFile:
    return ConfigFile(current_app.config["XBOT_CONFIG_PATH"])


def env_file() -> EnvFile:
    return EnvFile(current_app.config["XBOT_ENV_PATH"])


def current_config() -> Config:
    return runner().config


def tail_file(path: str | Path, lines: int = 200, *, max_bytes: int = 512_000) -> list[str]:
    """Die letzten ``lines`` Zeilen einer Datei, ohne sie ganz zu lesen.

    Protokolldateien werden mehrere Megabyte gross; hier wird nur das Ende
    gelesen, das für die Anzeige gebraucht wird.
    """
    path = Path(path)
    if not path.exists() or not path.is_file():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()          # angeschnittene Zeile verwerfen
            data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-max(1, lines):]


def format_local(moment: datetime | str | None, config: Config, pattern: str = "%d.%m. %H:%M") -> str:
    """Zeitpunkt in der konfigurierten Zeitzone darstellen."""
    if moment is None:
        return "-"
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment)
        except ValueError:
            return str(moment)
    return moment.astimezone(config.bot.tzinfo).strftime(pattern)


def humanize_delta(target: datetime | str | None, *, now: datetime | None = None) -> str:
    """"in 3 Std. 12 Min." - für die Anzeige des nächsten Laufs."""
    from ..state import utcnow

    if target is None:
        return "-"
    if isinstance(target, str):
        try:
            target = datetime.fromisoformat(target)
        except ValueError:
            return "-"
    now = now or utcnow()
    seconds = int((target - now).total_seconds())
    if seconds <= 0:
        return "jetzt"
    if seconds < 60:
        return f"in {seconds} Sek."
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"in {minutes} Min."
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"in {hours} Std. {minutes} Min." if minutes else f"in {hours} Std."
    days, hours = divmod(hours, 24)
    return f"in {days} Tg. {hours} Std." if hours else f"in {days} Tg."


def is_local_request(remote_addr: str | None) -> bool:
    return remote_addr in ("127.0.0.1", "::1", "localhost")


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "ja")

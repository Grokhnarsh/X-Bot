"""Datenmodelle fuer Discord-Nachrichten.

Die REST-API liefert je nach Berechtigung unterschiedlich vollstaendige
Objekte - fehlt etwa das Message-Content-Intent, ist ``content`` leer.
``DiscordMessage.from_api`` normalisiert das auf eine feste Form, damit
Filter und Regeln nicht an jeder Stelle auf fehlende Felder pruefen muessen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

#: Nachrichtentypen, die der Bot ueberhaupt betrachtet.
#: 0 = normale Nachricht, 19 = Antwort auf eine andere Nachricht.
TYPE_DEFAULT = 0
TYPE_REPLY = 19

#: Discord begrenzt den Inhalt einer Nachricht.
MESSAGE_LIMIT = 2000


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class DiscordAuthor:
    id: str = ""
    username: str = ""
    display_name: str = ""
    is_bot: bool = False

    @property
    def label(self) -> str:
        """Was in Protokoll und Oberflaeche steht."""
        return self.display_name or self.username or self.id or "?"

    @classmethod
    def from_api(cls, data: Any) -> "DiscordAuthor":
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            id=str(data.get("id", "")),
            username=str(data.get("username", "")),
            # global_name ist der neue Anzeigename; aeltere Konten haben nur username.
            display_name=str(data.get("global_name") or data.get("username") or ""),
            is_bot=bool(data.get("bot", False)),
        )


@dataclass(frozen=True)
class DiscordMessage:
    id: str
    channel_id: str = ""
    guild_id: str = ""
    content: str = ""
    author: DiscordAuthor = field(default_factory=DiscordAuthor)
    created_at: datetime | None = None
    mentions: tuple[str, ...] = ()
    mention_everyone: bool = False
    urls: tuple[str, ...] = ()
    is_reply: bool = False
    attachments: int = 0
    reaction_count: int = 0
    #: Emoji, mit denen der Bot selbst bereits reagiert hat.
    own_reactions: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        guild = self.guild_id or "@me"
        return f"https://discord.com/channels/{guild}/{self.channel_id}/{self.id}"

    @property
    def preview(self) -> str:
        """Einzeilige Kurzfassung fuer Logausgaben."""
        flat = " ".join(self.content.split())
        return flat if len(flat) <= 90 else flat[:87] + "..."

    def has_reacted(self, emoji: str) -> bool:
        return emoji in self.own_reactions

    @classmethod
    def from_api(cls, data: Any, *, guild_id: str = "") -> "DiscordMessage":
        if not isinstance(data, Mapping):
            return cls(id="")

        content = str(data.get("content", "") or "")
        mentions = tuple(
            str(user.get("id", ""))
            for user in (data.get("mentions") or [])
            if isinstance(user, Mapping) and user.get("id")
        )

        eigene: list[str] = []
        gesamt = 0
        for reaktion in data.get("reactions") or []:
            if not isinstance(reaktion, Mapping):
                continue
            gesamt += int(reaktion.get("count", 0) or 0)
            emoji = reaktion.get("emoji") or {}
            name = str(emoji.get("name", "")) if isinstance(emoji, Mapping) else ""
            if reaktion.get("me") and name:
                eigene.append(name)

        return cls(
            id=str(data.get("id", "")),
            channel_id=str(data.get("channel_id", "")),
            guild_id=str(data.get("guild_id", "") or guild_id),
            content=content,
            author=DiscordAuthor.from_api(data.get("author")),
            created_at=_parse_timestamp(data.get("timestamp")),
            mentions=mentions,
            mention_everyone=bool(data.get("mention_everyone", False)),
            urls=tuple(URL_PATTERN.findall(content)),
            is_reply=int(data.get("type", TYPE_DEFAULT) or 0) == TYPE_REPLY
            or bool(data.get("message_reference")),
            attachments=len(data.get("attachments") or []),
            reaction_count=gesamt,
            own_reactions=tuple(eigene),
        )

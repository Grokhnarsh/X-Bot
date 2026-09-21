"""Eigene Beitraege in Discord-Kanaele schreiben.

Der Ablauf gleicht dem auf X. Ein Unterschied faellt ins Gewicht: auf X gibt
es genau eine Zeitleiste, in Discord mehrere Kanaele. Der Bot geht sie
reihum durch und merkt sich den Stand in der Datenbank - sonst landet alles
im erstbesten Kanal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..config import Config
from ..content import ContentGenerator
from ..errors import ContentError
from ..models import ActionResult
from ..quota import QuotaGuard
from ..state import PLATFORM_DISCORD, Store, utcnow

logger = logging.getLogger(__name__)

#: Wo der naechste Kanal der Reihe nach vermerkt wird.
CHANNEL_CURSOR_KEY = "discord.posting.next_channel"


@dataclass
class DiscordPostReport:
    posted: bool = False
    text: str = ""
    source: str = ""
    channel_id: str = ""
    reason: str = ""
    dry_run: bool = False
    #: True nur bei echten Stoerungen (Texterstellung, Discord-API). Ein
    #: regulaeres Ueberspringen - Zeitfenster zu, Limit erreicht - ist kein Fehler.
    failed: bool = False
    result: ActionResult | None = field(default=None, repr=False)

    def describe(self) -> str:
        if self.posted:
            marker = "[PROBELAUF] " if self.dry_run else ""
            return (
                f"{marker}Discord-Beitrag in Kanal {self.channel_id} "
                f"({self.source}, {len(self.text)} Zeichen)"
            )
        return f"Kein Discord-Beitrag: {self.reason}"


class DiscordPoster:
    def __init__(
        self,
        config: Config,
        client,
        store: Store,
        quota: QuotaGuard,
        generator: ContentGenerator,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store
        self.quota = quota
        self.generator = generator

    # -- Zeitfenster --------------------------------------------------------
    def window_open(self, now: datetime | None = None) -> tuple[bool, str]:
        """Liegt der Zeitpunkt im konfigurierten Sendefenster?"""
        now = now or utcnow()
        local = now.astimezone(self.config.bot.tzinfo)
        posting = self.config.discord.posting

        if local.weekday() not in posting.active_weekdays:
            return False, f"{local:%A} ist kein aktiver Wochentag"

        start, end = posting.active_hours
        if not (start <= local.hour < end):
            return False, f"{local:%H:%M} liegt ausserhalb von {start:02d}:00-{end:02d}:00"

        return True, ""

    # -- Kanalwahl ----------------------------------------------------------
    def next_channel(self) -> str:
        """Der naechste Kanal der Reihe nach.

        Der Zeiger wandert erst nach einem erfolgreichen Beitrag weiter
        (siehe ``advance_channel``) - scheitert das Senden, ist der Kanal
        beim naechsten Versuch wieder an der Reihe.
        """
        channels = self.config.discord.posting.channels
        if not channels:
            return ""
        gespeichert = self.store.get_state(CHANNEL_CURSOR_KEY, "")
        if gespeichert in channels:
            return gespeichert
        return channels[0]

    def advance_channel(self, aktuell: str) -> None:
        channels = self.config.discord.posting.channels
        if len(channels) < 2:
            return
        try:
            index = channels.index(aktuell)
        except ValueError:
            index = -1
        self.store.set_state(CHANNEL_CURSOR_KEY, channels[(index + 1) % len(channels)])

    # -- Hauptablauf --------------------------------------------------------
    def run(
        self,
        *,
        topic: str | None = None,
        channel: str | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> DiscordPostReport:
        now = now or utcnow()
        discord = self.config.discord

        if not discord.enabled and not force:
            return DiscordPostReport(reason="discord.enabled ist false")

        if not discord.posting.enabled and not force:
            return DiscordPostReport(reason="discord.posting.enabled ist false")

        ziel = str(channel or self.next_channel()).strip()
        if not ziel:
            return DiscordPostReport(
                reason="kein Zielkanal - trage unter discord.posting.channels eine Kanal-ID ein"
            )

        if not force:
            open_now, why = self.window_open(now)
            if not open_now:
                return DiscordPostReport(reason=why, channel_id=ziel)

        # Limits gelten immer - auch bei einem erzwungenen Aufruf.
        decision = self.quota.check("post", now)
        if not decision:
            return DiscordPostReport(reason=decision.reason, channel_id=ziel)

        try:
            generated = self.generator.generate_discord_post(topic=topic)
        except ContentError as exc:
            logger.warning("Discord-Texterstellung fehlgeschlagen: %s", exc)
            return DiscordPostReport(
                reason=f"Texterstellung fehlgeschlagen: {exc}", channel_id=ziel, failed=True
            )

        result = self.client.post(generated.text, ziel)
        if not result.ok:
            return DiscordPostReport(
                reason=result.error or "unbekannter Fehler",
                text=generated.text,
                channel_id=ziel,
                result=result,
                failed=True,
            )

        self.store.record_action(
            "post",
            platform=PLATFORM_DISCORD,
            target_id=ziel,
            text=generated.text,
            dry_run=result.dry_run,
            created_at=now,
        )
        self.advance_channel(ziel)
        logger.info(
            "%sDiscord-Beitrag in %s (%s, %d Zeichen): %s",
            "[PROBELAUF] " if result.dry_run else "",
            ziel,
            generated.source,
            len(generated.text),
            generated.text.replace("\n", " / "),
        )
        return DiscordPostReport(
            posted=True,
            text=generated.text,
            source=generated.source,
            channel_id=ziel,
            dry_run=result.dry_run,
            result=result,
        )

"""Der Bot als Ganzes - haelt die Einzelteile zusammen.

Die Kommandozeile spricht ausschliesslich mit dieser Klasse. Dadurch bleibt
die CLI duenn und die Ablaeufe sind ohne Terminal testbar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterator

from .actions import EngagementEngine, EngagementReport, PostReport, Poster
from .client import XClient
from .config import Config
from .content import ContentGenerator, GeneratedText
from .content.templates import TemplateLibrary
from .discord import (
    DiscordClient,
    DiscordEngagementEngine,
    DiscordEngagementReport,
    DiscordPoster,
    DiscordPostReport,
)
from .errors import ContentError, CredentialsError, XBotError
from .models import Author
from .quota import QuotaGuard
from .scheduler import Job, Scheduler
from .state import PLATFORM_DISCORD, PLATFORM_X, Store, utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Check:
    """Ein Pruefpunkt aus ``xbot doctor``."""

    name: str
    ok: bool
    detail: str = ""
    warning: bool = False

    @property
    def symbol(self) -> str:
        if self.ok:
            return "!" if self.warning else "+"
        return "x"


class Bot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = Store(config.storage.database)
        self.client = XClient(config.credentials, dry_run=config.bot.dry_run)
        self.quota = QuotaGuard(
            config.engagement.limits,
            self.store,
            config.bot.tzinfo,
            dry_run=config.bot.dry_run,
        )
        self.generator = ContentGenerator(config, self.store)
        self.poster = Poster(config, self.client, self.store, self.quota, self.generator)
        self.engine = EngagementEngine(config, self.client, self.store, self.quota, self.generator)

        # Discord bekommt einen eigenen Waechter: Reaktionen dort duerfen
        # Likes auf X nicht ausbremsen und umgekehrt.
        self.discord_client = DiscordClient(
            config.credentials.discord_bot_token, dry_run=config.bot.dry_run
        )
        self.discord_quota = QuotaGuard(
            config.discord.engagement.limits,
            self.store,
            config.bot.tzinfo,
            dry_run=config.bot.dry_run,
            platform=PLATFORM_DISCORD,
        )
        self.discord_poster = DiscordPoster(
            config, self.discord_client, self.store, self.discord_quota, self.generator
        )
        self.discord_engine = DiscordEngagementEngine(
            config, self.discord_client, self.store, self.discord_quota, self.generator
        )

    # -- Lebenszyklus -------------------------------------------------------
    def close(self) -> None:
        self.discord_client.close()
        self.store.close()

    def __enter__(self) -> "Bot":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def dry_run(self) -> bool:
        return self.config.bot.dry_run

    # -- Einzelaktionen -----------------------------------------------------
    def post_once(self, *, topic: str | None = None, force: bool = False) -> PostReport:
        return self.poster.run(topic=topic, force=force)

    def engage_once(self) -> EngagementReport:
        return self.engine.run_cycle()

    def discord_post_once(
        self, *, topic: str | None = None, channel: str | None = None, force: bool = False
    ) -> DiscordPostReport:
        return self.discord_poster.run(topic=topic, channel=channel, force=force)

    def discord_engage_once(self) -> DiscordEngagementReport:
        return self.discord_engine.run_cycle()

    @property
    def discord_active(self) -> bool:
        """Ist Discord eingeschaltet und mit einem Token versehen?"""
        return self.config.discord.enabled and bool(self.config.credentials.discord_bot_token)

    def preview(self, *, count: int = 3, topic: str | None = None) -> Iterator[GeneratedText]:
        """Erzeugt Texte, ohne etwas zu senden oder zu speichern."""
        for _ in range(max(1, count)):
            yield self.generator.generate_post(topic=topic)

    # -- Dauerbetrieb -------------------------------------------------------
    def build_jobs(self) -> list[Job]:
        posting = self.config.posting
        engagement = self.config.engagement
        discord = self.config.discord
        jobs = [
            Job(
                name="Beitrag veroeffentlichen",
                interval_minutes=posting.interval_minutes,
                jitter_minutes=posting.jitter_minutes,
                enabled=posting.enabled,
                run=self._posting_job,
            ),
            Job(
                name="Hashtags beobachten",
                interval_minutes=engagement.interval_minutes,
                jitter_minutes=engagement.jitter_minutes,
                enabled=engagement.enabled,
                run=self._engagement_job,
            ),
        ]
        if discord.enabled:
            jobs.extend(
                [
                    Job(
                        name="Discord-Beitrag veroeffentlichen",
                        interval_minutes=discord.posting.interval_minutes,
                        jitter_minutes=discord.posting.jitter_minutes,
                        enabled=discord.posting.enabled,
                        run=self._discord_posting_job,
                    ),
                    Job(
                        name="Discord-Kanaele beobachten",
                        interval_minutes=discord.engagement.interval_minutes,
                        jitter_minutes=discord.engagement.jitter_minutes,
                        enabled=discord.engagement.enabled,
                        run=self._discord_engagement_job,
                    ),
                ]
            )
        return jobs

    def _posting_job(self) -> None:
        report = self.post_once()
        if report.posted:
            logger.info(report.describe())
        else:
            logger.info("Kein Beitrag: %s", report.reason)

    def _engagement_job(self) -> None:
        report = self.engage_once()
        logger.info(report.describe())
        for reason, count in report.top_skips():
            logger.debug("  uebersprungen %dx: %s", count, reason)
        for error in report.errors[:3]:
            logger.warning("  Fehler: %s", error)

    def _discord_posting_job(self) -> None:
        report = self.discord_post_once()
        if report.posted:
            logger.info(report.describe())
        else:
            logger.info("Kein Discord-Beitrag: %s", report.reason)

    def _discord_engagement_job(self) -> None:
        report = self.discord_engage_once()
        logger.info("Discord: %s", report.describe())
        for reason, count in report.top_skips():
            logger.debug("  uebersprungen %dx: %s", count, reason)
        for error in report.errors[:3]:
            logger.warning("  Fehler: %s", error)

    def run(self, *, initial_run: bool = True, max_cycles: int | None = None) -> None:
        mode = "PROBELAUF - es wird nichts gesendet" if self.dry_run else "ECHTBETRIEB"
        logger.info("X-Bot startet. Modus: %s", mode)
        if not self.dry_run:
            try:
                me = self.client.verify()
                logger.info("Angemeldet als @%s (%s Follower)", me.username, me.followers)
            except CredentialsError as exc:
                logger.error("Anmeldung fehlgeschlagen: %s", exc)
                raise
            if self.config.discord.enabled:
                try:
                    bot_konto = self.discord_client.verify()
                    logger.info("Discord: angemeldet als %s", bot_konto.label)
                except CredentialsError as exc:
                    logger.error("Discord-Anmeldung fehlgeschlagen: %s", exc)
                    raise
        Scheduler(self.build_jobs()).run(initial_run=initial_run, max_cycles=max_cycles)

    # -- Auswertung ---------------------------------------------------------
    def stats(self, *, days: int = 7) -> dict[str, object]:
        since = utcnow() - timedelta(days=max(1, days))
        return {
            "dry_run": self.dry_run,
            "days": days,
            "summary": self.store.summary(since, platform=PLATFORM_X),
            "quota": self.quota.snapshot(),
            "discord_enabled": self.config.discord.enabled,
            "discord_summary": self.store.summary(since, platform=PLATFORM_DISCORD),
            "discord_quota": self.discord_quota.snapshot(),
            "recent": self.store.recent_actions(10),
            "database": str(self.store.path),
        }

    # -- Selbsttest ---------------------------------------------------------
    def doctor(self) -> list[Check]:
        checks: list[Check] = []
        cfg = self.config
        creds = cfg.credentials

        source = cfg.source_path or "(aus dem Speicher)"
        checks.append(Check("Konfiguration", True, f"{source}, Zeitzone {cfg.bot.timezone}"))

        checks.append(
            Check(
                "Betriebsmodus",
                True,
                "PROBELAUF - es wird nichts gesendet" if self.dry_run else "ECHTBETRIEB - es wird wirklich gesendet",
                warning=not self.dry_run,
            )
        )

        # -- Datenbank ------------------------------------------------------
        try:
            self.store.connect()
            checks.append(Check("Datenbank", True, str(self.store.path)))
        except Exception as exc:
            checks.append(Check("Datenbank", False, f"{self.store.path} nicht nutzbar: {exc}"))

        # -- X-Zugangsdaten -------------------------------------------------
        if creds.has_write_access:
            checks.append(Check("X-Zugangsdaten", True, "alle vier OAuth-Werte vorhanden"))
            try:
                me: Author = self.client.verify()
                checks.append(
                    Check("X-Verbindung", True, f"angemeldet als @{me.username} ({me.followers} Follower)")
                )
            except CredentialsError as exc:
                checks.append(Check("X-Verbindung", False, str(exc)))
        else:
            missing = ", ".join(creds.missing_write_fields())
            checks.append(
                Check("X-Zugangsdaten", False, f"fehlt in der .env: {missing}")
            )

        checks.append(
            Check(
                "Suchzugriff",
                creds.has_search_access,
                "Bearer Token gesetzt"
                if creds.bearer_token
                else (
                    "kein X_BEARER_TOKEN - die Suche laeuft ueber den Nutzerkontext"
                    if creds.has_write_access
                    else "weder Bearer Token noch OAuth-Zugangsdaten"
                ),
                warning=creds.has_search_access and not creds.bearer_token,
            )
        )

        # -- Texterstellung -------------------------------------------------
        if cfg.content.provider == "template":
            checks.append(Check("Texterstellung", True, "Vorlagen (KI abgeschaltet)"))
        elif creds.has_ai:
            checks.append(Check("Texterstellung", True, f"Claude ({cfg.content.model}, Tiefe {cfg.content.effort})"))
        elif cfg.content.provider == "ai":
            checks.append(
                Check("Texterstellung", False, "content.provider ist 'ai', aber ANTHROPIC_API_KEY fehlt")
            )
        else:
            checks.append(
                Check("Texterstellung", True, "kein ANTHROPIC_API_KEY - es werden Vorlagen genutzt", warning=True)
            )

        try:
            library = TemplateLibrary.load(cfg.content.templates_file)
            checks.append(
                Check("Vorlagen", True, f"{len(library.posts)} Beitraege, {len(library.replies)} Antworten")
            )
        except ContentError as exc:
            needs_templates = cfg.content.provider != "ai"
            checks.append(Check("Vorlagen", not needs_templates, str(exc), warning=not needs_templates))

        # -- Regeln ---------------------------------------------------------
        if cfg.rules:
            actions = sorted({action for rule in cfg.rules for action in rule.actions})
            checks.append(
                Check(
                    "Regeln",
                    True,
                    f"{len(cfg.rules)} Regeln, {len(cfg.monitored_hashtags)} Hashtags, Aktionen: {', '.join(actions)}",
                )
            )
        else:
            checks.append(Check("Regeln", not cfg.engagement.enabled, "keine Hashtag-Regel definiert"))

        # -- Grenzwerte -----------------------------------------------------
        limits = cfg.engagement.limits
        checks.append(
            Check(
                "Limits",
                True,
                f"pro Tag: {limits.post_per_day} Beitraege, {limits.like_per_day} Likes, "
                f"{limits.repost_per_day} Reposts, {limits.reply_per_day} Antworten; "
                f"Mindestabstand {limits.min_seconds_between_actions}s",
            )
        )

        checks.extend(self._discord_checks())
        return checks

    def _discord_checks(self) -> list[Check]:
        """Pruefpunkte der Discord-Erweiterung.

        Ist Discord abgeschaltet, bleibt es bei einer Zeile - der Selbsttest
        soll nicht an einer Erweiterung scheitern, die niemand nutzt.
        """
        cfg = self.config
        discord = cfg.discord
        if not discord.enabled:
            return [Check("Discord", True, "abgeschaltet (discord.enabled: false)")]

        checks: list[Check] = []
        token = cfg.credentials.discord_bot_token
        if not token:
            checks.append(
                Check(
                    "Discord-Zugangsdaten",
                    False,
                    "discord.enabled ist true, aber DISCORD_BOT_TOKEN fehlt in der .env",
                )
            )
        else:
            checks.append(Check("Discord-Zugangsdaten", True, "DISCORD_BOT_TOKEN vorhanden"))
            try:
                konto = self.discord_client.verify()
                checks.append(Check("Discord-Verbindung", True, f"angemeldet als {konto.label}"))
            except (CredentialsError, XBotError) as exc:
                checks.append(Check("Discord-Verbindung", False, str(exc)))

        if discord.rules:
            actions = sorted({a for rule in discord.rules for a in rule.actions})
            checks.append(
                Check(
                    "Discord-Regeln",
                    True,
                    f"{len(discord.rules)} Regeln, {len(discord.watched_keywords)} Schluesselwoerter, "
                    f"Aktionen: {', '.join(actions)}",
                )
            )
        else:
            checks.append(
                Check("Discord-Regeln", not discord.engagement.enabled, "keine Discord-Regel definiert")
            )

        beobachtet = discord.all_watch_channels
        checks.append(
            Check(
                "Discord-Kanaele",
                bool(beobachtet) or not discord.engagement.enabled,
                f"{len(beobachtet)} beobachtet, {len(discord.posting.channels)} zum Posten"
                if beobachtet or discord.posting.channels
                else "keine Kanal-ID eingetragen",
            )
        )

        dl = discord.engagement.limits
        checks.append(
            Check(
                "Discord-Limits",
                True,
                f"pro Tag: {dl.post_per_day} Beitraege, {dl.react_per_day} Reaktionen, "
                f"{dl.reply_per_day} Antworten; Mindestabstand {dl.min_seconds_between_actions}s",
            )
        )
        return checks

    def check_ai(self) -> Check:
        """Erzeugt testweise einen Text ueber die Claude-API."""
        if not self.generator.ai_configured:
            return Check("KI-Test", False, "keine KI konfiguriert")
        try:
            generated = self.generator.generate_post()
        except XBotError as exc:
            return Check("KI-Test", False, str(exc))
        if generated.source != "ai":
            return Check("KI-Test", False, "es kam ein Vorlagentext zurueck - die KI war nicht erreichbar")
        return Check("KI-Test", True, generated.text)

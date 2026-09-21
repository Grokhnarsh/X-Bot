"""Kanal-Monitoring: lesen, filtern, reagieren.

Ablauf eines Durchlaufs:

1. Jeder beobachtete Kanal wird abgerufen - ab der Nachricht, bei der der
   letzte Durchlauf aufgehoert hat.
2. Zu alte Nachrichten und bereits bewertete fallen raus.
3. Jede verbleibende Nachricht durchlaeuft die Sicherheitsfilter und wird
   einer Regel zugeordnet.
4. Die Kandidaten werden nach Regelgewicht und vorhandener Resonanz sortiert -
   das knappe Aktionsbudget geht an die besten zuerst.
5. Pro Aktion greifen Dedupe und Limits erneut, unmittelbar vor dem Senden.

Zum Lesezeichen: es wandert am Ende eines Durchlaufs auf die neueste
abgerufene Nachricht, auch wenn das Budget nicht fuer alle gereicht hat. Ein
Bot, der einen Rueckstand abarbeitet, reagiert sonst irgendwann auf
Gespraeche von gestern. Der naechste Durchlauf nimmt, was neu ist.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Sequence

from ..config import Config, DiscordRule
from ..content import ContentGenerator
from ..errors import ContentError, CredentialsError, XBotError
from ..quota import QuotaGuard
from ..state import PLATFORM_DISCORD, Store, utcnow
from .filters import DiscordMessageFilter, first_matching_rule
from .models import DiscordMessage

logger = logging.getLogger(__name__)

#: So lange wird innerhalb eines Durchlaufs auf den Mindestabstand gewartet.
#: Laengere Wartezeiten werden auf den naechsten Durchlauf verschoben.
MAX_INLINE_WAIT_SECONDS = 120

#: Feste Reihenfolge - erst die guenstigste, unaufdringlichste Aktion.
ACTION_ORDER = ("react", "reply")

#: Praefix der Lesezeichen je Kanal in der kv-Tabelle.
CURSOR_PREFIX = "discord.cursor."


def cursor_key(channel_id: str) -> str:
    return f"{CURSOR_PREFIX}{channel_id}"


@dataclass
class DiscordEngagementReport:
    """Was ein Durchlauf bewirkt hat."""

    channels: int = 0
    found: int = 0
    fresh: int = 0
    candidates: int = 0
    actions: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)

    @property
    def total_actions(self) -> int:
        return sum(self.actions.values())

    def describe(self) -> str:
        if self.errors and not self.found:
            return f"Durchlauf fehlgeschlagen: {self.errors[0]}"
        acted = (
            ", ".join(f"{count}x {name}" for name, count in sorted(self.actions.items()))
            or "keine Aktion"
        )
        return (
            f"{self.channels} Kanal/Kanaele, {self.found} Nachrichten, "
            f"{self.fresh} neu, {self.candidates} Kandidaten -> {acted}"
        )

    def top_skips(self, limit: int = 5) -> list[tuple[str, int]]:
        return self.skipped.most_common(limit)


class DiscordEngagementEngine:
    def __init__(
        self,
        config: Config,
        client,
        store: Store,
        quota: QuotaGuard,
        generator: ContentGenerator,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store
        self.quota = quota
        self.generator = generator
        self.sleeper = sleeper
        self._own_user_id: str | None = None

    # -- Eigenes Konto ------------------------------------------------------
    def own_user_id(self) -> str:
        """Die eigene Konto-ID, um eigene Nachrichten auszuschliessen."""
        if self._own_user_id is None:
            try:
                self._own_user_id = self.client.verify().id
            except (CredentialsError, XBotError) as exc:
                logger.debug("Eigenes Discord-Konto nicht ermittelbar: %s", exc)
                self._own_user_id = ""
        return self._own_user_id

    # -- Hauptablauf --------------------------------------------------------
    def run_cycle(self, now: datetime | None = None) -> DiscordEngagementReport:
        now = now or utcnow()
        report = DiscordEngagementReport()
        discord = self.config.discord

        if not discord.enabled:
            report.errors.append("discord.enabled ist false")
            return report
        if not discord.engagement.enabled:
            report.errors.append("discord.engagement.enabled ist false")
            return report
        if not discord.rules:
            report.errors.append("keine Discord-Regeln konfiguriert")
            return report

        messages = self._collect(report, now)
        if not messages:
            return report

        candidates = self._select(messages, report)
        report.candidates = len(candidates)
        if not candidates:
            return report

        self._act(candidates, report)
        return report

    # -- Schritt 1: lesen ---------------------------------------------------
    def _collect(self, report: DiscordEngagementReport, now: datetime) -> list[DiscordMessage]:
        discord = self.config.discord
        channels = discord.all_watch_channels
        report.channels = len(channels)
        if not channels:
            report.errors.append("kein beobachteter Kanal konfiguriert")
            return []

        aelteste = now - timedelta(minutes=discord.engagement.lookback_minutes)
        gesammelt: dict[str, DiscordMessage] = {}

        for channel_id in channels:
            lesezeichen = self.store.get_state(cursor_key(channel_id)) or None
            try:
                nachrichten = self.client.fetch_messages(
                    channel_id,
                    limit=discord.engagement.max_messages_per_channel,
                    after=lesezeichen,
                )
            except XBotError as exc:
                logger.error("Kanal %s nicht lesbar: %s", channel_id, exc)
                report.errors.append(str(exc))
                continue

            if not nachrichten:
                continue

            report.found += len(nachrichten)
            # Lesezeichen auf die neueste abgerufene Nachricht setzen.
            self.store.set_state(cursor_key(channel_id), nachrichten[-1].id)

            for nachricht in nachrichten:
                if nachricht.created_at and nachricht.created_at < aelteste:
                    report.skipped["aelter als das Zeitfenster"] += 1
                    continue
                gesammelt.setdefault(nachricht.id, nachricht)

        if not gesammelt:
            return []

        # Bereits bewertete Nachrichten ueberspringen - eine Abfrage statt N.
        bekannt = self.store.seen_ids(list(gesammelt), platform=PLATFORM_DISCORD)
        frisch = [n for item_id, n in gesammelt.items() if item_id not in bekannt]
        report.fresh = len(frisch)
        if bekannt:
            report.skipped["bereits bewertet"] += len(bekannt)
        return frisch

    # -- Schritt 2: filtern und zuordnen ------------------------------------
    def _select(
        self, messages: Sequence[DiscordMessage], report: DiscordEngagementReport
    ) -> list[tuple[DiscordMessage, DiscordRule]]:
        message_filter = DiscordMessageFilter(
            self.config.discord.filters, own_user_id=self.own_user_id()
        )
        candidates: list[tuple[DiscordMessage, DiscordRule]] = []

        for message in messages:
            verdict = message_filter.check(message)
            if not verdict:
                report.skipped[verdict.reason] += 1
                self.store.mark_seen(
                    message.id,
                    platform=PLATFORM_DISCORD,
                    author=message.author.label,
                    decision=f"gefiltert: {verdict.reason}",
                )
                continue

            rule, why = first_matching_rule(self.config.discord.rules, message)
            if rule is None:
                report.skipped[why] += 1
                self.store.mark_seen(
                    message.id,
                    platform=PLATFORM_DISCORD,
                    author=message.author.label,
                    decision=f"keine Regel: {why}",
                )
                continue

            candidates.append((message, rule))

        # Bestes zuerst: hoeheres Regelgewicht, dann mehr Resonanz.
        candidates.sort(key=lambda pair: (pair[1].weight, pair[0].reaction_count), reverse=True)
        return candidates

    # -- Schritt 3: handeln -------------------------------------------------
    def _act(
        self,
        candidates: Sequence[tuple[DiscordMessage, DiscordRule]],
        report: DiscordEngagementReport,
    ) -> None:
        budget = self.config.discord.engagement.max_actions_per_cycle
        if budget <= 0:
            report.skipped["Aktionsbudget ist 0"] += len(candidates)
            return

        # Hoechstens eine Nachricht je Verfasser und Durchlauf - sonst haengt
        # der Bot an jeder Zeile eines Gespraechs.
        bearbeitete_autoren: set[str] = set()

        for message, rule in candidates:
            if report.total_actions >= budget:
                report.skipped["Aktionsbudget des Durchlaufs erschoepft"] += 1
                continue

            autor = (message.author.id or message.author.username or "").lower()
            if autor and autor in bearbeitete_autoren:
                report.skipped["schon eine Aktion fuer diesen Verfasser in diesem Durchlauf"] += 1
                continue

            erledigt: list[str] = []
            for action in ACTION_ORDER:
                if action not in rule.actions:
                    continue
                if report.total_actions >= budget:
                    break
                if self._perform(action, message, rule, report):
                    erledigt.append(action)

            if erledigt:
                bearbeitete_autoren.add(autor)
                self.store.mark_seen(
                    message.id,
                    platform=PLATFORM_DISCORD,
                    author=message.author.label,
                    rule_name=rule.name,
                    decision="bearbeitet: " + "+".join(erledigt),
                )
            else:
                self.store.mark_seen(
                    message.id,
                    platform=PLATFORM_DISCORD,
                    author=message.author.label,
                    rule_name=rule.name,
                    decision="keine Aktion",
                )

    def _perform(
        self,
        action: str,
        message: DiscordMessage,
        rule: DiscordRule,
        report: DiscordEngagementReport,
    ) -> bool:
        dry_run = getattr(self.client, "dry_run", False)

        if action == "react" and message.has_reacted(rule.emoji):
            report.skipped["react: Reaktion steht schon dran"] += 1
            return False

        if self.store.has_acted(
            action, message.id, platform=PLATFORM_DISCORD, include_dry_run=dry_run
        ):
            report.skipped[f"{action}: bereits erledigt"] += 1
            return False

        if not self._await_quota(action, report):
            return False

        text: str | None = None
        if action == "reply":
            try:
                generated = self.generator.generate_discord_reply(message, rule)
            except ContentError as exc:
                logger.warning("Antwort auf %s nicht erzeugt: %s", message.id, exc)
                report.skipped[f"reply: {exc}"] += 1
                return False
            text = generated.text

        if action == "react":
            result = self.client.react(message.channel_id, message.id, rule.emoji)
        else:
            result = self.client.post(text or "", message.channel_id, reply_to=message.id)

        if not result.ok:
            report.errors.append(f"{action} auf {message.id}: {result.error}")
            report.skipped[f"{action}: Fehler der Discord-API"] += 1
            return False

        self.store.record_action(
            action,
            platform=PLATFORM_DISCORD,
            target_id=message.id,
            target_author=message.author.label,
            rule_name=rule.name,
            text=text or (rule.emoji if action == "react" else None),
            dry_run=result.dry_run,
        )
        report.actions[action] += 1
        logger.info(
            "%s%s -> %s (%s) | %s",
            "[PROBELAUF] " if result.dry_run else "",
            action,
            message.author.label,
            rule.name,
            message.preview,
        )
        return True

    def _await_quota(self, action: str, report: DiscordEngagementReport) -> bool:
        """Kurze Wartezeiten aussitzen, lange auf den naechsten Durchlauf schieben."""
        decision = self.quota.check(action)
        if decision:
            return True

        wait = decision.retry_after_seconds
        if 0 < wait <= MAX_INLINE_WAIT_SECONDS:
            logger.debug("Warte %ds: %s", wait, decision.reason)
            self.sleeper(wait)
            decision = self.quota.check(action)
            if decision:
                return True

        report.skipped[f"{action}: {decision.reason}"] += 1
        return False

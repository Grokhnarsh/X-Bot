"""Eigene Beitraege erstellen und veroeffentlichen."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..config import Config
from ..content import ContentGenerator
from ..content.text import append_hashtags, tweet_length
from ..errors import ContentError
from ..models import ActionResult
from ..quota import QuotaGuard
from ..state import Store, utcnow

logger = logging.getLogger(__name__)


@dataclass
class PostReport:
    posted: bool = False
    text: str = ""
    source: str = ""
    reason: str = ""
    dry_run: bool = False
    #: True nur bei echten Stoerungen (Texterstellung, X-API). Ein regulaeres
    #: Ueberspringen - Zeitfenster zu, Limit erreicht - ist kein Fehler.
    failed: bool = False
    result: ActionResult | None = field(default=None, repr=False)

    def describe(self) -> str:
        if self.posted:
            marker = "[PROBELAUF] " if self.dry_run else ""
            return f"{marker}Beitrag veroeffentlicht ({self.source}, {tweet_length(self.text)} Zeichen)"
        return f"Kein Beitrag: {self.reason}"


class Poster:
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
        posting = self.config.posting

        if local.weekday() not in posting.active_weekdays:
            return False, f"{local:%A} ist kein aktiver Wochentag"

        start, end = posting.active_hours
        if not (start <= local.hour < end):
            return False, f"{local:%H:%M} liegt ausserhalb von {start:02d}:00-{end:02d}:00"

        return True, ""

    # -- Hauptablauf --------------------------------------------------------
    def run(self, *, topic: str | None = None, force: bool = False, now: datetime | None = None) -> PostReport:
        now = now or utcnow()
        cfg = self.config

        if not cfg.posting.enabled and not force:
            return PostReport(reason="posting.enabled ist false")

        if not force:
            open_now, why = self.window_open(now)
            if not open_now:
                return PostReport(reason=why)

        # Limits gelten immer - auch bei einem erzwungenen Aufruf.
        decision = self.quota.check("post", now)
        if not decision:
            return PostReport(reason=decision.reason)

        try:
            generated = self.generator.generate_post(topic=topic)
        except ContentError as exc:
            logger.warning("Texterstellung fehlgeschlagen: %s", exc)
            return PostReport(reason=f"Texterstellung fehlgeschlagen: {exc}", failed=True)

        text = generated.text
        if cfg.posting.include_hashtags and cfg.posting.hashtag_pool:
            pool = list(cfg.posting.hashtag_pool)
            self.generator.rng.shuffle(pool)
            text = append_hashtags(
                text,
                pool,
                max_chars=cfg.content.max_chars,
                max_tags=cfg.posting.max_hashtags,
            )

        result = self.client.post(text)
        if not result.ok:
            return PostReport(
                reason=result.error or "unbekannter Fehler", text=text, result=result, failed=True
            )

        self.store.record_action(
            "post",
            target_id=result.result_id,
            text=text,
            dry_run=result.dry_run,
            created_at=now,
        )
        logger.info(
            "%sBeitrag (%s, %d Zeichen): %s",
            "[PROBELAUF] " if result.dry_run else "",
            generated.source,
            tweet_length(text),
            text.replace("\n", " / "),
        )
        return PostReport(
            posted=True,
            text=text,
            source=generated.source,
            dry_run=result.dry_run,
            result=result,
        )

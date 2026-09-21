"""Hashtag-Monitoring: suchen, filtern, reagieren.

Ablauf eines Durchlaufs:

1. Aus allen Regel-Hashtags werden moeglichst wenige Suchanfragen gebaut.
2. Die Treffer werden entdoppelt; bereits bewertete Tweets fallen raus.
3. Jeder verbleibende Tweet durchlaeuft die Sicherheitsfilter und wird einer
   Regel zugeordnet.
4. Die Kandidaten werden nach Regelgewicht und Resonanz sortiert - das knappe
   Aktionsbudget geht an die besten zuerst.
5. Pro Aktion greifen Dedupe und Limits erneut, unmittelbar vor dem Senden.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Sequence

from ..client import build_search_query
from ..config import Config, Rule
from ..content import ContentGenerator
from ..errors import ContentError, CredentialsError, XBotError
from ..filters import TweetFilter, first_matching_rule
from ..models import Tweet
from ..quota import QuotaGuard
from ..state import Store, utcnow

logger = logging.getLogger(__name__)

#: Die X-API begrenzt Suchanfragen; 450 Zeichen lassen Luft fuer die Operatoren.
MAX_QUERY_LENGTH = 450

#: So lange wird innerhalb eines Durchlaufs auf den Mindestabstand gewartet.
#: Laengere Wartezeiten werden auf den naechsten Durchlauf verschoben.
MAX_INLINE_WAIT_SECONDS = 120

#: Feste Reihenfolge - erst die guenstigste, unaufdringlichste Aktion.
ACTION_ORDER = ("like", "repost", "reply")


def build_queries(
    hashtags: Sequence[str],
    *,
    languages: Sequence[str] = (),
    exclude_retweets: bool = True,
    exclude_replies: bool = True,
    own_handle: str = "",
    max_length: int = MAX_QUERY_LENGTH,
) -> list[str]:
    """Buendelt Hashtags zu moeglichst wenigen Suchanfragen.

    Eine Anfrage fuer alle Hashtags spart API-Kontingent; wird sie zu lang,
    wird sie aufgeteilt.
    """
    kwargs = dict(
        languages=languages,
        exclude_retweets=exclude_retweets,
        exclude_replies=exclude_replies,
        exclude_own_handle=own_handle,
    )
    queries: list[str] = []
    batch: list[str] = []

    for tag in hashtags:
        trial = [*batch, tag]
        if len(build_search_query(trial, **kwargs)) > max_length and batch:
            queries.append(build_search_query(batch, **kwargs))
            batch = [tag]
        else:
            batch = trial

    if batch:
        queries.append(build_search_query(batch, **kwargs))

    return [query for query in queries if query]


@dataclass
class EngagementReport:
    """Was ein Durchlauf bewirkt hat."""

    queries: int = 0
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
        acted = ", ".join(f"{count}x {name}" for name, count in sorted(self.actions.items())) or "keine Aktion"
        return (
            f"{self.queries} Suchanfrage(n), {self.found} Treffer, "
            f"{self.fresh} neu, {self.candidates} Kandidaten -> {acted}"
        )

    def top_skips(self, limit: int = 5) -> list[tuple[str, int]]:
        return self.skipped.most_common(limit)


class EngagementEngine:
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
        self._own_username: str | None = None

    # -- Eigener Account ----------------------------------------------------
    def own_username(self) -> str:
        """Der eigene Handle, um eigene Beitraege auszuschliessen."""
        if self._own_username is None:
            try:
                self._own_username = self.client.verify().username
            except (CredentialsError, XBotError) as exc:
                logger.debug("Eigener Account nicht ermittelbar: %s", exc)
                self._own_username = ""
        return self._own_username

    # -- Hauptablauf --------------------------------------------------------
    def run_cycle(self, now: datetime | None = None) -> EngagementReport:
        now = now or utcnow()
        report = EngagementReport()
        cfg = self.config

        if not cfg.engagement.enabled:
            report.errors.append("engagement.enabled ist false")
            return report
        if not cfg.rules:
            report.errors.append("keine Regeln konfiguriert")
            return report

        tweets = self._collect(report)
        if not tweets:
            return report

        candidates = self._select(tweets, report)
        report.candidates = len(candidates)
        if not candidates:
            return report

        self._act(candidates, report, now)
        return report

    # -- Schritt 1: suchen --------------------------------------------------
    def _collect(self, report: EngagementReport) -> list[Tweet]:
        cfg = self.config
        queries = build_queries(
            cfg.monitored_hashtags,
            languages=cfg.filters.languages,
            exclude_retweets=cfg.filters.skip_retweets,
            exclude_replies=cfg.filters.skip_replies,
            own_handle=self.own_username(),
        )
        report.queries = len(queries)

        collected: dict[str, Tweet] = {}
        for query in queries:
            try:
                results = self.client.search(
                    query,
                    max_results=cfg.engagement.max_results_per_query,
                    lookback_minutes=cfg.engagement.lookback_minutes,
                )
            except XBotError as exc:
                logger.error("Suche fehlgeschlagen: %s", exc)
                report.errors.append(str(exc))
                continue
            for tweet in results:
                if tweet.id:
                    collected.setdefault(tweet.id, tweet)

        report.found = len(collected)
        if not collected:
            return []

        # Bereits bewertete Tweets ueberspringen - eine Abfrage statt N.
        known = self.store.seen_ids(list(collected))
        fresh = [tweet for tweet_id, tweet in collected.items() if tweet_id not in known]
        report.fresh = len(fresh)
        if known:
            report.skipped["bereits bewertet"] += len(known)
        return fresh

    # -- Schritt 2: filtern und zuordnen ------------------------------------
    def _select(self, tweets: Sequence[Tweet], report: EngagementReport) -> list[tuple[Tweet, Rule]]:
        tweet_filter = TweetFilter(self.config.filters, own_username=self.own_username())
        candidates: list[tuple[Tweet, Rule]] = []

        for tweet in tweets:
            verdict = tweet_filter.check(tweet)
            if not verdict:
                report.skipped[verdict.reason] += 1
                self.store.mark_seen(
                    tweet.id,
                    author=tweet.author.username,
                    decision=f"gefiltert: {verdict.reason}",
                )
                continue

            rule, why = first_matching_rule(self.config.rules, tweet)
            if rule is None:
                report.skipped[why] += 1
                self.store.mark_seen(
                    tweet.id, author=tweet.author.username, decision=f"keine Regel: {why}"
                )
                continue

            candidates.append((tweet, rule))

        # Bestes zuerst: hoeheres Regelgewicht, dann mehr Resonanz.
        candidates.sort(key=lambda pair: (pair[1].weight, pair[0].like_count), reverse=True)
        return candidates

    # -- Schritt 3: handeln -------------------------------------------------
    def _act(self, candidates: Sequence[tuple[Tweet, Rule]], report: EngagementReport, now: datetime) -> None:
        budget = self.config.engagement.max_actions_per_cycle
        if budget <= 0:
            report.skipped["Aktionsbudget ist 0"] += len(candidates)
            return

        # Hoechstens ein Tweet pro Autor und Durchlauf - sonst wirkt der Bot
        # aufdringlich, wenn jemand mehrfach zum selben Hashtag postet.
        handled_authors: set[str] = set()

        for tweet, rule in candidates:
            if report.total_actions >= budget:
                report.skipped["Aktionsbudget des Durchlaufs erschoepft"] += 1
                continue

            author = (tweet.author.username or tweet.author.id or "").lower()
            if author and author in handled_authors:
                report.skipped["schon eine Aktion fuer diesen Autor in diesem Durchlauf"] += 1
                continue

            performed: list[str] = []
            for action in ACTION_ORDER:
                if action not in rule.actions:
                    continue
                if report.total_actions >= budget:
                    break
                outcome = self._perform(action, tweet, rule, report)
                if outcome:
                    performed.append(action)

            if performed:
                handled_authors.add(author)
                self.store.mark_seen(
                    tweet.id,
                    author=tweet.author.username,
                    rule_name=rule.name,
                    decision="bearbeitet: " + "+".join(performed),
                )
            else:
                self.store.mark_seen(
                    tweet.id, author=tweet.author.username, rule_name=rule.name, decision="keine Aktion"
                )

    def _perform(self, action: str, tweet: Tweet, rule: Rule, report: EngagementReport) -> bool:
        dry_run = getattr(self.client, "dry_run", False)

        if self.store.has_acted(action, tweet.id, include_dry_run=dry_run):
            report.skipped[f"{action}: bereits erledigt"] += 1
            return False

        if not self._await_quota(action, report):
            return False

        text: str | None = None
        if action == "reply":
            try:
                generated = self.generator.generate_reply(tweet, rule)
            except ContentError as exc:
                logger.warning("Antwort auf %s nicht erzeugt: %s", tweet.id, exc)
                report.skipped[f"reply: {exc}"] += 1
                return False
            text = generated.text

        if action == "like":
            result = self.client.like(tweet.id)
        elif action == "repost":
            result = self.client.repost(tweet.id)
        else:
            result = self.client.post(text or "", in_reply_to=tweet.id)

        if not result.ok:
            report.errors.append(f"{action} auf {tweet.id}: {result.error}")
            report.skipped[f"{action}: Fehler der X-API"] += 1
            return False

        self.store.record_action(
            action,
            target_id=tweet.id,
            target_author=tweet.author.username,
            rule_name=rule.name,
            text=text,
            dry_run=result.dry_run,
        )
        report.actions[action] += 1
        logger.info(
            "%s%s -> @%s (%s) | %s",
            "[PROBELAUF] " if result.dry_run else "",
            action,
            tweet.author.username or "?",
            rule.name,
            tweet.preview,
        )
        return True

    def _await_quota(self, action: str, report: EngagementReport) -> bool:
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

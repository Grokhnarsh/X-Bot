"""Sicherheitsfilter fuer fremde Beitraege.

Jeder Tweet muss diese Pruefung bestehen, bevor der Bot ihn liked, teilt oder
beantwortet. Die Filter sind der Unterschied zwischen einem nuetzlichen Bot und
einem, der irgendwann versehentlich Gluecksspielwerbung teilt.

Die Reihenfolge ist Absicht: erst die harten Ausschluesse (eigener Beitrag,
Sperrliste), dann die guenstigen Textpruefungen, zuletzt die Autorenkriterien.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import FilterSettings, Rule
from .content.text import URL_PATTERN
from .models import Tweet

MENTION_PATTERN = re.compile(r"[@#]\w+", re.UNICODE)


@dataclass(frozen=True)
class FilterVerdict:
    """Ergebnis einer Filterpruefung - bei Ablehnung mit Begruendung."""

    passed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.passed


PASSED = FilterVerdict(True)


def _rejected(reason: str) -> FilterVerdict:
    return FilterVerdict(False, reason)


def substantive_length(text: str) -> int:
    """Textlaenge ohne URLs, Hashtags und Erwaehnungen.

    Ein Beitrag aus zehn Hashtags und einem Link hat viele Zeichen, aber
    keinen Inhalt - diese Zaehlung entlarvt das.
    """
    without_urls = URL_PATTERN.sub(" ", text)
    without_tags = MENTION_PATTERN.sub(" ", without_urls)
    return len(" ".join(without_tags.split()))


class TweetFilter:
    def __init__(self, settings: FilterSettings, *, own_username: str = "") -> None:
        self.settings = settings
        self.own_username = own_username.lstrip("@").lower()

    def check(self, tweet: Tweet) -> FilterVerdict:
        s = self.settings
        author = (tweet.author.username or "").lower()
        text_lower = tweet.text.lower()

        # -- Harte Ausschluesse ------------------------------------------------
        if not tweet.id:
            return _rejected("ohne Tweet-ID")

        if self.own_username and author == self.own_username:
            return _rejected("eigener Beitrag")

        if s.allowed_users and author not in s.allowed_users:
            return _rejected(f"@{author or '?'} steht nicht auf der Positivliste")

        if author and author in s.blocked_users:
            return _rejected(f"@{author} steht auf der Sperrliste")

        # -- Art des Beitrags --------------------------------------------------
        if s.skip_retweets and tweet.is_retweet:
            return _rejected("ist ein Retweet")

        if s.skip_replies and tweet.is_reply:
            return _rejected("ist eine Antwort")

        if s.skip_quotes and tweet.is_quote:
            return _rejected("ist ein Zitat-Beitrag")

        if s.skip_sensitive and tweet.possibly_sensitive:
            return _rejected("als sensibel markiert")

        # -- Sprache -----------------------------------------------------------
        if s.languages and tweet.lang and tweet.lang not in s.languages:
            return _rejected(f"Sprache '{tweet.lang}' nicht in {list(s.languages)}")

        # -- Inhalt ------------------------------------------------------------
        for keyword in s.blocked_keywords:
            if keyword and keyword in text_lower:
                return _rejected(f"enthaelt gesperrten Begriff '{keyword}'")

        if s.skip_links and tweet.urls:
            return _rejected("enthaelt einen Link")

        if s.max_hashtags_in_tweet and len(tweet.hashtags) > s.max_hashtags_in_tweet:
            return _rejected(f"{len(tweet.hashtags)} Hashtags (erlaubt: {s.max_hashtags_in_tweet})")

        if s.max_mentions_in_tweet and len(tweet.mentions) > s.max_mentions_in_tweet:
            return _rejected(f"{len(tweet.mentions)} Erwaehnungen (erlaubt: {s.max_mentions_in_tweet})")

        if s.min_tweet_length:
            length = substantive_length(tweet.text)
            if length < s.min_tweet_length:
                return _rejected(f"nur {length} Zeichen Inhalt (mindestens {s.min_tweet_length})")

        # -- Autor -------------------------------------------------------------
        # Follower-Kriterien nur pruefen, wenn das Profil wirklich vorliegt.
        # Fehlt die Nutzer-Erweiterung in der API-Antwort, wuerde sonst alles
        # an "0 Follower" scheitern.
        if tweet.author.username:
            if s.min_author_followers and tweet.author.followers < s.min_author_followers:
                return _rejected(
                    f"@{author} hat {tweet.author.followers} Follower (mindestens {s.min_author_followers})"
                )
            if s.max_author_followers and tweet.author.followers > s.max_author_followers:
                return _rejected(
                    f"@{author} hat {tweet.author.followers} Follower (hoechstens {s.max_author_followers})"
                )
            if s.require_verified and not tweet.author.verified:
                return _rejected(f"@{author} ist nicht verifiziert")

        return PASSED


def rule_accepts(rule: Rule, tweet: Tweet) -> FilterVerdict:
    """Regelspezifische Zusatzbedingungen (Sprache, Mindestresonanz)."""
    if not rule.matches(tweet.hashtags):
        return _rejected(f"Hashtags passen nicht zu Regel '{rule.name}'")
    if rule.languages and tweet.lang and tweet.lang not in rule.languages:
        return _rejected(f"Sprache '{tweet.lang}' passt nicht zu Regel '{rule.name}'")
    if rule.min_likes and tweet.like_count < rule.min_likes:
        return _rejected(f"nur {tweet.like_count} Likes (Regel verlangt {rule.min_likes})")
    if rule.min_reposts and tweet.repost_count < rule.min_reposts:
        return _rejected(f"nur {tweet.repost_count} Reposts (Regel verlangt {rule.min_reposts})")
    return PASSED


def first_matching_rule(rules: tuple[Rule, ...], tweet: Tweet) -> tuple[Rule | None, str]:
    """Erste Regel, die auf den Tweet passt - samt Begruendung bei Misserfolg."""
    hashtag_misses: list[str] = []
    other_reasons: list[str] = []
    for rule in rules:
        verdict = rule_accepts(rule, tweet)
        if verdict:
            return rule, ""
        # Eine Regel, deren Hashtags passen, die aber an einer anderen
        # Bedingung scheitert, erklaert die Ablehnung besser.
        if rule.matches(tweet.hashtags):
            other_reasons.append(verdict.reason)
        else:
            hashtag_misses.append(verdict.reason)
    if other_reasons:
        return None, other_reasons[0]
    if hashtag_misses:
        return None, "kein passender Hashtag"
    return None, "keine Regel definiert"

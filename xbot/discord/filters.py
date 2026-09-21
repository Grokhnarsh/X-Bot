"""Sicherheitsfilter fuer fremde Discord-Nachrichten.

Jede Nachricht muss diese Pruefung bestehen, bevor der Bot darauf reagiert
oder antwortet. Gegenueber der X-Seite kommt ein Punkt hinzu, der in Discord
wirklich wehtut: **Bots.** Antwortet der Bot auf einen anderen Bot, der
seinerseits antwortet, schaukelt sich ein Kanal in Minuten hoch. Deshalb ist
``skip_bots`` voreingestellt an und die eigene Nachricht wird doppelt
ausgeschlossen - ueber die Konto-ID und ueber das Bot-Kennzeichen.

Die Reihenfolge ist wie bei X Absicht: erst die harten Ausschluesse, dann die
guenstigen Textpruefungen.
"""

from __future__ import annotations

import re

from ..config import DiscordFilterSettings, DiscordRule
from ..filters import PASSED, FilterVerdict
from .models import URL_PATTERN, DiscordMessage

#: Discord-Markup: <@123>, <@!123>, <#123>, <@&123>, <:name:123>, <a:name:123>.
MARKUP_PATTERN = re.compile(r"<(?:@[!&]?|#|a?:\w+:)\d+>|@(?:everyone|here)")

#: Zusammenhaengende Codebloecke zaehlen nicht als Gespraechsinhalt.
CODE_PATTERN = re.compile(r"```.*?```|`[^`]*`", re.DOTALL)


def _rejected(reason: str) -> FilterVerdict:
    return FilterVerdict(False, reason)


def substantive_length(text: str) -> int:
    """Textlaenge ohne URLs, Erwaehnungen, Emoji-Markup und Code.

    Eine Nachricht aus drei Pings und einem Link hat viele Zeichen, aber
    nichts, worauf sich antworten liesse.
    """
    ohne_code = CODE_PATTERN.sub(" ", text)
    ohne_urls = URL_PATTERN.sub(" ", ohne_code)
    ohne_markup = MARKUP_PATTERN.sub(" ", ohne_urls)
    return len(" ".join(ohne_markup.split()))


class DiscordMessageFilter:
    def __init__(
        self,
        settings: DiscordFilterSettings,
        *,
        own_user_id: str = "",
    ) -> None:
        self.settings = settings
        self.own_user_id = str(own_user_id or "")

    def check(self, message: DiscordMessage) -> FilterVerdict:
        s = self.settings
        autor = message.author
        name = (autor.username or "").lower()
        text_lower = message.content.lower()

        # -- Harte Ausschluesse ------------------------------------------------
        if not message.id:
            return _rejected("ohne Nachrichten-ID")

        if self.own_user_id and autor.id == self.own_user_id:
            return _rejected("eigene Nachricht")

        if s.skip_bots and autor.is_bot:
            return _rejected(f"{autor.label} ist ein Bot")

        if s.allowed_users and not self._auf_liste(autor, s.allowed_users):
            return _rejected(f"{autor.label} steht nicht auf der Positivliste")

        if self._auf_liste(autor, s.blocked_users):
            return _rejected(f"{autor.label} steht auf der Sperrliste")

        # -- Art der Nachricht -------------------------------------------------
        if s.skip_replies and message.is_reply:
            return _rejected("ist eine Antwort auf eine andere Nachricht")

        if message.mention_everyone:
            # @everyone ist fast immer eine Ankuendigung der Serverleitung.
            return _rejected("erwaehnt @everyone oder @here")

        # -- Inhalt ------------------------------------------------------------
        for begriff in s.blocked_keywords:
            if begriff and begriff in text_lower:
                return _rejected(f"enthaelt gesperrten Begriff '{begriff}'")

        if s.skip_links and message.urls:
            return _rejected("enthaelt einen Link")

        if s.max_mentions and len(message.mentions) > s.max_mentions:
            return _rejected(f"{len(message.mentions)} Erwaehnungen (erlaubt: {s.max_mentions})")

        if s.min_message_length:
            laenge = substantive_length(message.content)
            if laenge < s.min_message_length:
                # Fehlt das Message-Content-Intent, ist der Inhalt immer leer -
                # dieser Hinweis erspart die Suche nach der Ursache.
                if not message.content:
                    return _rejected(
                        "ohne Textinhalt - fehlt im Developer Portal das "
                        "MESSAGE CONTENT INTENT?"
                    )
                return _rejected(f"nur {laenge} Zeichen Inhalt (mindestens {s.min_message_length})")

        return PASSED

    @staticmethod
    def _auf_liste(autor, liste: tuple[str, ...]) -> bool:
        """Listen duerfen Konto-IDs oder Benutzernamen enthalten."""
        eintraege = {eintrag.lstrip("@").lower() for eintrag in liste if eintrag}
        if not eintraege:
            return False
        kandidaten = {
            wert.lower()
            for wert in (autor.id, autor.username, autor.display_name)
            if wert
        }
        return bool(kandidaten & eintraege)


def rule_accepts(rule: DiscordRule, message: DiscordMessage) -> FilterVerdict:
    """Regelspezifische Zusatzbedingungen (Kanal, Schluesselwoerter, Laenge)."""
    if rule.channels and message.channel_id and message.channel_id not in rule.channels:
        return _rejected(f"Kanal gehoert nicht zu Regel '{rule.name}'")
    if not rule.matches(message.content, message.channel_id):
        return _rejected(f"Schluesselwoerter passen nicht zu Regel '{rule.name}'")
    if rule.min_length:
        laenge = substantive_length(message.content)
        if laenge < rule.min_length:
            return _rejected(f"nur {laenge} Zeichen (Regel verlangt {rule.min_length})")
    return PASSED


def first_matching_rule(
    rules: tuple[DiscordRule, ...], message: DiscordMessage
) -> tuple[DiscordRule | None, str]:
    """Erste Regel, die auf die Nachricht passt - samt Begruendung bei Misserfolg."""
    keyword_misses: list[str] = []
    other_reasons: list[str] = []
    for rule in rules:
        verdict = rule_accepts(rule, message)
        if verdict:
            return rule, ""
        # Eine Regel, deren Schluesselwoerter passen, die aber an einer
        # anderen Bedingung scheitert, erklaert die Ablehnung besser.
        if rule.matches(message.content, message.channel_id):
            other_reasons.append(verdict.reason)
        else:
            keyword_misses.append(verdict.reason)
    if other_reasons:
        return None, other_reasons[0]
    if keyword_misses:
        return None, "kein passendes Schluesselwort"
    return None, "keine Regel definiert"

"""Wrapper um die X-API v2 (tweepy).

Zwei Dinge macht diese Schicht, die der rohe tweepy-Client nicht macht:

* **Probelauf.** Ist ``dry_run`` aktiv, wird keine einzige Schreibanfrage
  gesendet. Lesen (Suche, Profil) laeuft weiter, damit ein Probelauf zeigt,
  was der Bot tatsaechlich taete.
* **Verstaendliche Fehler.** Die typischen Stolpersteine der X-API - fehlende
  Schreibrechte, zu niedrige Zugriffsstufe, doppelter Text - werden in
  Klartext uebersetzt statt als nackter HTTP-Code durchgereicht.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import tweepy

from .config import Credentials
from .errors import CredentialsError, XBotError
from .models import ActionResult, Author, Tweet

logger = logging.getLogger(__name__)

TWEET_FIELDS = [
    "created_at",
    "lang",
    "public_metrics",
    "entities",
    "possibly_sensitive",
    "referenced_tweets",
    "author_id",
    "conversation_id",
    "in_reply_to_user_id",
]
USER_FIELDS = ["username", "name", "public_metrics", "verified", "created_at"]
EXPANSIONS = ["author_id"]

#: Die Recent-Search der X-API deckt nur die letzten sieben Tage ab.
MAX_LOOKBACK_MINUTES = 7 * 24 * 60 - 30


class XClientError(XBotError):
    """Die X-API hat die Anfrage abgelehnt."""


def _describe(exc: Exception) -> str:
    """Macht aus einer tweepy-Ausnahme eine Zeile, die weiterhilft."""
    if isinstance(exc, tweepy.Unauthorized):
        return (
            "401 Nicht autorisiert - Schluessel oder Token sind falsch. "
            "Pruefe X_API_KEY/X_API_SECRET/X_ACCESS_TOKEN/X_ACCESS_TOKEN_SECRET in der .env."
        )
    if isinstance(exc, tweepy.Forbidden):
        detail = "; ".join(str(m) for m in getattr(exc, "api_messages", []) or []) or str(exc)
        return (
            f"403 Verboten - {detail}. Haeufigste Ursachen: die App hat nur Leserechte "
            "(App permissions auf 'Read and write' stellen und Access Token DANACH neu erzeugen), "
            "der Text ist ein Duplikat, oder die Zugriffsstufe deckt den Endpunkt nicht ab."
        )
    if isinstance(exc, tweepy.TooManyRequests):
        return "429 Rate Limit der X-API erreicht - der naechste Zyklus versucht es erneut."
    if isinstance(exc, tweepy.NotFound):
        return "404 Nicht gefunden - der Tweet wurde vermutlich geloescht."
    if isinstance(exc, tweepy.BadRequest):
        detail = "; ".join(str(m) for m in getattr(exc, "api_messages", []) or []) or str(exc)
        return f"400 Ungueltige Anfrage - {detail}"
    if isinstance(exc, tweepy.TwitterServerError):
        return f"5xx Serverfehler bei X - spaeter erneut versuchen ({exc})."
    return f"{type(exc).__name__}: {exc}"


class XClient:
    """Alle Schreib- und Lesezugriffe auf X laufen durch diese Klasse."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        dry_run: bool = True,
        wait_on_rate_limit: bool = True,
    ) -> None:
        self.credentials = credentials
        self.dry_run = dry_run
        self.wait_on_rate_limit = wait_on_rate_limit
        self._client: tweepy.Client | None = None
        self._me: Author | None = None

    # -- Verbindung ---------------------------------------------------------
    @property
    def client(self) -> tweepy.Client:
        if self._client is None:
            creds = self.credentials
            if not creds.has_write_access and not creds.bearer_token:
                raise CredentialsError(
                    "Keine X-Zugangsdaten gefunden. Lege eine .env nach dem Muster von "
                    ".env.example an (siehe 'xbot doctor')."
                )
            self._client = tweepy.Client(
                bearer_token=creds.bearer_token or None,
                consumer_key=creds.api_key or None,
                consumer_secret=creds.api_secret or None,
                access_token=creds.access_token or None,
                access_token_secret=creds.access_token_secret or None,
                wait_on_rate_limit=self.wait_on_rate_limit,
            )
        return self._client

    @property
    def _search_uses_user_auth(self) -> bool:
        """Ohne Bearer Token muss die Suche ueber den Nutzerkontext laufen."""
        return not bool(self.credentials.bearer_token)

    def verify(self) -> Author:
        """Prueft die Zugangsdaten und liefert das eigene Profil."""
        if self._me is not None:
            return self._me
        if not self.credentials.has_write_access:
            missing = ", ".join(self.credentials.missing_write_fields())
            raise CredentialsError(f"Fuer Schreibaktionen fehlen diese Werte in der .env: {missing}")
        try:
            response = self.client.get_me(user_fields=USER_FIELDS)
        except tweepy.TweepyException as exc:
            raise CredentialsError(_describe(exc)) from exc
        if not response or not response.data:
            raise CredentialsError("X hat kein Profil zurueckgegeben - sind die Tokens gueltig?")
        self._me = Author.from_api(response.data)
        return self._me

    @property
    def me(self) -> Author | None:
        return self._me

    # -- Lesen --------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        max_results: int = 25,
        lookback_minutes: int = 180,
        since_id: str | None = None,
    ) -> list[Tweet]:
        """Recent-Search der X-API v2.

        Benoetigt mindestens die Zugriffsstufe "Basic" im Developer-Portal.
        Die Free-Stufe erlaubt kein Suchen - dann kommt ein 403 zurueck.
        """
        if not query.strip():
            return []

        lookback_minutes = max(1, min(lookback_minutes, MAX_LOOKBACK_MINUTES))
        # X verlangt einen Startzeitpunkt, der mindestens 10 Sekunden zurueckliegt.
        start_time = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)

        params: dict[str, Any] = {
            "query": query,
            "max_results": max(10, min(int(max_results), 100)),
            "tweet_fields": TWEET_FIELDS,
            "user_fields": USER_FIELDS,
            "expansions": EXPANSIONS,
            "start_time": start_time,
            "user_auth": self._search_uses_user_auth,
        }
        if since_id:
            # since_id und start_time schliessen sich gegenseitig aus.
            params.pop("start_time")
            params["since_id"] = since_id

        logger.debug("Suche: %s", query)
        try:
            response = self.client.search_recent_tweets(**params)
        except tweepy.Forbidden as exc:
            raise XClientError(
                _describe(exc)
                + " Fuer die Hashtag-Suche ist mindestens die Zugriffsstufe 'Basic' noetig."
            ) from exc
        except tweepy.TweepyException as exc:
            raise XClientError(_describe(exc)) from exc

        if not response or not response.data:
            return []

        users: dict[str, Author] = {}
        includes = response.includes or {}
        for user in includes.get("users", []) or []:
            author = Author.from_api(user)
            if author.id:
                users[author.id] = author

        return [Tweet.from_api(item, users) for item in response.data]

    # -- Schreiben ----------------------------------------------------------
    def post(
        self,
        text: str,
        *,
        in_reply_to: str | None = None,
        quote_of: str | None = None,
    ) -> ActionResult:
        action = "reply" if in_reply_to else "post"
        text = text.strip()
        if not text:
            return ActionResult(action, ok=False, error="Leerer Text - nichts zu senden.")

        if self.dry_run:
            target = in_reply_to or quote_of
            logger.info("[PROBELAUF] %s%s: %s", action, f" -> {target}" if target else "", text)
            return ActionResult(action, ok=True, dry_run=True, target_id=in_reply_to, text=text)

        kwargs: dict[str, Any] = {"text": text}
        if in_reply_to:
            kwargs["in_reply_to_tweet_id"] = in_reply_to
        if quote_of:
            kwargs["quote_tweet_id"] = quote_of

        try:
            response = self.client.create_tweet(**kwargs)
        except tweepy.TweepyException as exc:
            message = _describe(exc)
            logger.error("%s fehlgeschlagen: %s", action, message)
            return ActionResult(action, ok=False, target_id=in_reply_to, text=text, error=message)

        result_id = str((response.data or {}).get("id", "")) if response else ""
        logger.info("%s veroeffentlicht (id=%s): %s", action, result_id, text)
        return ActionResult(action, ok=True, target_id=in_reply_to, result_id=result_id, text=text)

    def like(self, tweet_id: str) -> ActionResult:
        return self._simple_write("like", tweet_id, "like")

    def repost(self, tweet_id: str) -> ActionResult:
        return self._simple_write("repost", tweet_id, "retweet")

    def _simple_write(self, action: str, tweet_id: str, method: str) -> ActionResult:
        """``method`` ist der Name der tweepy-Methode; sie wird erst im
        Echtbetrieb aufgeloest, damit der Probelauf ohne Zugangsdaten laeuft."""
        tweet_id = str(tweet_id)
        if self.dry_run:
            logger.info("[PROBELAUF] %s -> %s", action, tweet_id)
            return ActionResult(action, ok=True, dry_run=True, target_id=tweet_id)
        try:
            getattr(self.client, method)(tweet_id)
        except tweepy.TweepyException as exc:
            message = _describe(exc)
            logger.error("%s auf %s fehlgeschlagen: %s", action, tweet_id, message)
            return ActionResult(action, ok=False, target_id=tweet_id, error=message)
        logger.info("%s ausgefuehrt -> %s", action, tweet_id)
        return ActionResult(action, ok=True, target_id=tweet_id)


def build_search_query(
    hashtags: Sequence[str],
    *,
    languages: Sequence[str] = (),
    exclude_retweets: bool = True,
    exclude_replies: bool = True,
    exclude_own_handle: str = "",
) -> str:
    """Baut eine Suchanfrage in der Query-Syntax der X-API v2.

    Beispiel::

        (#python OR #ki) -is:retweet -is:reply -from:meinbot (lang:de OR lang:en)
    """
    tags = [t if t.startswith("#") else f"#{t}" for t in (h.strip() for h in hashtags) if t]
    if not tags:
        return ""

    parts = [f"({' OR '.join(tags)})"] if len(tags) > 1 else [tags[0]]
    if exclude_retweets:
        parts.append("-is:retweet")
    if exclude_replies:
        parts.append("-is:reply")
    if exclude_own_handle:
        parts.append(f"-from:{exclude_own_handle.lstrip('@')}")

    langs = [lang.strip().lower() for lang in languages if lang.strip()]
    if len(langs) == 1:
        parts.append(f"lang:{langs[0]}")
    elif len(langs) > 1:
        parts.append("(" + " OR ".join(f"lang:{lang}" for lang in langs) + ")")

    return " ".join(parts)

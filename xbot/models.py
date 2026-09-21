"""Datenmodelle fuer Tweets und Aktionsergebnisse.

Die X-API liefert je nach Zugriffsstufe unterschiedlich vollstaendige Objekte.
``Tweet.from_api`` normalisiert das auf eine feste Form, damit Filter und
Regeln nicht an jeder Stelle auf fehlende Felder pruefen muessen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def _as_dict(obj: Any) -> dict[str, Any]:
    """Tweepy-Objekte bieten ``.data``; Mappings werden direkt genutzt."""
    if obj is None:
        return {}
    if isinstance(obj, Mapping):
        return dict(obj)
    data = getattr(obj, "data", None)
    if isinstance(data, Mapping):
        return dict(data)
    return {}


@dataclass(frozen=True)
class Author:
    id: str = ""
    username: str = ""
    name: str = ""
    followers: int = 0
    following: int = 0
    tweet_count: int = 0
    verified: bool = False
    created_at: datetime | None = None

    @classmethod
    def from_api(cls, user: Any) -> "Author":
        data = _as_dict(user)
        metrics = data.get("public_metrics") or {}
        created = data.get("created_at")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                created = None
        return cls(
            id=str(data.get("id", "")),
            username=str(data.get("username", "")),
            name=str(data.get("name", "")),
            followers=int(metrics.get("followers_count", 0) or 0),
            following=int(metrics.get("following_count", 0) or 0),
            tweet_count=int(metrics.get("tweet_count", 0) or 0),
            # "verified" ist je nach Zugriffsstufe nicht enthalten.
            verified=bool(data.get("verified", False)),
            created_at=created if isinstance(created, datetime) else None,
        )


@dataclass(frozen=True)
class Tweet:
    id: str
    text: str
    author: Author = field(default_factory=Author)
    created_at: datetime | None = None
    lang: str = ""
    hashtags: tuple[str, ...] = ()
    mentions: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()
    like_count: int = 0
    repost_count: int = 0
    reply_count: int = 0
    quote_count: int = 0
    is_retweet: bool = False
    is_reply: bool = False
    is_quote: bool = False
    possibly_sensitive: bool = False

    @property
    def url(self) -> str:
        handle = self.author.username or "i"
        return f"https://x.com/{handle}/status/{self.id}"

    @property
    def preview(self) -> str:
        """Einzeilige Kurzfassung fuer Logausgaben."""
        flat = " ".join(self.text.split())
        return flat if len(flat) <= 90 else flat[:87] + "..."

    @classmethod
    def from_api(cls, tweet: Any, users: Mapping[str, Author] | None = None) -> "Tweet":
        data = _as_dict(tweet)
        users = users or {}
        metrics = data.get("public_metrics") or {}
        entities = data.get("entities") or {}

        hashtags = tuple(
            f"#{str(tag.get('tag', '')).lower()}"
            for tag in (entities.get("hashtags") or [])
            if tag.get("tag")
        )
        mentions = tuple(
            str(m.get("username", "")).lower()
            for m in (entities.get("mentions") or [])
            if m.get("username")
        )
        urls = tuple(
            str(u.get("expanded_url") or u.get("url", ""))
            for u in (entities.get("urls") or [])
            if u.get("expanded_url") or u.get("url")
        )

        referenced = data.get("referenced_tweets") or []
        ref_types = {str(_as_dict(ref).get("type") or getattr(ref, "type", "")) for ref in referenced}

        created = data.get("created_at")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                created = None
        if isinstance(created, datetime) and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        author_id = str(data.get("author_id", ""))
        text = str(data.get("text", ""))

        return cls(
            id=str(data.get("id", "")),
            text=text,
            author=users.get(author_id, Author(id=author_id)),
            created_at=created if isinstance(created, datetime) else None,
            lang=str(data.get("lang", "") or "").lower(),
            hashtags=hashtags,
            mentions=mentions,
            urls=urls,
            like_count=int(metrics.get("like_count", 0) or 0),
            repost_count=int(metrics.get("retweet_count", 0) or 0),
            reply_count=int(metrics.get("reply_count", 0) or 0),
            quote_count=int(metrics.get("quote_count", 0) or 0),
            # Ein Retweet beginnt im Volltext immer mit "RT @" - das ist der
            # zuverlaessigste Hinweis, wenn referenced_tweets fehlt.
            is_retweet="retweeted" in ref_types or text.startswith("RT @"),
            is_reply="replied_to" in ref_types or bool(data.get("in_reply_to_user_id")),
            is_quote="quoted" in ref_types,
            possibly_sensitive=bool(data.get("possibly_sensitive", False)),
        )


@dataclass(frozen=True)
class ActionResult:
    """Ergebnis einer Schreibaktion."""

    action: str
    ok: bool
    dry_run: bool = False
    target_id: str | None = None
    result_id: str | None = None
    text: str | None = None
    error: str | None = None

    @property
    def status(self) -> str:
        if not self.ok:
            return "FEHLER"
        return "PROBELAUF" if self.dry_run else "OK"

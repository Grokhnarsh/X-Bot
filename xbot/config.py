"""Laden und Validieren der Bot-Konfiguration.

Die Konfiguration kommt aus zwei Quellen:

* ``config.yaml``  - Verhalten des Bots (Regeln, Limits, Filter)
* ``.env``         - Zugangsdaten und der Not-Aus-Schalter ``XBOT_DRY_RUN``

Alles wird beim Laden validiert. Ein Tippfehler in der YAML soll den Bot beim
Start stoppen und nicht erst mitten im Betrieb auffallen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .errors import ConfigError

VALID_ACTIONS = ("like", "repost", "reply")
VALID_PROVIDERS = ("auto", "ai", "template")
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
VALID_MATCH_MODES = ("any", "all")

#: X erlaubt 280 Zeichen pro Beitrag.
TWEET_LIMIT = 280


# ---------------------------------------------------------------------------
# Hilfsfunktionen zum typsicheren Auslesen
# ---------------------------------------------------------------------------
def _section(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"Abschnitt '{key}' muss ein Objekt sein, ist aber {type(value).__name__}.")
    return dict(value)


def _bool(data: Mapping[str, Any], key: str, default: bool, *, ctx: str = "") -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1", "on", "ja"):
            return True
        if lowered in ("false", "no", "0", "off", "nein"):
            return False
    raise ConfigError(f"{ctx}{key}: erwartet true/false, erhalten {value!r}.")


def _int(
    data: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    ctx: str = "",
) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(str(value))
        except (TypeError, ValueError):
            raise ConfigError(f"{ctx}{key}: erwartet eine ganze Zahl, erhalten {value!r}.") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{ctx}{key}: muss mindestens {minimum} sein (ist {value}).")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{ctx}{key}: darf hoechstens {maximum} sein (ist {value}).")
    return value


def _float(
    data: Mapping[str, Any],
    key: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    ctx: str = "",
) -> float:
    value = data.get(key, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{ctx}{key}: erwartet eine Zahl, erhalten {value!r}.") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{ctx}{key}: muss mindestens {minimum} sein (ist {value}).")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{ctx}{key}: darf hoechstens {maximum} sein (ist {value}).")
    return value


def _str(data: Mapping[str, Any], key: str, default: str, *, ctx: str = "") -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{ctx}{key}: erwartet Text, erhalten {type(value).__name__}.")
    return value.strip()


def _str_list(data: Mapping[str, Any], key: str, default: Sequence[str] = (), *, ctx: str = "") -> tuple[str, ...]:
    value = data.get(key, default)
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence):
        raise ConfigError(f"{ctx}{key}: erwartet eine Liste, erhalten {type(value).__name__}.")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"{ctx}{key}: Eintraege muessen Text sein, gefunden {item!r}.")
        item = item.strip()
        if item:
            out.append(item)
    return tuple(out)


def _int_list(data: Mapping[str, Any], key: str, default: Sequence[int], *, ctx: str = "") -> tuple[int, ...]:
    value = data.get(key, default)
    if value is None:
        return tuple(default)
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ConfigError(f"{ctx}{key}: erwartet eine Liste von Zahlen.")
    out: list[int] = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            raise ConfigError(f"{ctx}{key}: Eintraege muessen Zahlen sein, gefunden {item!r}.") from None
    return tuple(out)


def normalise_hashtag(tag: str) -> str:
    """``KI`` und ``#ki`` werden beide zu ``#ki``."""
    tag = tag.strip().lstrip("#").strip()
    return f"#{tag.lower()}" if tag else ""


# ---------------------------------------------------------------------------
# Konfigurationsabschnitte
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BotSettings:
    dry_run: bool = True
    timezone: str = "Europe/Berlin"
    language: str = "de"
    persona: str = ""
    topics: tuple[str, ...] = ()

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> "BotSettings":
        ctx = "bot."
        timezone = _str(data, "timezone", "Europe/Berlin", ctx=ctx) or "UTC"
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"{ctx}timezone: '{timezone}' ist keine gueltige Zeitzone ({exc}).") from None
        return cls(
            dry_run=_bool(data, "dry_run", True, ctx=ctx),
            timezone=timezone,
            language=_str(data, "language", "de", ctx=ctx).lower() or "de",
            persona=_str(data, "persona", "", ctx=ctx),
            topics=_str_list(data, "topics", ctx=ctx),
        )


@dataclass(frozen=True)
class PostingSettings:
    enabled: bool = True
    interval_minutes: int = 240
    jitter_minutes: int = 60
    active_hours: tuple[int, int] = (8, 22)
    active_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    max_per_day: int = 5
    include_hashtags: bool = True
    max_hashtags: int = 2
    hashtag_pool: tuple[str, ...] = ()

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> "PostingSettings":
        ctx = "posting."
        hours = _int_list(data, "active_hours", (8, 22), ctx=ctx)
        if len(hours) != 2:
            raise ConfigError(f"{ctx}active_hours: erwartet genau zwei Werte [start, ende].")
        start, end = hours
        for value in (start, end):
            if not 0 <= value <= 23:
                raise ConfigError(f"{ctx}active_hours: Stunden muessen zwischen 0 und 23 liegen (ist {value}).")
        if start >= end:
            raise ConfigError(f"{ctx}active_hours: Startstunde ({start}) muss kleiner als Endstunde ({end}) sein.")

        weekdays = _int_list(data, "active_weekdays", (0, 1, 2, 3, 4, 5, 6), ctx=ctx)
        for day in weekdays:
            if not 0 <= day <= 6:
                raise ConfigError(f"{ctx}active_weekdays: Werte muessen zwischen 0 (Mo) und 6 (So) liegen (ist {day}).")
        if not weekdays:
            raise ConfigError(f"{ctx}active_weekdays: mindestens ein Wochentag muss aktiv sein.")

        return cls(
            enabled=_bool(data, "enabled", True, ctx=ctx),
            interval_minutes=_int(data, "interval_minutes", 240, minimum=1, ctx=ctx),
            jitter_minutes=_int(data, "jitter_minutes", 60, minimum=0, ctx=ctx),
            active_hours=(start, end),
            active_weekdays=tuple(sorted(set(weekdays))),
            max_per_day=_int(data, "max_per_day", 5, minimum=0, ctx=ctx),
            include_hashtags=_bool(data, "include_hashtags", True, ctx=ctx),
            max_hashtags=_int(data, "max_hashtags", 2, minimum=0, maximum=10, ctx=ctx),
            hashtag_pool=tuple(dict.fromkeys(normalise_hashtag(t) for t in _str_list(data, "hashtag_pool", ctx=ctx) if t)),
        )


@dataclass(frozen=True)
class Limits:
    """Obergrenzen pro Aktion. ``0`` bedeutet: Aktion vollstaendig gesperrt."""

    like_per_hour: int = 12
    like_per_day: int = 80
    repost_per_hour: int = 3
    repost_per_day: int = 15
    reply_per_hour: int = 3
    reply_per_day: int = 12
    post_per_hour: int = 2
    post_per_day: int = 5
    min_seconds_between_actions: int = 30

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> "Limits":
        ctx = "engagement.limits."
        return cls(
            like_per_hour=_int(data, "like_per_hour", 12, minimum=0, ctx=ctx),
            like_per_day=_int(data, "like_per_day", 80, minimum=0, ctx=ctx),
            repost_per_hour=_int(data, "repost_per_hour", 3, minimum=0, ctx=ctx),
            repost_per_day=_int(data, "repost_per_day", 15, minimum=0, ctx=ctx),
            reply_per_hour=_int(data, "reply_per_hour", 3, minimum=0, ctx=ctx),
            reply_per_day=_int(data, "reply_per_day", 12, minimum=0, ctx=ctx),
            post_per_hour=_int(data, "post_per_hour", 2, minimum=0, ctx=ctx),
            post_per_day=_int(data, "post_per_day", 5, minimum=0, ctx=ctx),
            min_seconds_between_actions=_int(data, "min_seconds_between_actions", 30, minimum=0, ctx=ctx),
        )

    def per_hour(self, action: str) -> int:
        return int(getattr(self, f"{action}_per_hour"))

    def per_day(self, action: str) -> int:
        return int(getattr(self, f"{action}_per_day"))


@dataclass(frozen=True)
class EngagementSettings:
    enabled: bool = True
    interval_minutes: int = 30
    jitter_minutes: int = 10
    max_results_per_query: int = 25
    lookback_minutes: int = 180
    max_actions_per_cycle: int = 5
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> "EngagementSettings":
        ctx = "engagement."
        return cls(
            enabled=_bool(data, "enabled", True, ctx=ctx),
            interval_minutes=_int(data, "interval_minutes", 30, minimum=1, ctx=ctx),
            jitter_minutes=_int(data, "jitter_minutes", 10, minimum=0, ctx=ctx),
            # Die X-API akzeptiert bei der Recent-Search 10 bis 100 Treffer.
            max_results_per_query=_int(data, "max_results_per_query", 25, minimum=10, maximum=100, ctx=ctx),
            lookback_minutes=_int(data, "lookback_minutes", 180, minimum=1, maximum=10080, ctx=ctx),
            max_actions_per_cycle=_int(data, "max_actions_per_cycle", 5, minimum=0, ctx=ctx),
            limits=Limits.parse(_section(data, "limits")),
        )


@dataclass(frozen=True)
class Rule:
    """Eine Hashtag-Regel: worauf reagiert wird und wie."""

    name: str
    hashtags: tuple[str, ...]
    actions: tuple[str, ...]
    match: str = "any"
    languages: tuple[str, ...] = ()
    min_likes: int = 0
    min_reposts: int = 0
    weight: float = 1.0
    reply_instruction: str = ""

    @classmethod
    def parse(cls, data: Mapping[str, Any], index: int) -> "Rule":
        if not isinstance(data, Mapping):
            raise ConfigError(f"rules[{index}]: erwartet ein Objekt, erhalten {type(data).__name__}.")
        name = _str(data, "name", f"Regel {index + 1}")
        ctx = f"rules[{index}] ({name}) -> "

        hashtags = tuple(dict.fromkeys(normalise_hashtag(t) for t in _str_list(data, "hashtags", ctx=ctx) if t.strip()))
        if not hashtags:
            raise ConfigError(f"{ctx}hashtags: mindestens ein Hashtag ist erforderlich.")

        actions = tuple(dict.fromkeys(a.strip().lower() for a in _str_list(data, "actions", ("like",), ctx=ctx)))
        unknown = [a for a in actions if a not in VALID_ACTIONS]
        if unknown:
            raise ConfigError(f"{ctx}actions: unbekannt {unknown}. Erlaubt sind {list(VALID_ACTIONS)}.")
        if not actions:
            raise ConfigError(f"{ctx}actions: mindestens eine Aktion ist erforderlich.")

        match = _str(data, "match", "any", ctx=ctx).lower()
        if match not in VALID_MATCH_MODES:
            raise ConfigError(f"{ctx}match: erwartet 'any' oder 'all', erhalten {match!r}.")

        return cls(
            name=name,
            hashtags=hashtags,
            actions=actions,
            match=match,
            languages=tuple(lang.lower() for lang in _str_list(data, "languages", ctx=ctx)),
            min_likes=_int(data, "min_likes", 0, minimum=0, ctx=ctx),
            min_reposts=_int(data, "min_reposts", 0, minimum=0, ctx=ctx),
            weight=_float(data, "weight", 1.0, minimum=0.0, ctx=ctx),
            reply_instruction=_str(data, "reply_instruction", "", ctx=ctx),
        )

    def matches(self, tweet_hashtags: Sequence[str]) -> bool:
        """Prueft, ob die Hashtags eines Tweets zu dieser Regel passen."""
        present = {normalise_hashtag(t) for t in tweet_hashtags}
        if self.match == "all":
            return all(tag in present for tag in self.hashtags)
        return any(tag in present for tag in self.hashtags)


@dataclass(frozen=True)
class FilterSettings:
    languages: tuple[str, ...] = ("de", "en")
    blocked_keywords: tuple[str, ...] = ()
    blocked_users: tuple[str, ...] = ()
    allowed_users: tuple[str, ...] = ()
    skip_retweets: bool = True
    skip_replies: bool = True
    skip_quotes: bool = False
    skip_sensitive: bool = True
    skip_links: bool = False
    max_hashtags_in_tweet: int = 5
    max_mentions_in_tweet: int = 3
    min_tweet_length: int = 30
    min_author_followers: int = 30
    max_author_followers: int = 0
    require_verified: bool = False

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> "FilterSettings":
        ctx = "filters."
        min_followers = _int(data, "min_author_followers", 30, minimum=0, ctx=ctx)
        max_followers = _int(data, "max_author_followers", 0, minimum=0, ctx=ctx)
        if max_followers and max_followers < min_followers:
            raise ConfigError(
                f"{ctx}max_author_followers ({max_followers}) darf nicht kleiner sein als "
                f"min_author_followers ({min_followers})."
            )
        return cls(
            languages=tuple(lang.lower() for lang in _str_list(data, "languages", ("de", "en"), ctx=ctx)),
            blocked_keywords=tuple(k.lower() for k in _str_list(data, "blocked_keywords", ctx=ctx)),
            blocked_users=tuple(u.lstrip("@").lower() for u in _str_list(data, "blocked_users", ctx=ctx)),
            allowed_users=tuple(u.lstrip("@").lower() for u in _str_list(data, "allowed_users", ctx=ctx)),
            skip_retweets=_bool(data, "skip_retweets", True, ctx=ctx),
            skip_replies=_bool(data, "skip_replies", True, ctx=ctx),
            skip_quotes=_bool(data, "skip_quotes", False, ctx=ctx),
            skip_sensitive=_bool(data, "skip_sensitive", True, ctx=ctx),
            skip_links=_bool(data, "skip_links", False, ctx=ctx),
            max_hashtags_in_tweet=_int(data, "max_hashtags_in_tweet", 5, minimum=0, ctx=ctx),
            max_mentions_in_tweet=_int(data, "max_mentions_in_tweet", 3, minimum=0, ctx=ctx),
            min_tweet_length=_int(data, "min_tweet_length", 30, minimum=0, ctx=ctx),
            min_author_followers=min_followers,
            max_author_followers=max_followers,
            require_verified=_bool(data, "require_verified", False, ctx=ctx),
        )


@dataclass(frozen=True)
class ContentSettings:
    provider: str = "auto"
    model: str = "claude-opus-5"
    effort: str = "low"
    max_chars: int = 260
    templates_file: str = "content/templates.yaml"
    history_lookback: int = 60
    similarity_threshold: float = 0.75
    max_generation_attempts: int = 3

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> "ContentSettings":
        ctx = "content."
        provider = _str(data, "provider", "auto", ctx=ctx).lower()
        if provider not in VALID_PROVIDERS:
            raise ConfigError(f"{ctx}provider: erwartet {list(VALID_PROVIDERS)}, erhalten {provider!r}.")
        effort = _str(data, "effort", "low", ctx=ctx).lower()
        if effort not in VALID_EFFORTS:
            raise ConfigError(f"{ctx}effort: erwartet {list(VALID_EFFORTS)}, erhalten {effort!r}.")
        return cls(
            provider=provider,
            model=_str(data, "model", "claude-opus-5", ctx=ctx) or "claude-opus-5",
            effort=effort,
            max_chars=_int(data, "max_chars", 260, minimum=20, maximum=TWEET_LIMIT, ctx=ctx),
            templates_file=_str(data, "templates_file", "content/templates.yaml", ctx=ctx),
            history_lookback=_int(data, "history_lookback", 60, minimum=0, ctx=ctx),
            similarity_threshold=_float(data, "similarity_threshold", 0.75, minimum=0.0, maximum=1.0, ctx=ctx),
            max_generation_attempts=_int(data, "max_generation_attempts", 3, minimum=1, maximum=10, ctx=ctx),
        )


@dataclass(frozen=True)
class StorageSettings:
    database: str = "data/xbot.db"

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> "StorageSettings":
        return cls(database=_str(data, "database", "data/xbot.db", ctx="storage.") or "data/xbot.db")


@dataclass(frozen=True)
class LoggingSettings:
    level: str = "INFO"
    file: str = "logs/xbot.log"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 3

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> "LoggingSettings":
        ctx = "logging."
        level = _str(data, "level", "INFO", ctx=ctx).upper() or "INFO"
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(f"{ctx}level: unbekannter Level {level!r}.")
        return cls(
            level=level,
            file=_str(data, "file", "logs/xbot.log", ctx=ctx),
            max_bytes=_int(data, "max_bytes", 5 * 1024 * 1024, minimum=1024, ctx=ctx),
            backup_count=_int(data, "backup_count", 3, minimum=0, ctx=ctx),
        )


@dataclass(frozen=True)
class Credentials:
    """Zugangsdaten aus der Umgebung. Werden nie geloggt oder serialisiert."""

    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""
    access_token_secret: str = ""
    bearer_token: str = ""
    anthropic_api_key: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Credentials":
        env = os.environ if env is None else env
        return cls(
            api_key=env.get("X_API_KEY", "").strip(),
            api_secret=env.get("X_API_SECRET", "").strip(),
            access_token=env.get("X_ACCESS_TOKEN", "").strip(),
            access_token_secret=env.get("X_ACCESS_TOKEN_SECRET", "").strip(),
            bearer_token=env.get("X_BEARER_TOKEN", "").strip(),
            anthropic_api_key=env.get("ANTHROPIC_API_KEY", "").strip(),
        )

    @property
    def has_write_access(self) -> bool:
        """OAuth 1.0a User Context - noetig fuer posten, liken, reposten."""
        return all((self.api_key, self.api_secret, self.access_token, self.access_token_secret))

    @property
    def has_search_access(self) -> bool:
        """Fuer die Recent-Search genuegt der App-only Bearer Token."""
        return bool(self.bearer_token) or self.has_write_access

    @property
    def has_ai(self) -> bool:
        return bool(self.anthropic_api_key)

    def missing_write_fields(self) -> list[str]:
        mapping = {
            "X_API_KEY": self.api_key,
            "X_API_SECRET": self.api_secret,
            "X_ACCESS_TOKEN": self.access_token,
            "X_ACCESS_TOKEN_SECRET": self.access_token_secret,
        }
        return [name for name, value in mapping.items() if not value]

    def __repr__(self) -> str:  # pragma: no cover - reine Schutzmassnahme
        return "Credentials(<redacted>)"


@dataclass(frozen=True)
class Config:
    bot: BotSettings
    posting: PostingSettings
    engagement: EngagementSettings
    rules: tuple[Rule, ...]
    filters: FilterSettings
    content: ContentSettings
    storage: StorageSettings
    logging: LoggingSettings
    credentials: Credentials
    source_path: Path | None = None

    @property
    def monitored_hashtags(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for rule in self.rules:
            for tag in rule.hashtags:
                seen.setdefault(tag, None)
        return tuple(seen)

    def rules_for_action(self, action: str) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if action in rule.actions)

    @classmethod
    def parse(cls, data: Mapping[str, Any], *, credentials: Credentials | None = None, source_path: Path | None = None) -> "Config":
        if not isinstance(data, Mapping):
            raise ConfigError("Die Konfigurationsdatei muss ein YAML-Objekt enthalten.")

        raw_rules = data.get("rules", []) or []
        if not isinstance(raw_rules, Sequence) or isinstance(raw_rules, str):
            raise ConfigError("rules: erwartet eine Liste von Regeln.")
        rules = tuple(Rule.parse(item, i) for i, item in enumerate(raw_rules))

        engagement = EngagementSettings.parse(_section(data, "engagement"))
        if engagement.enabled and not rules:
            raise ConfigError(
                "engagement.enabled ist true, aber es ist keine Regel definiert. "
                "Lege mindestens eine Regel unter 'rules:' an oder setze engagement.enabled auf false."
            )

        return cls(
            bot=BotSettings.parse(_section(data, "bot")),
            posting=PostingSettings.parse(_section(data, "posting")),
            engagement=engagement,
            rules=rules,
            filters=FilterSettings.parse(_section(data, "filters")),
            content=ContentSettings.parse(_section(data, "content")),
            storage=StorageSettings.parse(_section(data, "storage")),
            logging=LoggingSettings.parse(_section(data, "logging")),
            credentials=credentials or Credentials.from_env(),
            source_path=source_path,
        )


def _apply_env_overrides(config: Config, env: Mapping[str, str]) -> Config:
    """``XBOT_DRY_RUN`` sticht die YAML-Einstellung - als Not-Aus aus der Shell."""
    raw = env.get("XBOT_DRY_RUN")
    if raw is None or not raw.strip():
        return config
    lowered = raw.strip().lower()
    if lowered in ("true", "1", "yes", "on", "ja"):
        dry_run = True
    elif lowered in ("false", "0", "no", "off", "nein"):
        dry_run = False
    else:
        raise ConfigError(f"XBOT_DRY_RUN: erwartet true/false, erhalten {raw!r}.")
    if dry_run == config.bot.dry_run:
        return config
    from dataclasses import replace

    return replace(config, bot=replace(config.bot, dry_run=dry_run))


def load_config(path: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> Config:
    """Laedt ``config.yaml`` und die Zugangsdaten aus der Umgebung."""
    env = os.environ if env is None else env
    if path is None:
        path = env.get("XBOT_CONFIG") or "config.yaml"
    path = Path(path)

    if not path.exists():
        example = path.with_name("config.example.yaml")
        hint = f" Vorlage kopieren: cp {example} {path}" if example.exists() else ""
        raise ConfigError(f"Konfigurationsdatei '{path}' nicht gefunden.{hint}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"'{path}' ist kein gueltiges YAML: {exc}") from None

    config = Config.parse(raw, credentials=Credentials.from_env(env), source_path=path)
    return _apply_env_overrides(config, env)

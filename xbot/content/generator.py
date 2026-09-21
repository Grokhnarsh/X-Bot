"""Erzeugt die Texte des Bots.

Zwei Wege, eine Schnittstelle:

* **KI** - Claude schreibt den Beitrag. Liefert abwechslungsreiche, zum Anlass
  passende Texte und kann auf fremde Beitraege eingehen.
* **Vorlagen** - YAML-Bausteine aus ``content/templates.yaml``. Braucht keinen
  API-Key und ist der automatische Rueckfallweg, wenn die KI nicht erreichbar
  ist oder keinen brauchbaren Text liefert.

In beiden Faellen wird der Text gesaeubert, auf Laenge gebracht und gegen die
zuletzt veroeffentlichten Beitraege geprueft - der Bot soll sich nicht
wiederholen.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any, Sequence

from ..config import Config, DiscordRule, Rule
from ..errors import ContentError
from ..models import Tweet
from ..state import PLATFORM_DISCORD, PLATFORM_X, Store
from . import prompts
from .templates import TemplateLibrary
from .text import is_duplicate, sanitize, truncate, tweet_length

logger = logging.getLogger(__name__)

#: Reicht fuer einen kurzen Beitrag samt adaptivem Nachdenken.
MAX_TOKENS = 4000

#: Server-seitige Ausweichmodelle, falls eine Anfrage abgelehnt wird.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class GeneratedText:
    """Ein fertiger, sendefaehiger Text samt Herkunft."""

    text: str
    source: str           # "ai" oder "template"
    attempts: int = 1
    note: str = ""

    @property
    def length(self) -> int:
        return tweet_length(self.text)


class ContentGenerator:
    def __init__(
        self,
        config: Config,
        store: Store | None = None,
        *,
        ai_client: Any | None = None,
        library: TemplateLibrary | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.rng = rng or random.Random()
        self._ai_client = ai_client
        self._ai_client_ready = ai_client is not None
        self._library = library
        self._ai_disabled_reason = ""

    # -- Verfuegbarkeit -----------------------------------------------------
    @property
    def ai_configured(self) -> bool:
        provider = self.config.content.provider
        if provider == "template":
            return False
        return bool(self.config.credentials.anthropic_api_key) or self._ai_client is not None

    @property
    def library(self) -> TemplateLibrary:
        if self._library is None:
            self._library = TemplateLibrary.load(self.config.content.templates_file)
        return self._library

    def _client(self) -> Any:
        if not self._ai_client_ready:
            import anthropic  # lokal importiert, damit der Template-Weg ohne SDK laeuft

            self._ai_client = anthropic.Anthropic(api_key=self.config.credentials.anthropic_api_key)
            self._ai_client_ready = True
        return self._ai_client

    # -- Oeffentliche Schnittstelle -----------------------------------------
    def generate_post(self, *, topic: str | None = None) -> GeneratedText:
        cfg = self.config.content
        history = self._history(("post",), cfg.history_lookback)
        chosen_topic = topic or self._pick_topic()

        system = prompts.system_prompt_post(
            persona=self.config.bot.persona,
            language=self.config.bot.language,
            max_chars=cfg.max_chars,
        )

        def build_user(attempt: int) -> str:
            return prompts.user_prompt_post(topic=chosen_topic, recent=history[:8])

        result = self._try_ai(system, build_user, history, kind="post")
        if result is not None:
            return result

        return self._from_templates("posts", history, kind="post")

    def generate_reply(self, tweet: Tweet, rule: Rule | None = None) -> GeneratedText:
        cfg = self.config.content
        history = self._history(("reply",), cfg.history_lookback)

        system = prompts.system_prompt_reply(
            persona=self.config.bot.persona,
            language=self.config.bot.language,
            max_chars=cfg.max_chars,
            instruction=rule.reply_instruction if rule else "",
        )

        def build_user(attempt: int) -> str:
            return prompts.user_prompt_reply(
                tweet_text=tweet.text,
                author=tweet.author.username,
                recent=history[:5],
            )

        result = self._try_ai(system, build_user, history, kind="reply")
        if result is not None:
            return result

        if self.config.content.provider == "ai":
            raise ContentError(
                "Antwort konnte nicht erzeugt werden und content.provider ist auf 'ai' gesetzt."
            )
        # Vorlagen koennen den fremden Beitrag nicht lesen - deshalb nur ein
        # zurueckhaltender, allgemeiner Text.
        return self._from_templates("replies", history, kind="reply")

    # -- Discord ------------------------------------------------------------
    # Eigene Methoden statt eines Plattform-Schalters: Discord hat andere
    # Laengen, keine Hashtags und einen anderen Tonfall. Der Verlauf ist
    # ebenfalls getrennt - sonst haelt der Bot einen Discord-Beitrag fuer
    # eine Wiederholung seines X-Beitrags und schweigt.
    def generate_discord_post(self, *, topic: str | None = None) -> GeneratedText:
        cfg = self.config.content
        max_chars = self.config.discord.max_chars
        history = self._history(("post",), cfg.history_lookback, platform=PLATFORM_DISCORD)
        chosen_topic = topic or self._pick_topic()

        system = prompts.system_prompt_discord_post(
            persona=self.config.bot.persona,
            language=self.config.bot.language,
            max_chars=max_chars,
        )

        def build_user(attempt: int) -> str:
            return prompts.user_prompt_post(topic=chosen_topic, recent=history[:8])

        result = self._try_ai(system, build_user, history, kind="post", max_chars=max_chars)
        if result is not None:
            return result

        return self._from_templates("posts", history, kind="post", max_chars=max_chars)

    def generate_discord_reply(self, message, rule: DiscordRule | None = None) -> GeneratedText:
        cfg = self.config.content
        max_chars = self.config.discord.max_chars
        history = self._history(("reply",), cfg.history_lookback, platform=PLATFORM_DISCORD)

        system = prompts.system_prompt_discord_reply(
            persona=self.config.bot.persona,
            language=self.config.bot.language,
            max_chars=max_chars,
            instruction=rule.reply_instruction if rule else "",
        )

        def build_user(attempt: int) -> str:
            return prompts.user_prompt_discord_reply(
                message_text=message.content,
                author=message.author.label,
                recent=history[:5],
            )

        result = self._try_ai(system, build_user, history, kind="reply", max_chars=max_chars)
        if result is not None:
            return result

        if self.config.content.provider == "ai":
            raise ContentError(
                "Antwort konnte nicht erzeugt werden und content.provider ist auf 'ai' gesetzt."
            )
        # Vorlagen koennen die fremde Nachricht nicht lesen - deshalb nur ein
        # zurueckhaltender, allgemeiner Text.
        return self._from_templates("replies", history, kind="reply", max_chars=max_chars)

    # -- KI-Weg -------------------------------------------------------------
    def _try_ai(
        self,
        system: str,
        build_user: Any,
        history: Sequence[str],
        *,
        kind: str,
        max_chars: int | None = None,
    ) -> GeneratedText | None:
        if not self.ai_configured:
            if self.config.content.provider == "ai":
                raise ContentError(
                    "content.provider ist 'ai', aber ANTHROPIC_API_KEY ist nicht gesetzt."
                )
            return None
        if self._ai_disabled_reason:
            return None

        cfg = self.config.content
        for attempt in range(1, cfg.max_generation_attempts + 1):
            try:
                raw = self._call_model(system, build_user(attempt))
            except ContentError as exc:
                logger.warning("KI-Text fehlgeschlagen (%s): %s", kind, exc)
                if self.config.content.provider == "ai":
                    raise
                return None

            text = self._finalise(raw, max_chars)
            if not text:
                logger.debug("KI lieferte leeren Text (Versuch %d)", attempt)
                continue
            if is_duplicate(text, history, cfg.similarity_threshold):
                logger.debug("KI-Text zu aehnlich zu einem frueheren Beitrag (Versuch %d)", attempt)
                continue
            return GeneratedText(text=text, source="ai", attempts=attempt)

        logger.info("KI lieferte nach %d Versuchen keinen neuen Text.", cfg.max_generation_attempts)
        if self.config.content.provider == "ai":
            raise ContentError(
                f"Nach {cfg.max_generation_attempts} Versuchen kam nur Wiederholtes zurueck."
            )
        return None

    def _call_model(self, system: str, user: str) -> str:
        import anthropic

        cfg = self.config.content
        params: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            # Adaptives Nachdenken bei geringer Tiefe: kurze Texte brauchen
            # keine lange Ueberlegung, das spart Tokens.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": cfg.effort},
        }

        try:
            client = self._client()
            try:
                response = client.beta.messages.create(
                    **params, betas=[FALLBACK_BETA], fallbacks="default"
                )
            except TypeError:
                # Aeltere SDK-Versionen kennen die Ausweichmodelle noch nicht.
                logger.debug("SDK ohne 'fallbacks' - Anfrage ohne Ausweichmodell.")
                response = client.messages.create(**params)
        except anthropic.AuthenticationError as exc:
            self._ai_disabled_reason = "ANTHROPIC_API_KEY wurde abgelehnt"
            raise ContentError(f"ANTHROPIC_API_KEY wurde abgelehnt: {exc}") from None
        except anthropic.RateLimitError as exc:
            raise ContentError(f"Rate Limit der Claude-API erreicht: {exc}") from None
        except anthropic.APITimeoutError as exc:
            raise ContentError(f"Zeitueberschreitung bei der Claude-API: {exc}") from None
        except anthropic.APIConnectionError as exc:
            raise ContentError(f"Claude-API nicht erreichbar: {exc}") from None
        except anthropic.APIStatusError as exc:
            raise ContentError(f"Claude-API antwortete mit Status {exc.status_code}: {exc}") from None
        except anthropic.APIError as exc:
            raise ContentError(f"Fehler der Claude-API: {exc}") from None

        # Sicherheitsklassifikatoren koennen eine Anfrage ablehnen - das ist
        # kein Fehler, sondern eine regulaere Antwort mit HTTP 200.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ContentError(f"Die Anfrage wurde abgelehnt (Kategorie: {category or 'unbekannt'}).")

        parts = [
            block.text
            for block in (getattr(response, "content", None) or [])
            if getattr(block, "type", None) == "text" and getattr(block, "text", "")
        ]
        return "\n".join(parts).strip()

    # -- Vorlagenweg --------------------------------------------------------
    def _from_templates(
        self,
        kind_key: str,
        history: Sequence[str],
        *,
        kind: str,
        max_chars: int | None = None,
    ) -> GeneratedText:
        if self.config.content.provider == "ai":
            raise ContentError("content.provider ist 'ai' - kein Rueckfall auf Vorlagen erlaubt.")

        library = self.library
        total = library.count(kind_key)
        if total == 0:
            raise ContentError(f"Keine Vorlagen unter '{kind_key}' in {library.source}.")

        # Alle Vorlagen in zufaelliger Reihenfolge durchgehen, statt blind zu
        # ziehen - so wird der Vorrat wirklich ausgeschoepft.
        order = list(range(total))
        self.rng.shuffle(order)

        for position, index in enumerate(order, start=1):
            text = self._finalise(library.render_index(kind_key, index, rng=self.rng), max_chars)
            if not text:
                continue
            if is_duplicate(text, history, self.config.content.similarity_threshold):
                continue
            note = "Rueckfall auf Vorlagen" if self.ai_configured else ""
            return GeneratedText(text=text, source="template", attempts=position, note=note)

        raise ContentError(
            f"Alle {total} Vorlagen unter '{kind_key}' aehneln bereits veroeffentlichten "
            "Texten. Ergaenze content/templates.yaml oder senke content.similarity_threshold."
        )

    # -- Hilfsfunktionen ----------------------------------------------------
    def _finalise(self, raw: str, max_chars: int | None = None) -> str:
        return truncate(sanitize(raw), max_chars or self.config.content.max_chars)

    def _history(
        self, actions: Sequence[str], limit: int, *, platform: str = PLATFORM_X
    ) -> list[str]:
        if self.store is None or limit <= 0:
            return []
        return self.store.recent_texts(actions, limit, platform=platform)

    def _pick_topic(self) -> str:
        topics = self.config.bot.topics
        if not topics:
            return ""
        return self.rng.choice(list(topics))

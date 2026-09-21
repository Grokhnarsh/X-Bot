"""Wrapper um die Discord-REST-API (v10).

Dieselben zwei Zusagen wie beim X-Client:

* **Probelauf.** Ist ``dry_run`` aktiv, geht keine Schreibanfrage raus.
  Gelesen wird weiter, damit ein Probelauf zeigt, was der Bot taete.
* **Verstaendliche Fehler.** Die typischen Discord-Stolpersteine - fehlendes
  Message-Content-Intent, Bot nicht im Kanal, zu wenig Rechte - werden in
  Klartext uebersetzt statt als nackter HTTP-Code durchgereicht.

Bewusst ohne ``discord.py``: dessen Gateway braucht einen asynchronen
Dauerlauf. Der Bot taktet synchron, also fragt er die REST-API ab.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import requests

from ..errors import CredentialsError, XBotError
from ..models import ActionResult
from .models import MESSAGE_LIMIT, DiscordAuthor, DiscordMessage

logger = logging.getLogger(__name__)

BASE_URL = "https://discord.com/api/v10"
USER_AGENT = "XBot (https://github.com/grokhnarsh/x-bot, 1.0)"

#: Discord liefert hoechstens 100 Nachrichten pro Abruf.
MAX_MESSAGES_PER_REQUEST = 100

#: Wie lange bei einem Rate Limit hoechstens gewartet wird. Laengere Sperren
#: laesst der Bot lieber fallen - der naechste Zyklus kommt ohnehin.
MAX_RETRY_WAIT_SECONDS = 30.0

DEFAULT_TIMEOUT = 20.0

#: Fehlercodes aus der Discord-API, die eine eigene Erklaerung verdienen.
#: https://discord.com/developers/docs/topics/opcodes-and-status-codes
CODE_MISSING_ACCESS = 50001
CODE_MISSING_PERMISSIONS = 50013
CODE_UNKNOWN_MESSAGE = 10008
CODE_UNKNOWN_CHANNEL = 10003


class DiscordClientError(XBotError):
    """Die Discord-API hat die Anfrage abgelehnt."""


def _payload(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _describe(response: requests.Response, *, kontext: str = "") -> str:
    """Macht aus einer abgelehnten Antwort eine Zeile, die weiterhilft."""
    data = _payload(response)
    code = int(data.get("code", 0) or 0)
    meldung = str(data.get("message", "") or response.reason or "").strip()
    ziel = f" ({kontext})" if kontext else ""

    if response.status_code == 401:
        return (
            f"401 Nicht autorisiert{ziel} - der Bot-Token ist falsch oder wurde zurueckgesetzt. "
            "Pruefe DISCORD_BOT_TOKEN in der .env (Developer Portal > Bot > Reset Token)."
        )
    if response.status_code == 403 or code in (CODE_MISSING_ACCESS, CODE_MISSING_PERMISSIONS):
        return (
            f"403 Verboten{ziel} - {meldung or 'fehlende Rechte'}. Haeufigste Ursachen: der Bot "
            "ist dem Server nicht beigetreten, er sieht den Kanal nicht (Rechte 'View Channel' "
            "und 'Read Message History'), oder ihm fehlt 'Send Messages' bzw. 'Add Reactions'."
        )
    if response.status_code == 404 or code in (CODE_UNKNOWN_CHANNEL, CODE_UNKNOWN_MESSAGE):
        return (
            f"404 Nicht gefunden{ziel} - Kanal oder Nachricht gibt es nicht (mehr). "
            "Pruefe die Kanal-ID: im Entwicklermodus per Rechtsklick auf den Kanal kopieren."
        )
    if response.status_code == 429:
        return f"429 Rate Limit von Discord erreicht{ziel} - der naechste Zyklus versucht es erneut."
    if response.status_code >= 500:
        return f"{response.status_code} Serverfehler bei Discord{ziel} - spaeter erneut versuchen."
    return f"{response.status_code} {meldung or 'Anfrage abgelehnt'}{ziel}"


class DiscordClient:
    """Alle Schreib- und Lesezugriffe auf Discord laufen durch diese Klasse."""

    def __init__(
        self,
        token: str,
        *,
        dry_run: bool = True,
        wait_on_rate_limit: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        self.token = (token or "").strip()
        self.dry_run = dry_run
        self.wait_on_rate_limit = wait_on_rate_limit
        self.timeout = timeout
        self._session = session
        self._me: DiscordAuthor | None = None

    # -- Verbindung ---------------------------------------------------------
    @property
    def session(self) -> requests.Session:
        if self._session is None:
            if not self.token:
                raise CredentialsError(
                    "Kein DISCORD_BOT_TOKEN gefunden. Lege ihn nach dem Muster von "
                    ".env.example an (siehe 'xbot doctor')."
                )
            session = requests.Session()
            session.headers.update(
                {
                    "Authorization": f"Bot {self.token}",
                    "User-Agent": USER_AGENT,
                    "Content-Type": "application/json",
                }
            )
            self._session = session
        return self._session

    def _request(
        self,
        method: str,
        pfad: str,
        *,
        kontext: str = "",
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> requests.Response:
        """Eine Anfrage inklusive einmaligem Warten bei Rate Limit."""
        url = f"{BASE_URL}{pfad}"
        for versuch in (1, 2):
            try:
                response = self.session.request(
                    method, url, params=params, json=json, timeout=self.timeout
                )
            except requests.RequestException as exc:
                raise DiscordClientError(
                    f"Discord nicht erreichbar ({kontext or pfad}): {exc}"
                ) from exc

            if response.status_code != 429 or versuch == 2 or not self.wait_on_rate_limit:
                return response

            wartezeit = float(_payload(response).get("retry_after", 1) or 1)
            if wartezeit > MAX_RETRY_WAIT_SECONDS:
                return response
            logger.warning(
                "Discord-Rate-Limit (%s) - warte %.1fs", kontext or pfad, wartezeit
            )
            time.sleep(wartezeit)
        return response

    def verify(self) -> DiscordAuthor:
        """Prueft den Token und liefert das eigene Bot-Konto."""
        if self._me is not None:
            return self._me
        if not self.token:
            raise CredentialsError(
                "Fuer Discord fehlt DISCORD_BOT_TOKEN in der .env."
            )
        response = self._request("GET", "/users/@me", kontext="Zugangsdaten pruefen")
        if not response.ok:
            raise CredentialsError(_describe(response, kontext="Zugangsdaten pruefen"))
        self._me = DiscordAuthor.from_api(_payload(response))
        return self._me

    @property
    def me(self) -> DiscordAuthor | None:
        return self._me

    # -- Lesen --------------------------------------------------------------
    def fetch_messages(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> list[DiscordMessage]:
        """Holt die juengsten Nachrichten eines Kanals.

        Ohne ``after`` liefert Discord die neuesten zuerst; mit ``after`` nur
        das, was seit der genannten Nachricht dazugekommen ist. Beides wird
        hier auf aufsteigende Zeit sortiert, damit der Bot in der Reihenfolge
        arbeitet, in der geschrieben wurde.
        """
        channel_id = str(channel_id).strip()
        if not channel_id:
            return []

        params: dict[str, Any] = {"limit": max(1, min(int(limit), MAX_MESSAGES_PER_REQUEST))}
        if after:
            params["after"] = str(after)

        logger.debug("Lese Kanal %s (limit=%s, after=%s)", channel_id, params["limit"], after)
        response = self._request(
            "GET",
            f"/channels/{quote(channel_id)}/messages",
            kontext=f"Kanal {channel_id}",
            params=params,
        )
        if not response.ok:
            raise DiscordClientError(_describe(response, kontext=f"Kanal {channel_id}"))

        try:
            rohdaten = response.json()
        except ValueError as exc:
            raise DiscordClientError(
                f"Discord hat auf Kanal {channel_id} keine gueltige Antwort geliefert."
            ) from exc
        if not isinstance(rohdaten, list):
            return []

        nachrichten = [DiscordMessage.from_api(eintrag) for eintrag in rohdaten]
        nachrichten = [m for m in nachrichten if m.id]
        # Discord sortiert absteigend; Snowflakes wachsen mit der Zeit.
        nachrichten.sort(key=lambda m: int(m.id) if m.id.isdigit() else 0)
        return nachrichten

    # -- Schreiben ----------------------------------------------------------
    def post(
        self,
        text: str,
        channel_id: str,
        *,
        reply_to: str | None = None,
    ) -> ActionResult:
        action = "reply" if reply_to else "post"
        text = (text or "").strip()
        channel_id = str(channel_id).strip()

        if not text:
            return ActionResult(action, ok=False, error="Leerer Text - nichts zu senden.")
        if not channel_id:
            return ActionResult(action, ok=False, text=text, error="Keine Kanal-ID angegeben.")
        if len(text) > MESSAGE_LIMIT:
            return ActionResult(
                action,
                ok=False,
                target_id=reply_to,
                text=text,
                error=f"Text ist {len(text)} Zeichen lang, Discord erlaubt {MESSAGE_LIMIT}.",
            )

        if self.dry_run:
            logger.info(
                "[PROBELAUF] discord %s -> Kanal %s%s: %s",
                action,
                channel_id,
                f" (Antwort auf {reply_to})" if reply_to else "",
                text,
            )
            return ActionResult(
                action, ok=True, dry_run=True, target_id=reply_to or channel_id, text=text
            )

        nutzlast: dict[str, Any] = {"content": text}
        if reply_to:
            nutzlast["message_reference"] = {"message_id": str(reply_to)}
            # Keine Benachrichtigung an die angeschriebene Person: die Antwort
            # haengt sichtbar an ihrer Nachricht, ein Ping obendrauf waere
            # aufdringlich. Ist die Nachricht geloescht, lehnt Discord die
            # Antwort ab - genau richtig, sonst stuende sie beziehungslos da.
            nutzlast["allowed_mentions"] = {"replied_user": False}

        response = self._request(
            "POST",
            f"/channels/{quote(channel_id)}/messages",
            kontext=f"Kanal {channel_id}",
            json=nutzlast,
        )
        if not response.ok:
            meldung = _describe(response, kontext=f"Kanal {channel_id}")
            logger.error("discord %s fehlgeschlagen: %s", action, meldung)
            return ActionResult(
                action, ok=False, target_id=reply_to or channel_id, text=text, error=meldung
            )

        neue_id = str(_payload(response).get("id", ""))
        logger.info("discord %s veroeffentlicht (id=%s): %s", action, neue_id, text)
        return ActionResult(
            action,
            ok=True,
            target_id=reply_to or channel_id,
            result_id=neue_id,
            text=text,
        )

    def react(self, channel_id: str, message_id: str, emoji: str) -> ActionResult:
        """Setzt eine Reaktion. ``emoji`` ist ein Unicode-Zeichen oder
        ``name:id`` fuer ein Server-Emoji."""
        channel_id = str(channel_id).strip()
        message_id = str(message_id).strip()
        emoji = (emoji or "").strip()

        if not channel_id or not message_id:
            return ActionResult("react", ok=False, error="Kanal- oder Nachrichten-ID fehlt.")
        if not emoji:
            return ActionResult(
                "react", ok=False, target_id=message_id, error="Kein Emoji angegeben."
            )

        if self.dry_run:
            logger.info("[PROBELAUF] discord react %s -> %s/%s", emoji, channel_id, message_id)
            return ActionResult(
                "react", ok=True, dry_run=True, target_id=message_id, text=emoji
            )

        pfad = (
            f"/channels/{quote(channel_id)}/messages/{quote(message_id)}"
            f"/reactions/{quote(emoji, safe='')}/@me"
        )
        response = self._request("PUT", pfad, kontext=f"Nachricht {message_id}")
        if not response.ok:
            meldung = _describe(response, kontext=f"Nachricht {message_id}")
            logger.error("discord react auf %s fehlgeschlagen: %s", message_id, meldung)
            return ActionResult(
                "react", ok=False, target_id=message_id, text=emoji, error=meldung
            )

        logger.info("discord react %s -> %s", emoji, message_id)
        return ActionResult("react", ok=True, target_id=message_id, text=emoji)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

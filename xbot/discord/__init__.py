"""Discord als zweite Plattform.

Der Aufbau spiegelt die X-Seite: ein Client mit Probelauf, eigene Modelle,
Sicherheitsfilter und zwei Ablaeufe (posten, reagieren). Geteilt werden
Zeitgeber, Textgenerator, Limits und Datenbank - getrennt sind die Zaehler,
weil eine Reaktion in Discord nichts mit einem Like auf X zu tun hat.

Bewusst ueber die REST-API per Abruf, nicht ueber das Gateway: Discord
erlaubt ``GET /channels/{id}/messages``, und damit passt die Anbindung in
denselben synchronen Taktgeber wie alles andere. Eine Gateway-Verbindung
haette einen asynchronen Dauerlauf erzwungen.
"""

from .client import DiscordClient, DiscordClientError
from .engage import DiscordEngagementEngine, DiscordEngagementReport
from .filters import DiscordMessageFilter
from .models import DiscordAuthor, DiscordMessage
from .post import DiscordPoster, DiscordPostReport

__all__ = [
    "DiscordClient",
    "DiscordClientError",
    "DiscordAuthor",
    "DiscordMessage",
    "DiscordMessageFilter",
    "DiscordEngagementEngine",
    "DiscordEngagementReport",
    "DiscordPoster",
    "DiscordPostReport",
]

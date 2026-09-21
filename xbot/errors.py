"""Zentrale Ausnahmetypen des Bots."""


class XBotError(Exception):
    """Basisklasse fuer alle Fehler des Bots."""


class ConfigError(XBotError):
    """Die Konfiguration ist unvollstaendig oder widerspruechlich."""


class CredentialsError(XBotError):
    """Zugangsdaten fehlen oder wurden von X abgelehnt."""


class ContentError(XBotError):
    """Es konnte kein verwendbarer Text erzeugt werden."""


class QuotaExceeded(XBotError):
    """Ein Limit verbietet die Aktion zum jetzigen Zeitpunkt."""

    def __init__(self, action: str, reason: str) -> None:
        super().__init__(f"{action}: {reason}")
        self.action = action
        self.reason = reason

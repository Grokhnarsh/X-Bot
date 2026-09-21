"""Den Webserver starten.

Bevorzugt wird ``waitress`` - ein schlanker, mehrfaedig arbeitender
WSGI-Server. Fehlt er, springt der eingebaute Server von Flask ein; der ist
für die Entwicklung gedacht und sagt das auch.
"""

from __future__ import annotations

import logging
import secrets

logger = logging.getLogger(__name__)

#: Adressen, bei denen die Oberfläche über das Netz erreichbar waere.
PUBLIC_HOSTS = ("0.0.0.0", "::", "")


def warn_if_public(host: str, password_generated: bool) -> list[str]:
    """Hinweise, wenn die Oberfläche nicht nur lokal erreichbar ist."""
    notes: list[str] = []
    if host in PUBLIC_HOSTS:
        notes.append(
            "Die Oberfläche ist im Netz erreichbar. Sie kann im Namen deines Kontos "
            "auf X schreiben und verwaltet API-Schlüssel."
        )
        notes.append(
            "Stelle sicher, dass davor ein HTTPS-Proxy steht, und setze "
            "XBOT_WEB_HTTPS=true, damit das Sitzungs-Cookie nur verschluesselt uebertragen wird."
        )
        if password_generated:
            notes.append(
                "Es wurde ein Zufallspasswort erzeugt. Setze XBOT_WEB_PASSWORD dauerhaft, "
                "sonst gilt nach jedem Neustart ein anderes."
            )
    return notes


def serve(app, host: str = "127.0.0.1", port: int = 8080) -> None:
    banner(app, host, port)
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        logger.warning(
            "waitress ist nicht installiert - es laeuft der Entwicklungsserver von Flask. "
            "Für den Dauerbetrieb: pip install waitress"
        )
        app.run(host=host, port=port, threaded=True, use_reloader=False)
        return

    waitress_serve(app, host=host, port=port, threads=8, ident="X-Bot")


def banner(app, host: str, port: int) -> None:
    shown_host = "127.0.0.1" if host in PUBLIC_HOSTS else host
    lines = ["", "  X-Bot Weboberfläche", f"  http://{shown_host}:{port}", ""]

    if app.config.get("XBOT_PASSWORD_GENERATED"):
        lines += [
            "  Es war kein Passwort gesetzt. Für diesen Start gilt:",
            f"      {app.config.get('XBOT_GENERATED_PASSWORD', '')}",
            "  Dauerhaft festlegen mit XBOT_WEB_PASSWORD in der .env.",
            "",
        ]

    for note in warn_if_public(host, bool(app.config.get("XBOT_PASSWORD_GENERATED"))):
        lines.append(f"  ACHTUNG: {note}")
    if len(lines) and lines[-1] != "":
        lines.append("")

    print("\n".join(lines), flush=True)


def random_password() -> str:
    return secrets.token_urlsafe(12)

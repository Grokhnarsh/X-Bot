"""Zugangsschutz der Weboberflaeche.

Diese Oberfläche kann im Namen eines Kontos auf X schreiben und verwaltet
API-Schlüssel. Sie darf deshalb unter keinen Umstaenden offen erreichbar sein.
Der Schutz besteht aus vier Teilen:

* **Passwort.** Aus ``XBOT_WEB_PASSWORD``. Fehlt die Variable, wird beim Start
  ein zufaelliges Passwort erzeugt und auf der Konsole ausgegeben - so ist
  nie versehentlich "kein Passwort" eingestellt.
* **Sitzung.** Signiertes Cookie. Der Signaturschluessel liegt in der
  Datenbank, damit Anmeldungen einen Neustart ueberleben.
* **CSRF.** Jede schreibende Anfrage braucht das Sitzungs-Token, entweder als
  Formularfeld oder als Kopfzeile.
* **Bremse.** Nach mehreren Fehlversuchen ist die Anmeldung kurz gesperrt.

Dazu kommt: gebunden wird standardmaessig auf 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from functools import wraps
from typing import Callable

from flask import Flask, current_app, jsonify, redirect, request, session, url_for

logger = logging.getLogger(__name__)

SESSION_KEY = "xbot_auth"
CSRF_KEY = "xbot_csrf"

#: Nach so vielen Fehlversuchen innerhalb des Fensters wird gesperrt.
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW_SECONDS = 300
LOCKOUT_SECONDS = 300

#: Methoden, die den Zustand aendern und deshalb ein CSRF-Token brauchen.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    # Messbalken und Diagrammhoehen stehen als style-Attribut im Markup.
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)


@dataclass
class PasswordGate:
    """Haelt das Passwort nur als Hash - nicht im Klartext im Speicher."""

    salt: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    digest: bytes = b""

    def set_password(self, password: str) -> None:
        self.digest = hash_password(password, self.salt)

    def verify(self, password: str) -> bool:
        if not self.digest:
            return False
        return hmac.compare_digest(self.digest, hash_password(password, self.salt))


@dataclass
class LoginThrottle:
    """Einfache Bremse gegen das Durchprobieren von Passwoertern."""

    failures: dict[str, list[float]] = field(default_factory=dict)
    blocked_until: dict[str, float] = field(default_factory=dict)

    def blocked_for(self, key: str, *, now: float | None = None) -> int:
        now = now or time.time()
        until = self.blocked_until.get(key, 0.0)
        return max(0, int(until - now))

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        now = now or time.time()
        recent = [stamp for stamp in self.failures.get(key, []) if now - stamp < ATTEMPT_WINDOW_SECONDS]
        recent.append(now)
        self.failures[key] = recent
        if len(recent) >= MAX_ATTEMPTS:
            self.blocked_until[key] = now + LOCKOUT_SECONDS
            self.failures[key] = []
            logger.warning("Anmeldung für %s nach %d Fehlversuchen gesperrt.", key, MAX_ATTEMPTS)

    def reset(self, key: str) -> None:
        self.failures.pop(key, None)
        self.blocked_until.pop(key, None)


# ---------------------------------------------------------------------------
# Sitzung
# ---------------------------------------------------------------------------
def is_logged_in() -> bool:
    return bool(session.get(SESSION_KEY))


def log_in() -> None:
    session.clear()
    session[SESSION_KEY] = True
    session[CSRF_KEY] = secrets.token_urlsafe(32)
    session.permanent = True


def log_out() -> None:
    session.clear()


def csrf_token() -> str:
    token = session.get(CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_KEY] = token
    return token


def csrf_valid() -> bool:
    expected = session.get(CSRF_KEY)
    if not expected:
        return False
    provided = request.headers.get("X-CSRF-Token") or request.form.get("_csrf", "")
    if not provided and request.is_json:
        payload = request.get_json(silent=True) or {}
        provided = str(payload.get("_csrf", ""))
    return bool(provided) and hmac.compare_digest(str(expected), str(provided))


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            if request.path.startswith("/api/"):
                return jsonify({"error": "nicht angemeldet"}), 401
            return redirect(url_for("pages.login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Einbau in die App
# ---------------------------------------------------------------------------
def init_auth(app: Flask, password: str) -> None:
    gate = PasswordGate()
    gate.set_password(password)
    app.extensions["xbot_gate"] = gate
    app.extensions["xbot_throttle"] = LoginThrottle()

    @app.before_request
    def _check_csrf():
        if request.method not in UNSAFE_METHODS:
            return None
        # Die Anmeldung selbst schuetzt das Token aus der frischen Sitzung.
        if not csrf_valid():
            logger.warning("Anfrage ohne gültiges CSRF-Token: %s %s", request.method, request.path)
            if request.path.startswith("/api/"):
                return jsonify({"error": "CSRF-Token fehlt oder ist ungültig"}), 403
            return ("CSRF-Token fehlt oder ist ungültig. Lade die Seite neu.", 403)
        return None

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if is_logged_in():
            # Seiten hinter der Anmeldung nicht im Browsercache ablegen.
            response.headers.setdefault("Cache-Control", "no-store, max-age=0")
        return response

    @app.context_processor
    def _inject_csrf():
        return {"csrf_token": csrf_token}


def verify_password(password: str) -> bool:
    gate: PasswordGate = current_app.extensions["xbot_gate"]
    return gate.verify(password)


def throttle() -> LoginThrottle:
    return current_app.extensions["xbot_throttle"]


def client_key() -> str:
    """Kennung für die Anmeldebremse."""
    return request.remote_addr or "unbekannt"

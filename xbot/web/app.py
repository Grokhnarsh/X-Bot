"""Die Flask-Anwendung.

``create_app`` baut alles zusammen: Zugangsschutz, den Arbeits-Thread mit dem
Bot und die Routen. Der Arbeits-Thread laeuft, solange der Webserver laeuft -
auch wenn der Taktgeber gerade aus ist. Nur so können manuelle Befehle aus dem
Browser sofort ausgefuehrt werden, ohne dass zwei Stellen den Bot benutzen.
"""

from __future__ import annotations

import atexit
import logging
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, render_template

from ..config import load_config
from ..errors import ConfigError
from ..logging_setup import configure_logging
from ..state import Store
from .auth import init_auth
from .runner import BotRunner

logger = logging.getLogger(__name__)

PASSWORD_ENV = "XBOT_WEB_PASSWORD"
SECRET_ENV = "XBOT_WEB_SECRET"
SECRET_STATE_KEY = "web.secret_key"
SESSION_DAYS = 14


def resolve_password(explicit: str | None = None) -> tuple[str, bool]:
    """Passwort bestimmen. Ohne Vorgabe wird eines erzeugt und gemeldet.

    Es gibt bewusst keinen Modus ohne Passwort: diese Oberfläche kann im Namen
    eines Kontos schreiben.
    """
    password = (explicit or os.environ.get(PASSWORD_ENV, "")).strip()
    if password:
        return password, False
    generated = secrets.token_urlsafe(12)
    return generated, True


def _session_secret(config_path: Path) -> str:
    """Signaturschluessel der Sitzung - moeglichst dauerhaft.

    Aus der Umgebung, sonst aus der Datenbank (damit Anmeldungen einen Neustart
    ueberstehen), sonst frisch erzeugt.
    """
    from_env = os.environ.get(SECRET_ENV, "").strip()
    if from_env:
        return from_env
    try:
        config = load_config(config_path)
        with Store(config.storage.database) as store:
            stored = store.get_state(SECRET_STATE_KEY)
            if stored:
                return stored
            fresh = secrets.token_urlsafe(48)
            store.set_state(SECRET_STATE_KEY, fresh)
            return fresh
    except Exception as exc:
        logger.debug("Sitzungsschluessel nicht dauerhaft speicherbar: %s", exc)
        return secrets.token_urlsafe(48)


def create_app(
    config_path: str | Path | None = None,
    *,
    password: str | None = None,
    env_path: str | Path = ".env",
    start_runner: bool = True,
    setup_logging: bool = True,
) -> Flask:
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except ImportError:  # pragma: no cover
        pass

    config_path = Path(config_path or os.environ.get("XBOT_CONFIG") or "config.yaml")

    if setup_logging:
        try:
            configure_logging(load_config(config_path).logging)
        except ConfigError:
            logging.basicConfig(level=logging.INFO)

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )
    app.config.update(
        SECRET_KEY=_session_secret(config_path),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Nur wenn wirklich über HTTPS ausgeliefert wird - sonst kaeme das
        # Cookie bei einem lokalen Start nie an.
        SESSION_COOKIE_SECURE=os.environ.get("XBOT_WEB_HTTPS", "").lower() in ("1", "true", "yes"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        XBOT_CONFIG_PATH=config_path,
        XBOT_ENV_PATH=Path(env_path),
    )

    resolved, generated = resolve_password(password)
    app.config["XBOT_PASSWORD_GENERATED"] = generated
    app.config["XBOT_GENERATED_PASSWORD"] = resolved if generated else ""
    init_auth(app, resolved)

    runner = BotRunner(config_path)
    app.extensions["xbot_runner"] = runner
    if start_runner:
        runner.start()
        try:
            runner.restore_scheduler_state()
        except ConfigError as exc:
            logger.warning("Laufzustand nicht wiederherstellbar: %s", exc)
        atexit.register(runner.shutdown)

    from .views.api import bp as api_bp
    from .views.pages import bp as pages_bp

    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)

    @app.errorhandler(ConfigError)
    def _config_broken(exc: ConfigError):
        """Eine kaputte config.yaml soll die Oberfläche nicht unbenutzbar machen."""
        from flask import request

        if request.path.startswith("/api/"):
            from flask import jsonify

            return jsonify({"error": str(exc)}), 400
        raw = ""
        try:
            raw = Path(app.config["XBOT_CONFIG_PATH"]).read_text(encoding="utf-8")
        except OSError:
            pass
        return render_template("repair.html", error=str(exc), raw=raw, path=config_path), 500

    @app.errorhandler(404)
    def _not_found(_exc):
        return render_template("error.html", code=404, message="Diese Seite gibt es nicht."), 404

    return app

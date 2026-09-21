"""JSON-Schnittstelle für Steuerung und Abfrage.

Die Oberfläche fragt hier regelmaessig den Zustand ab und loest Aktionen aus.
Alle schreibenden Endpunkte laufen durch die CSRF-Prüfung aus ``auth.py``.
"""

from __future__ import annotations

import logging
import os

from flask import Blueprint, jsonify, request

from ...errors import ConfigError
from ..auth import login_required
from ..settings_io import ConfigFile
from ..support import config_file, env_file, runner, tail_file

logger = logging.getLogger(__name__)

bp = Blueprint("api", __name__, url_prefix="/api")

#: Aus dem Browser ausloesbare Aufträge.
RUNNABLE = {
    "post": "Beitrag jetzt veröffentlichen",
    "engage": "Jetzt auf Hashtags reagieren",
    "discord_post": "Discord-Beitrag jetzt senden",
    "discord_engage": "Jetzt auf Discord-Kanäle reagieren",
    "preview": "Textvorschläge erzeugen",
    "doctor": "Einrichtung prüfen",
    "reload": "Konfiguration neu laden",
}

DRY_RUN_ENV = "XBOT_DRY_RUN"


@bp.get("/status")
@login_required
def status():
    bot = runner()
    payload = bot.status()
    payload["snapshot"] = bot.snapshot()
    payload["tasks"] = [task.as_dict() for task in bot.recent_tasks(5)]
    return jsonify(payload)


@bp.post("/control")
@login_required
def control():
    data = request.get_json(silent=True) or {}
    command = str(data.get("command", "")).strip()
    bot = runner()

    if command == "start":
        bot.set_scheduler(True)
        return jsonify({"ok": True, "running": True, "message": "Der Bot laeuft."})

    if command == "stop":
        bot.set_scheduler(False)
        return jsonify({"ok": True, "running": False, "message": "Der Bot ist gestoppt."})

    if command == "mode":
        dry_run = bool(data.get("dry_run", True))
        try:
            _set_dry_run(config_file(), dry_run)
        except ConfigError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        bot.reload()
        return jsonify(
            {
                "ok": True,
                "dry_run": dry_run,
                "message": "Probelauf aktiv - es wird nichts gesendet."
                if dry_run
                else "Echtbetrieb aktiv - der Bot sendet jetzt wirklich.",
            }
        )

    return jsonify({"ok": False, "error": f"Unbekannter Befehl '{command}'."}), 400


def _set_dry_run(file: ConfigFile, dry_run: bool) -> None:
    """Betriebsmodus umstellen - an allen Stellen, die ihn bestimmen.

    ``XBOT_DRY_RUN`` sticht die Konfigurationsdatei. Wenn die Variable gesetzt
    ist, muss sie mitgezogen werden, sonst bliebe der Schalter wirkungslos und
    der Nutzer wuerde glauben, der Echtbetrieb sei an.
    """
    config = file.apply({"bot": {"dry_run": dry_run}})
    runner().set_config(config)

    if DRY_RUN_ENV in os.environ:
        value = "true" if dry_run else "false"
        os.environ[DRY_RUN_ENV] = value
        try:
            env_file().update({DRY_RUN_ENV: value})
        except OSError as exc:  # pragma: no cover - Schreibfehler
            logger.warning("XBOT_DRY_RUN konnte nicht in die .env geschrieben werden: %s", exc)


@bp.post("/run/<kind>")
@login_required
def run(kind: str):
    if kind not in RUNNABLE:
        return jsonify({"ok": False, "error": f"Unbekannter Auftrag '{kind}'."}), 400
    task = runner().submit(kind, RUNNABLE[kind])
    return jsonify({"ok": True, "task": task.as_dict()})


@bp.get("/task/<task_id>")
@login_required
def task(task_id: str):
    found = runner().task(task_id)
    if found is None:
        return jsonify({"error": "Auftrag unbekannt"}), 404
    return jsonify(found.as_dict())


@bp.get("/tasks")
@login_required
def tasks():
    return jsonify({"tasks": [item.as_dict() for item in runner().recent_tasks(12)]})


@bp.get("/logs")
@login_required
def logs():
    config = runner().config
    try:
        count = max(20, min(int(request.args.get("lines", 200)), 1000))
    except ValueError:
        count = 200
    lines = tail_file(config.logging.file, count) if config.logging.file else []
    return jsonify({"path": config.logging.file, "lines": lines})

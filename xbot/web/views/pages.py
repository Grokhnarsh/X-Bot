"""Die Seiten der Oberfläche."""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from ...content.templates import TemplateLibrary
from ...errors import ConfigError, XBotError
from ...quota import ACTIONS, DISCORD_ACTIONS
from ...state import PLATFORMS, Store, from_iso
from ..auth import client_key, is_logged_in, log_in, log_out, login_required, throttle, verify_password
from ..forms import parse_discord_rule_form, parse_form, parse_rule_form
from ..settings_io import CLEAR, ENV_FIELDS, _atomic_write, preserve_trailing_comment
from ..support import (
    config_file,
    current_config,
    env_file,
    format_local,
    humanize_delta,
    runner,
    tail_file,
)

logger = logging.getLogger(__name__)

bp = Blueprint("pages", __name__)

ACTION_LABELS = {
    "post": "Beitrag",
    "like": "Like",
    "repost": "Repost",
    "reply": "Antwort",
}

DISCORD_ACTION_LABELS = {
    "post": "Beitrag",
    "react": "Reaktion",
    "reply": "Antwort",
}


@bp.app_context_processor
def _globals():
    """In jeder Vorlage verfuegbar.

    Der Zustand für die Kopfzeile wird abgesichert ermittelt: eine kaputte
    Konfiguration soll die Navigation nicht mitreissen, sonst kaeme man nicht
    mehr an die Stelle, an der man sie repariert.
    """
    state = {"dry_run": True, "running": False, "ok": False}
    try:
        bot = runner()
        state = {
            "dry_run": bot.config.bot.dry_run,
            "running": bot.scheduler_running,
            "ok": True,
        }
    except Exception:  # noqa: BLE001 - Navigation darf nie scheitern
        pass

    return {
        "action_labels": ACTION_LABELS,
        "actions": ACTIONS,
        "discord_action_labels": DISCORD_ACTION_LABELS,
        "discord_actions": DISCORD_ACTIONS,
        "humanize_delta": humanize_delta,
        "logged_in": is_logged_in(),
        "nav_state": state,
    }


# ---------------------------------------------------------------------------
# Anmeldung
# ---------------------------------------------------------------------------
@bp.route("/login", methods=["GET", "POST"])
def login():
    if is_logged_in():
        return redirect(url_for("pages.dashboard"))

    error = ""
    if request.method == "POST":
        key = client_key()
        blocked = throttle().blocked_for(key)
        if blocked:
            error = f"Zu viele Fehlversuche. Erneut möglich in {blocked // 60 + 1} Minuten."
        elif verify_password(request.form.get("password", "")):
            throttle().reset(key)
            log_in()
            target = request.args.get("next", "")
            # Nur eigene Pfade, damit die Weiterleitung nicht nach aussen fuehrt.
            if target.startswith("/") and not target.startswith("//"):
                return redirect(target)
            return redirect(url_for("pages.dashboard"))
        else:
            throttle().record_failure(key)
            error = "Passwort stimmt nicht."

    return render_template(
        "login.html",
        error=error,
        generated=current_app.config.get("XBOT_PASSWORD_GENERATED", False),
    )


@bp.post("/logout")
def logout():
    log_out()
    return redirect(url_for("pages.login"))


# ---------------------------------------------------------------------------
# Übersicht
# ---------------------------------------------------------------------------
@bp.get("/")
@login_required
def dashboard():
    bot = runner()
    config = bot.config
    status = bot.status()
    snapshot = bot.snapshot()

    history = snapshot["history"]
    peak = max((day["total"] for day in history), default=0)

    return render_template(
        "dashboard.html",
        page="dashboard",
        config=config,
        status=status,
        snapshot=snapshot,
        history=history,
        peak=peak,
        tasks=[task.as_dict() for task in bot.recent_tasks(5)],
        format_local=lambda value, pattern="%d.%m. %H:%M": format_local(value, config, pattern),
    )


# ---------------------------------------------------------------------------
# Regeln
# ---------------------------------------------------------------------------
@bp.get("/rules")
@login_required
def rules():
    config = current_config()
    return render_template(
        "rules.html",
        page="rules",
        config=config,
        rules=list(enumerate(config.rules)),
        discord_rules=list(enumerate(config.discord.rules)),
    )


def _rule_list(tree, pfad: tuple[str, ...]):
    """Die Regelliste im YAML-Baum - bei Bedarf angelegt.

    ``pfad`` ist ``("rules",)`` fuer X und ``("discord", "rules")`` fuer
    Discord. Beide Seiten teilen sich dadurch Speichern und Loeschen samt
    Kommentarerhalt.
    """
    zweig = tree
    for schluessel in pfad[:-1]:
        unter = zweig.get(schluessel)
        if unter is None:
            unter = {}
            zweig[schluessel] = unter
        zweig = unter
    liste = zweig.get(pfad[-1])
    if liste is None:
        liste = []
        zweig[pfad[-1]] = liste
    return liste


def _save_rule(pfad: tuple[str, ...], rule: dict) -> str:
    index_raw = request.form.get("index", "").strip()
    file = config_file()
    tree = file.load_tree()
    existing = _rule_list(tree, pfad)

    with preserve_trailing_comment(existing):
        if index_raw == "":
            existing.append(rule)
            message = f"Regel '{rule['name']}' angelegt."
        else:
            index = int(index_raw)
            if not 0 <= index < len(existing):
                raise ConfigError("Diese Regel gibt es nicht mehr.")
            existing[index] = rule
            message = f"Regel '{rule['name']}' gespeichert."

    config = file.save_tree(tree)
    runner().set_config(config)
    runner().reload()
    return message


def _delete_rule(pfad: tuple[str, ...]) -> str:
    index = int(request.form.get("index", "-1"))
    file = config_file()
    tree = file.load_tree()
    existing = _rule_list(tree, pfad)
    if not 0 <= index < len(existing):
        raise ConfigError("Diese Regel gibt es nicht mehr.")
    name = str(existing[index].get("name", "Regel"))
    with preserve_trailing_comment(existing):
        del existing[index]
    config = file.save_tree(tree)
    runner().set_config(config)
    runner().reload()
    return f"Regel '{name}' gelöscht."


@bp.post("/rules/save")
@login_required
def rules_save():
    try:
        flash(_save_rule(("rules",), parse_rule_form(request.form)), "ok")
    except (ConfigError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("pages.rules"))


@bp.post("/rules/delete")
@login_required
def rules_delete():
    try:
        flash(_delete_rule(("rules",)), "ok")
    except (ConfigError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("pages.rules"))


@bp.post("/rules/discord/save")
@login_required
def discord_rules_save():
    try:
        flash(_save_rule(("discord", "rules"), parse_discord_rule_form(request.form)), "ok")
    except (ConfigError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("pages.rules"))


@bp.post("/rules/discord/delete")
@login_required
def discord_rules_delete():
    try:
        flash(_delete_rule(("discord", "rules")), "ok")
    except (ConfigError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("pages.rules"))


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------
@bp.get("/settings")
@login_required
def settings():
    file = config_file()
    return render_template(
        "settings.html",
        page="settings",
        config=current_config(),
        raw=file.read_text(),
        weekday_names=["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"],
    )


@bp.post("/settings/save")
@login_required
def settings_save():
    try:
        changes = parse_form(request.form)
        if not changes:
            raise ConfigError("Es wurde nichts übermittelt.")
        config = config_file().apply(changes)
        runner().set_config(config)
        runner().reload()
        flash("Einstellungen gespeichert.", "ok")
    except ConfigError as exc:
        flash(str(exc), "error")
    return redirect(url_for("pages.settings"))


@bp.post("/settings/raw")
@login_required
def settings_raw():
    try:
        config = config_file().save_text(request.form.get("raw", ""))
        runner().set_config(config)
        runner().reload()
        flash("Konfiguration gespeichert.", "ok")
    except ConfigError as exc:
        flash(str(exc), "error")
    return redirect(url_for("pages.settings"))


# ---------------------------------------------------------------------------
# Inhalte
# ---------------------------------------------------------------------------
@bp.get("/content")
@login_required
def content():
    config = current_config()
    path = config.content.templates_file
    try:
        library = TemplateLibrary.load(path)
        raw = open(path, encoding="utf-8").read()
        error = ""
    except (XBotError, OSError) as exc:
        library = None
        raw = ""
        error = str(exc)
    return render_template(
        "content.html",
        page="content",
        config=config,
        library=library,
        raw=raw,
        error=error,
        templates_path=path,
    )


@bp.post("/content/save")
@login_required
def content_save():
    config = current_config()
    path = config.content.templates_file
    text = request.form.get("raw", "")
    try:
        # Erst in eine Nebendatei schreiben und laden - eine kaputte
        # Vorlagendatei wuerde sonst den nächsten Beitrag verhindern.
        from pathlib import Path

        probe = Path(str(path) + ".probe")
        _atomic_write(probe, text)
        try:
            TemplateLibrary.load(probe)
        finally:
            probe.unlink(missing_ok=True)

        _atomic_write(Path(path), text)
        runner().reload()
        flash("Vorlagen gespeichert.", "ok")
    except (XBotError, OSError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("pages.content"))


# ---------------------------------------------------------------------------
# Aktivität
# ---------------------------------------------------------------------------
@bp.get("/activity")
@login_required
def activity():
    config = current_config()
    platform = request.args.get("platform", "").strip() or None
    if platform and platform not in PLATFORMS:
        platform = None

    action = request.args.get("action", "").strip() or None
    # Die Aktionsnamen der beiden Plattformen ueberschneiden sich nur teilweise.
    bekannte_aktionen = set(ACTIONS) | set(DISCORD_ACTIONS)
    if action and action not in bekannte_aktionen:
        action = None

    page_number = max(1, int(request.args.get("page", "1") or 1))
    per_page = 40
    include_dry = request.args.get("dry", "1") != "0"

    with Store(config.storage.database) as store:
        rows, total = store.list_actions(
            action=action,
            platform=platform,
            include_dry_run=include_dry,
            limit=per_page,
            offset=(page_number - 1) * per_page,
        )
        entries = [
            {
                "platform": row["platform"],
                "action": row["action"],
                "target_id": row["target_id"],
                "target_author": row["target_author"],
                "rule_name": row["rule_name"],
                "text": row["text"],
                "dry_run": bool(row["dry_run"]),
                "when": format_local(from_iso(row["created_at"]), config, "%d.%m.%Y %H:%M"),
            }
            for row in rows
        ]

    return render_template(
        "activity.html",
        page="activity",
        config=config,
        entries=entries,
        total=total,
        page_number=page_number,
        pages=max(1, -(-total // per_page)),
        filter_action=action or "",
        filter_platform=platform or "",
        # Filtert man nach einer Plattform, sollen nur deren Aktionen zur Wahl stehen.
        action_choices=DISCORD_ACTIONS if platform == "discord" else ACTIONS,
        include_dry=include_dry,
    )


# ---------------------------------------------------------------------------
# Einrichtung
# ---------------------------------------------------------------------------
@bp.get("/setup")
@login_required
def setup():
    return render_template(
        "setup.html",
        page="setup",
        config=current_config(),
        fields=env_file().fields(),
        status=runner().status(),
        env_path=current_app.config["XBOT_ENV_PATH"],
    )


@bp.post("/setup/credentials")
@login_required
def setup_credentials():
    values: dict[str, str] = {}
    for key, _label, _hint in ENV_FIELDS:
        if request.form.get(f"clear_{key}"):
            values[key] = CLEAR
        else:
            values[key] = request.form.get(key, "")
    try:
        changed = env_file().update(values)
        if changed:
            runner().reload()
            flash(f"Gespeichert: {', '.join(changed)}", "ok")
        else:
            flash("Keine Änderung übermittelt.", "info")
    except OSError as exc:
        flash(f"Die .env konnte nicht geschrieben werden: {exc}", "error")
    return redirect(url_for("pages.setup"))


# ---------------------------------------------------------------------------
# Protokoll
# ---------------------------------------------------------------------------
@bp.get("/logs")
@login_required
def logs():
    config = current_config()
    return render_template(
        "logs.html",
        page="logs",
        config=config,
        lines=tail_file(config.logging.file, 300) if config.logging.file else [],
        log_path=config.logging.file,
    )

"""Der Bot im Hintergrund, gesteuert aus dem Web.

Das zentrale Problem einer Weboberflaeche für diesen Bot: Flask bedient jede
Anfrage in einem eigenen Thread, während der Bot eine SQLite-Verbindung und
einen Taktgeber besitzt. SQLite-Verbindungen sind nicht threadsicher, und zwei
gleichzeitige Likes auf denselben Tweet waeren ein echter Fehler.

Die Loesung ist bewusst einfach: **genau ein Arbeits-Thread besitzt den Bot.**
Er fuehrt sowohl die geplanten Laeufe als auch die manuell ausgeloesten Befehle
aus - nacheinander, nie parallel. Webanfragen legen nur einen Auftrag in die
Warteschlange und bekommen sofort eine Auftragsnummer zurück; das Ergebnis
holt die Oberfläche per Abfrage ab. Dadurch blockiert keine Anfrage, und es
gibt keinen gemeinsam genutzten Zustand, der gesperrt werden muesste.

Lesende Zugriffe (Zähler, Protokoll) oeffnen ihre eigene, kurzlebige
Datenbankverbindung. SQLite laeuft im WAL-Modus und vertraegt beliebig viele
Leser neben einem Schreiber.
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..bot import Bot
from ..config import Config, load_config
from ..errors import XBotError
from ..quota import ACTIONS, DISCORD_ACTIONS, QuotaGuard
from ..scheduler import Job
from ..state import PLATFORM_DISCORD, PLATFORM_X, Store, from_iso, utcnow

logger = logging.getLogger(__name__)

#: Schlüssel im kv-Speicher, unter dem der Laufzustand ueberlebt.
STATE_KEY_RUNNING = "web.scheduler_running"

#: So viele erledigte Aufträge bleiben abrufbar.
TASK_HISTORY = 40

#: Wie lange der Arbeits-Thread höchstens auf einen Auftrag wartet, bevor er
#: nach faelligen Laeufen sieht.
POLL_SECONDS = 1.0


@dataclass
class Task:
    """Ein aus dem Web ausgeloester Auftrag."""

    id: str
    kind: str
    label: str
    status: str = "wartet"          # wartet | laeuft | fertig | fehler
    created_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    summary: str = ""
    detail: list[str] = field(default_factory=list)
    ok: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "ok": self.ok,
            "summary": self.summary,
            "detail": self.detail,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "done": self.status in ("fertig", "fehler"),
        }


class BotRunner:
    """Besitzt den Bot und ist die einzige Stelle, die ihn benutzt."""

    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path)
        self._queue: queue.Queue[Task | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._scheduler_on = threading.Event()

        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._task_order: list[str] = []
        self._config: Config | None = None
        self._config_stamp: int | None = None
        self._jobs_view: list[dict[str, Any]] = []
        self._last_error: str = ""
        self._started_at: datetime | None = None

        # Nur der Arbeits-Thread fasst diese beiden an.
        self._bot: Bot | None = None
        self._jobs: list[Job] = []

    # -- Lebenszyklus -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._work, name="xbot-worker", daemon=True)
        self._thread.start()
        self._started_at = utcnow()

    def shutdown(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._queue.put(None)          # Wartezeit sofort beenden
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- Konfiguration ------------------------------------------------------
    def _file_stamp(self) -> int | None:
        try:
            return self.config_path.stat().st_mtime_ns
        except OSError:
            return None

    @property
    def config(self) -> Config:
        """Die aktuelle Konfiguration.

        Der zwischengespeicherte Stand wird verworfen, sobald sich die Datei
        geaendert hat - auch wenn jemand sie neben der Oberflaeche im Editor
        bearbeitet. Dadurch zeigt die Oberflaeche nie Werte an, die so nicht
        mehr auf der Platte stehen, und eine kaputte Datei fuehrt sichtbar zur
        Reparaturseite statt stillschweigend zu veraltetem Stand.

        ``Config`` ist unveraenderlich; das Feld wird nur unter Sperre
        getauscht, deshalb ist das Lesen aus einem Webthread unbedenklich.
        """
        stamp = self._file_stamp()
        with self._lock:
            if self._config is not None and stamp == self._config_stamp:
                return self._config
        config = load_config(self.config_path)          # wirft ConfigError
        with self._lock:
            self._config = config
            self._config_stamp = stamp
        return config

    def reload(self) -> Task:
        """Konfiguration neu einlesen und den Bot neu aufbauen."""
        return self.submit("reload", "Konfiguration neu laden")

    def set_config(self, config: Config) -> None:
        """Bereits geprueftes Ergebnis einer Bearbeitung sofort uebernehmen.

        Der Arbeits-Thread baut den Bot gleich darauf neu auf; bis dahin zeigt
        die Oberfläche schon den neuen Stand statt des alten.
        """
        with self._lock:
            self._config = config
            self._config_stamp = self._file_stamp()
            self._last_error = ""

    # -- Taktgeber an/aus ---------------------------------------------------
    def set_scheduler(self, running: bool) -> None:
        if running:
            self._scheduler_on.set()
        else:
            self._scheduler_on.clear()
        # Beim Einschalten zaehlen die Abstände ab jetzt, nicht ab dem
        # Zeitpunkt des letzten Stopps.
        self.submit("reschedule", "Zeitplan neu setzen")
        try:
            with Store(self.config.storage.database) as store:
                store.set_state(STATE_KEY_RUNNING, "1" if running else "0")
        except Exception as exc:  # pragma: no cover - Speicherfehler
            logger.warning("Laufzustand nicht gespeichert: %s", exc)

    @property
    def scheduler_running(self) -> bool:
        return self._scheduler_on.is_set()

    def restore_scheduler_state(self) -> None:
        """Nach einem Neustart den vorherigen Laufzustand wiederherstellen."""
        try:
            with Store(self.config.storage.database) as store:
                if store.get_state(STATE_KEY_RUNNING) == "1":
                    self._scheduler_on.set()
                    self.submit("reschedule", "Zeitplan nach Neustart setzen")
        except Exception as exc:  # pragma: no cover
            logger.debug("Laufzustand nicht lesbar: %s", exc)

    # -- Aufträge ----------------------------------------------------------
    def submit(self, kind: str, label: str) -> Task:
        task = Task(id=uuid.uuid4().hex[:12], kind=kind, label=label)
        with self._lock:
            self._tasks[task.id] = task
            self._task_order.append(task.id)
            while len(self._task_order) > TASK_HISTORY:
                self._tasks.pop(self._task_order.pop(0), None)
        self._queue.put(task)
        return task

    def task(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def recent_tasks(self, limit: int = 8) -> list[Task]:
        with self._lock:
            ids = self._task_order[-limit:][::-1]
            return [self._tasks[i] for i in ids if i in self._tasks]

    # -- Arbeits-Thread -----------------------------------------------------
    def _work(self) -> None:
        logger.info("Arbeits-Thread gestartet.")
        self._rebuild()

        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=POLL_SECONDS)
            except queue.Empty:
                task = None

            if task is not None:
                self._run_task(task)
                continue

            if self._scheduler_on.is_set():
                self._run_due_jobs()

        if self._bot is not None:
            self._bot.close()
            self._bot = None
        logger.info("Arbeits-Thread beendet.")

    def _rebuild(self) -> None:
        """Bot verwerfen und aus der aktuellen Konfiguration neu aufbauen."""
        if self._bot is not None:
            self._bot.close()
            self._bot = None
        try:
            config = load_config(self.config_path)
            self._bot = Bot(config)
            self._jobs = self._bot.build_jobs()
            for job in self._jobs:
                if job.enabled:
                    job.schedule(utcnow())
            with self._lock:
                self._config = config
                self._config_stamp = self._file_stamp()
                self._last_error = ""
        except XBotError as exc:
            with self._lock:
                self._last_error = str(exc)
            logger.error("Konfiguration nicht ladbar: %s", exc)
        except Exception as exc:  # pragma: no cover - unerwartet
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Bot konnte nicht aufgebaut werden.")
        self._publish_jobs()

    def _publish_jobs(self) -> None:
        """Zeitplan für die Oberfläche sichtbar machen."""
        view = [
            {
                "name": job.name,
                "enabled": job.enabled,
                "interval_minutes": job.interval_minutes,
                "next_run": job.next_run.isoformat() if job.next_run else None,
                "runs": job.runs,
                "failures": job.failures,
            }
            for job in self._jobs
        ]
        with self._lock:
            self._jobs_view = view

    def _run_due_jobs(self) -> None:
        if self._bot is None:
            return
        now = utcnow()
        for job in self._jobs:
            if not job.due(now):
                continue
            logger.debug("Geplanter Lauf: %s", job.name)
            try:
                job.run()
            except Exception:
                job.failures += 1
                logger.exception("Geplanter Lauf '%s' fehlgeschlagen.", job.name)
            finally:
                job.runs += 1
                job.schedule(utcnow())
                self._publish_jobs()

    def _run_task(self, task: Task) -> None:
        handlers: dict[str, Callable[[Task], None]] = {
            "post": self._task_post,
            "engage": self._task_engage,
            "discord_post": self._task_discord_post,
            "discord_engage": self._task_discord_engage,
            "preview": self._task_preview,
            "doctor": self._task_doctor,
            "reload": self._task_reload,
            "reschedule": self._task_reschedule,
        }
        handler = handlers.get(task.kind)
        if handler is None:
            self._finish(task, ok=False, summary=f"Unbekannter Auftrag '{task.kind}'")
            return

        task.status = "laeuft"
        try:
            handler(task)
        except XBotError as exc:
            self._finish(task, ok=False, summary=str(exc))
        except Exception as exc:  # pragma: no cover - unerwartet
            logger.exception("Auftrag '%s' fehlgeschlagen.", task.kind)
            self._finish(task, ok=False, summary=f"{type(exc).__name__}: {exc}")

    # -- Einzelne Aufträge -------------------------------------------------
    def _require_bot(self) -> Bot:
        if self._bot is None:
            raise XBotError(self._last_error or "Der Bot ist nicht einsatzbereit.")
        return self._bot

    def _task_post(self, task: Task) -> None:
        report = self._require_bot().post_once(force=True)
        detail = [line for line in report.text.splitlines() if line.strip()] if report.text else []
        self._finish(
            task,
            ok=not report.failed,
            summary=report.describe(),
            detail=detail,
        )

    def _task_engage(self, task: Task) -> None:
        report = self._require_bot().engage_once()
        detail = [f"{count}x {reason}" for reason, count in report.top_skips(6)]
        detail += [f"Fehler: {error}" for error in report.errors[:3]]
        self._finish(
            task,
            ok=not (report.errors and not report.total_actions),
            summary=report.describe(),
            detail=detail,
        )

    def _task_discord_post(self, task: Task) -> None:
        bot = self._require_bot()
        if not bot.config.discord.enabled:
            self._finish(task, ok=False, summary="Discord ist abgeschaltet.")
            return
        report = bot.discord_post_once(force=True)
        detail = [line for line in report.text.splitlines() if line.strip()] if report.text else []
        self._finish(task, ok=not report.failed, summary=report.describe(), detail=detail)

    def _task_discord_engage(self, task: Task) -> None:
        bot = self._require_bot()
        if not bot.config.discord.enabled:
            self._finish(task, ok=False, summary="Discord ist abgeschaltet.")
            return
        report = bot.discord_engage_once()
        detail = [f"{count}x {reason}" for reason, count in report.top_skips(6)]
        detail += [f"Fehler: {error}" for error in report.errors[:3]]
        self._finish(
            task,
            ok=not (report.errors and not report.total_actions),
            summary=report.describe(),
            detail=detail,
        )

    def _task_preview(self, task: Task) -> None:
        bot = self._require_bot()
        texts = [f"[{item.source}, {item.length} Zeichen] {item.text}" for item in bot.preview(count=3)]
        self._finish(task, ok=True, summary=f"{len(texts)} Vorschläge erzeugt", detail=texts)

    def _task_doctor(self, task: Task) -> None:
        checks = self._require_bot().doctor()
        detail = [f"[{check.symbol}] {check.name}: {check.detail}" for check in checks]
        offen = [check for check in checks if not check.ok]
        self._finish(
            task,
            ok=not offen,
            summary="Alles bereit." if not offen else f"{len(offen)} offene(r) Punkt(e)",
            detail=detail,
        )

    def _task_reload(self, task: Task) -> None:
        self._rebuild()
        with self._lock:
            error = self._last_error
        if error:
            self._finish(task, ok=False, summary=error)
        else:
            self._finish(task, ok=True, summary="Konfiguration neu geladen.")

    def _task_reschedule(self, task: Task) -> None:
        now = utcnow()
        for job in self._jobs:
            if job.enabled:
                job.schedule(now)
        self._publish_jobs()
        self._finish(task, ok=True, summary="Zeitplan gesetzt.")

    def _finish(self, task: Task, *, ok: bool, summary: str, detail: list[str] | None = None) -> None:
        task.ok = ok
        task.status = "fertig" if ok else "fehler"
        task.summary = summary
        task.detail = detail or []
        task.finished_at = utcnow()

    # -- Lesender Zugriff für die Oberfläche ------------------------------
    def status(self) -> dict[str, Any]:
        config = self.config
        with self._lock:
            jobs = list(self._jobs_view)
            error = self._last_error
        return {
            "alive": self.alive,
            "running": self.scheduler_running,
            "dry_run": config.bot.dry_run,
            "timezone": config.bot.timezone,
            "posting_enabled": config.posting.enabled,
            "engagement_enabled": config.engagement.enabled,
            "rules": len(config.rules),
            "hashtags": len(config.monitored_hashtags),
            "ai": bool(config.credentials.anthropic_api_key) and config.content.provider != "template",
            "credentials_ok": config.credentials.has_write_access,
            "search_ok": config.credentials.has_search_access,
            "jobs": jobs,
            "error": error,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "discord": {
                "enabled": config.discord.enabled,
                "token_ok": bool(config.credentials.discord_bot_token),
                "posting_enabled": config.discord.posting.enabled,
                "engagement_enabled": config.discord.engagement.enabled,
                "rules": len(config.discord.rules),
                "keywords": len(config.discord.watched_keywords),
                "watch_channels": len(config.discord.all_watch_channels),
                "post_channels": len(config.discord.posting.channels),
            },
        }

    def snapshot(self, *, history_days: int = 14, recent: int = 12) -> dict[str, Any]:
        """Zähler, Auslastung und Protokoll - mit eigener Datenbankverbindung."""
        config = self.config
        now = utcnow()
        today_start = _local_midnight(now, config)

        def _quota(guard: QuotaGuard) -> dict[str, Any]:
            return {
                action: {
                    "used_hour": usage.used_hour,
                    "limit_hour": usage.limit_hour,
                    "used_day": usage.used_day,
                    "limit_day": usage.limit_day,
                    "remaining": usage.remaining,
                    "share_day": (usage.used_day / usage.limit_day) if usage.limit_day else 0.0,
                }
                for action, usage in guard.snapshot(now).items()
            }

        with Store(config.storage.database) as store:
            quota = _quota(
                QuotaGuard(
                    config.engagement.limits, store, config.bot.tzinfo, dry_run=config.bot.dry_run
                )
            )
            today = {
                action: store.count_actions(
                    action, today_start, platform=PLATFORM_X, include_dry_run=config.bot.dry_run
                )
                for action in ACTIONS
            }
            week = store.summary(now - timedelta(days=7), platform=PLATFORM_X)

            # Discord wird getrennt gezaehlt - eine Reaktion dort ist kein Like hier.
            discord_quota = _quota(
                QuotaGuard(
                    config.discord.engagement.limits,
                    store,
                    config.bot.tzinfo,
                    dry_run=config.bot.dry_run,
                    platform=PLATFORM_DISCORD,
                )
            )
            discord_today = {
                action: store.count_actions(
                    action,
                    today_start,
                    platform=PLATFORM_DISCORD,
                    include_dry_run=config.bot.dry_run,
                )
                for action in DISCORD_ACTIONS
            }
            discord_week = store.summary(now - timedelta(days=7), platform=PLATFORM_DISCORD)

            rows = [
                {
                    "action": row["action"],
                    "platform": row["platform"],
                    "target_id": row["target_id"],
                    "target_author": row["target_author"],
                    "rule_name": row["rule_name"],
                    "text": row["text"],
                    "dry_run": bool(row["dry_run"]),
                    "created_at": from_iso(row["created_at"]).isoformat(),
                }
                for row in store.recent_actions(recent)
            ]
            history = _daily_history(store, config, now, history_days)

        return {
            "quota": quota,
            "today": today,
            "today_total": sum(today.values()),
            "week": week,
            "discord": {
                "quota": discord_quota,
                "today": discord_today,
                "today_total": sum(discord_today.values()),
                "week": discord_week,
            },
            "recent": rows,
            "history": history,
        }


def _local_midnight(now: datetime, config: Config) -> datetime:
    from ..quota import local_midnight_utc

    return local_midnight_utc(now, config.bot.tzinfo)


def _daily_history(store: Store, config: Config, now: datetime, days: int) -> list[dict[str, Any]]:
    """Aktionen pro Tag - Grundlage des Verlaufsdiagramms.

    Eine Abfrage, danach wird in Ortszeit-Tage einsortiert. Die Tagesgrenze
    richtet sich nach der konfigurierten Zeitzone, nicht nach UTC.
    """
    tz = config.bot.tzinfo
    midnight = _local_midnight(now, config)
    window_start = midnight - timedelta(days=days - 1)

    buckets: dict[str, int] = {}
    labels: list[str] = []
    for offset in range(days - 1, -1, -1):
        day = (midnight - timedelta(days=offset)).astimezone(tz)
        key = day.strftime("%Y-%m-%d")
        buckets[key] = 0
        labels.append(key)

    for _action, moment in store.actions_since(window_start, include_dry_run=config.bot.dry_run):
        key = moment.astimezone(tz).strftime("%Y-%m-%d")
        if key in buckets:
            buckets[key] += 1

    return [
        {"date": datetime.strptime(key, "%Y-%m-%d").strftime("%d.%m."), "total": buckets[key]}
        for key in labels
    ]

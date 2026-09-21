"""Einfacher Taktgeber fuer die wiederkehrenden Aufgaben.

Bewusst ohne externe Scheduler-Bibliothek: der Bot hat zwei Aufgaben, die in
festen Abstaenden laufen sollen. Wichtig sind nur drei Eigenschaften:

* **Streuung** - jeder Lauf verschiebt sich zufaellig, damit kein exakter Takt
  entsteht, der maschinell aussieht.
* **Robustheit** - ein Fehler in einer Aufgabe beendet nicht den Bot.
* **Sauberes Beenden** - SIGINT/SIGTERM fuehren zu einem geordneten Stopp.
"""

from __future__ import annotations

import logging
import random
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Sequence

from .state import utcnow

logger = logging.getLogger(__name__)

#: Wie oft die Schleife aufwacht, um auf faellige Aufgaben und das
#: Stoppsignal zu pruefen.
TICK_SECONDS = 2.0


@dataclass
class Job:
    name: str
    interval_minutes: int
    run: Callable[[], object]
    jitter_minutes: int = 0
    enabled: bool = True
    next_run: datetime | None = None
    runs: int = 0
    failures: int = 0
    rng: random.Random = field(default_factory=random.Random)

    def schedule(self, reference: datetime) -> datetime:
        """Naechsten Lauf festlegen - Intervall plus zufaellige Streuung."""
        jitter = self.rng.uniform(-self.jitter_minutes, self.jitter_minutes) if self.jitter_minutes else 0.0
        delay = max(1.0, self.interval_minutes + jitter)
        self.next_run = reference + timedelta(minutes=delay)
        return self.next_run

    def due(self, now: datetime) -> bool:
        return self.enabled and self.next_run is not None and now >= self.next_run


class Scheduler:
    def __init__(
        self,
        jobs: Sequence[Job],
        *,
        clock: Callable[[], datetime] = utcnow,
        sleeper: Callable[[float], None] = time.sleep,
        handle_signals: bool = True,
    ) -> None:
        self.jobs = list(jobs)
        self.clock = clock
        self.sleeper = sleeper
        self._running = False
        self._handle_signals = handle_signals

    def stop(self, *_: object) -> None:
        if self._running:
            logger.info("Stoppsignal empfangen - laufender Zyklus wird noch beendet.")
        self._running = False

    def _install_signal_handlers(self) -> None:
        if not self._handle_signals:
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):  # z. B. in einem Nebenthread
                logger.debug("Signal %s konnte nicht belegt werden.", sig)

    def run(self, *, initial_run: bool = True, max_cycles: int | None = None) -> None:
        """Laeuft, bis gestoppt wird oder ``max_cycles`` Laeufe erfolgt sind."""
        self._install_signal_handlers()
        self._running = True

        now = self.clock()
        active = [job for job in self.jobs if job.enabled]
        if not active:
            logger.warning("Keine aktive Aufgabe - es gibt nichts zu tun.")
            return

        for job in self.jobs:
            if not job.enabled:
                logger.info("Aufgabe '%s' ist deaktiviert.", job.name)
                continue
            if initial_run:
                job.next_run = now
            else:
                job.schedule(now)
                logger.info("Aufgabe '%s' startet um %s UTC.", job.name, job.next_run.strftime("%H:%M:%S"))

        completed = 0
        while self._running:
            now = self.clock()
            for job in self.jobs:
                if not job.due(now):
                    continue
                logger.debug("Aufgabe '%s' laeuft.", job.name)
                try:
                    job.run()
                except Exception:  # eine kaputte Aufgabe darf den Bot nicht beenden
                    job.failures += 1
                    logger.exception("Aufgabe '%s' ist fehlgeschlagen.", job.name)
                finally:
                    job.runs += 1
                    completed += 1
                    nxt = job.schedule(self.clock())
                    logger.info("Aufgabe '%s' erneut um %s UTC.", job.name, nxt.strftime("%H:%M:%S"))

                if max_cycles is not None and completed >= max_cycles:
                    self._running = False
                    break

            if not self._running:
                break

            self.sleeper(self._time_to_sleep())

        logger.info("Taktgeber beendet.")

    def _time_to_sleep(self) -> float:
        """Bis zur naechsten Faelligkeit schlafen, aber regelmaessig aufwachen."""
        pending = [job.next_run for job in self.jobs if job.enabled and job.next_run]
        if not pending:
            return TICK_SECONDS
        seconds = (min(pending) - self.clock()).total_seconds()
        return max(0.0, min(TICK_SECONDS, seconds))

"""Limitierung der Schreibaktionen.

X erkennt automatisiertes Verhalten vor allem am Muster: zu viel, zu schnell,
zu gleichmaessig. Der Guard setzt deshalb drei Schranken durch:

* ein gleitendes Stundenlimit,
* ein Tageslimit ab lokaler Mitternacht,
* einen Mindestabstand zwischen zwei beliebigen Schreibaktionen.

Ein Limit von ``0`` sperrt die Aktion vollstaendig - das ist der bequemste Weg,
einzelne Aktionsarten abzuschalten.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Limits
from .state import Store, utcnow

ACTIONS = ("post", "like", "repost", "reply")


@dataclass(frozen=True)
class QuotaDecision:
    """Ergebnis einer Limitpruefung."""

    allowed: bool
    reason: str = ""
    retry_after_seconds: int = 0

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True)
class QuotaUsage:
    action: str
    used_hour: int
    limit_hour: int
    used_day: int
    limit_day: int

    @property
    def remaining_hour(self) -> int:
        return max(0, self.limit_hour - self.used_hour)

    @property
    def remaining_day(self) -> int:
        return max(0, self.limit_day - self.used_day)

    @property
    def remaining(self) -> int:
        return min(self.remaining_hour, self.remaining_day)


def local_midnight_utc(now: datetime, tz: ZoneInfo) -> datetime:
    """Beginn des laufenden Tages in Ortszeit, zurueckgerechnet nach UTC."""
    local = now.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


class QuotaGuard:
    def __init__(self, limits: Limits, store: Store, tz: ZoneInfo, *, dry_run: bool = False) -> None:
        self.limits = limits
        self.store = store
        self.tz = tz
        # Im Probelauf zaehlen Probelauf-Aktionen mit, damit die Simulation
        # dieselben Grenzen erreicht wie der Echtbetrieb. Live werden
        # Probelaeufe ignoriert.
        self.include_dry_run = dry_run

    # -- Abfragen -----------------------------------------------------------
    def usage(self, action: str, now: datetime | None = None) -> QuotaUsage:
        now = now or utcnow()
        limit_hour = self.limits.per_hour(action)
        limit_day = self.limits.per_day(action)
        used_hour = self.store.count_actions(
            action, now - timedelta(hours=1), include_dry_run=self.include_dry_run
        )
        used_day = self.store.count_actions(
            action, local_midnight_utc(now, self.tz), include_dry_run=self.include_dry_run
        )
        return QuotaUsage(action, used_hour, limit_hour, used_day, limit_day)

    def snapshot(self, now: datetime | None = None) -> dict[str, QuotaUsage]:
        now = now or utcnow()
        return {action: self.usage(action, now) for action in ACTIONS}

    def seconds_since_last_action(self, now: datetime | None = None) -> float | None:
        now = now or utcnow()
        last = self.store.last_action_at(include_dry_run=self.include_dry_run)
        if last is None:
            return None
        return max(0.0, (now - last).total_seconds())

    # -- Pruefung -----------------------------------------------------------
    def check(self, action: str, now: datetime | None = None) -> QuotaDecision:
        if action not in ACTIONS:
            return QuotaDecision(False, f"unbekannte Aktion '{action}'")

        now = now or utcnow()
        usage = self.usage(action, now)

        if usage.limit_hour <= 0 or usage.limit_day <= 0:
            return QuotaDecision(False, f"{action} ist per Limit 0 deaktiviert")

        if usage.used_day >= usage.limit_day:
            midnight = local_midnight_utc(now, self.tz) + timedelta(days=1)
            wait = max(1, int((midnight - now).total_seconds()))
            return QuotaDecision(
                False,
                f"Tageslimit erreicht ({usage.used_day}/{usage.limit_day})",
                retry_after_seconds=wait,
            )

        if usage.used_hour >= usage.limit_hour:
            return QuotaDecision(
                False,
                f"Stundenlimit erreicht ({usage.used_hour}/{usage.limit_hour})",
                # Konservativ: spaetestens in einer Stunde faellt das aelteste
                # Ereignis aus dem Fenster.
                retry_after_seconds=self._seconds_until_hour_slot(action, now),
            )

        gap = self.limits.min_seconds_between_actions
        if gap > 0:
            elapsed = self.seconds_since_last_action(now)
            if elapsed is not None and elapsed < gap:
                wait = int(gap - elapsed) + 1
                return QuotaDecision(
                    False,
                    f"Mindestabstand von {gap}s noch nicht erreicht (seit {int(elapsed)}s)",
                    retry_after_seconds=wait,
                )

        return QuotaDecision(True)

    def _seconds_until_hour_slot(self, action: str, now: datetime) -> int:
        """Wann faellt die aelteste Aktion aus dem Stundenfenster?"""
        window_start = now - timedelta(hours=1)
        sql = "SELECT created_at FROM actions WHERE action = ? AND created_at >= ?"
        params: list[object] = [action, window_start.strftime("%Y-%m-%dT%H:%M:%S.%fZ")]
        if not self.include_dry_run:
            sql += " AND dry_run = 0"
        sql += " ORDER BY created_at ASC LIMIT 1"
        row = self.store.conn.execute(sql, params).fetchone()
        if not row:
            return 60
        from .state import from_iso

        oldest = from_iso(str(row["created_at"]))
        return max(1, int((oldest + timedelta(hours=1) - now).total_seconds()) + 1)

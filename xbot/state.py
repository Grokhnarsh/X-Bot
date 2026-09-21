"""Persistenter Zustand des Bots (SQLite).

Der Store erfuellt drei Aufgaben:

1. **Dedupe** - jeder Beitrag wird hoechstens einmal geliked, geteilt,
   beantwortet oder mit einer Reaktion versehen.
2. **Zaehlwerk** - alle Aktionen landen in einem Log, aus dem die Limits
   berechnet werden. Es gibt bewusst keine separaten Zaehlerspalten, die aus
   dem Tritt geraten koennten.
3. **Textgedaechtnis** - bereits veroeffentlichte Texte, um Wiederholungen
   zu erkennen.

Alles davon ist nach **Plattform** getrennt. Eine Discord-Reaktion darf nicht
gegen das Like-Limit auf X zaehlen, und dieselbe Schneeflocken-ID kann auf
beiden Plattformen vorkommen. Deshalb traegt jede Zeile ihre Plattform, und
Dedupe wie Zaehlung fragen immer mit.

Probelaeufe (``dry_run``) werden mitprotokolliert, aber getrennt gezaehlt:
Im Echtbetrieb blockiert ein Probelauf keine Aktion.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

#: Kennungen der unterstuetzten Plattformen.
PLATFORM_X = "x"
PLATFORM_DISCORD = "discord"
PLATFORMS = (PLATFORM_X, PLATFORM_DISCORD)

#: Wird in der kv-Tabelle gefuehrt und steuert die Migration.
SCHEMA_VERSION = 2
SCHEMA_VERSION_KEY = "schema_version"

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT    NOT NULL DEFAULT 'x',
    action        TEXT    NOT NULL,
    target_id     TEXT,
    target_author TEXT,
    rule_name     TEXT,
    text          TEXT,
    dry_run       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_platform_action ON actions (platform, action, created_at);
CREATE INDEX IF NOT EXISTS idx_actions_target          ON actions (platform, action, target_id);
CREATE INDEX IF NOT EXISTS idx_actions_created         ON actions (created_at);

CREATE TABLE IF NOT EXISTS seen_items (
    platform   TEXT NOT NULL DEFAULT 'x',
    item_id    TEXT NOT NULL,
    author     TEXT,
    rule_name  TEXT,
    decision   TEXT,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (platform, item_id)
);
CREATE INDEX IF NOT EXISTS idx_seen_first_seen ON seen_items (first_seen);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Einheitliches, lexikographisch sortierbares UTC-Format."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def from_iso(value: str) -> datetime:
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def migrate(conn: sqlite3.Connection) -> int:
    """Bringt eine bestehende Datenbank auf den aktuellen Schemastand.

    Version 1 kannte nur X. Version 2 fuehrt die Plattform ein:

    * ``actions`` bekommt eine Spalte ``platform`` (alle Altdaten sind 'x').
    * ``seen_tweets`` wird zu ``seen_items`` mit zusammengesetztem
      Schluessel ``(platform, item_id)`` - notwendig, weil X und Discord
      beide Schneeflocken-IDs vergeben und derselbe Wert in beiden
      Namensraeumen auftreten kann.

    Gibt die Zahl der uebernommenen Altzeilen zurueck.
    """
    uebernommen = 0

    if _table_exists(conn, "actions") and not _has_column(conn, "actions", "platform"):
        conn.execute("ALTER TABLE actions ADD COLUMN platform TEXT NOT NULL DEFAULT 'x'")

    if _table_exists(conn, "seen_tweets"):
        conn.executescript(SCHEMA)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO seen_items (platform, item_id, author, rule_name, decision, first_seen)
            SELECT 'x', tweet_id, author, rule_name, decision, first_seen FROM seen_tweets
            """
        )
        uebernommen = cursor.rowcount or 0
        conn.execute("DROP TABLE seen_tweets")

    return uebernommen


class Store:
    """Duenne SQLite-Schicht. Bewusst synchron - der Bot ist nicht nebenlaeufig."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

    # -- Lebenszyklus -------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            if self.path.parent and str(self.path.parent) not in ("", "."):
                self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            # Erst wandern, dann anlegen: die Migration braucht die alten
            # Tabellen noch, bevor das neue Schema darueber laeuft.
            migrate(conn)
            conn.executescript(SCHEMA)
            conn.execute(
                """
                INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (SCHEMA_VERSION_KEY, str(SCHEMA_VERSION), to_iso(utcnow())),
            )
            conn.commit()
            self._conn = conn
        return self._conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connect()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Store":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # -- Aktionen -----------------------------------------------------------
    def record_action(
        self,
        action: str,
        *,
        platform: str = PLATFORM_X,
        target_id: str | None = None,
        target_author: str | None = None,
        rule_name: str | None = None,
        text: str | None = None,
        dry_run: bool = False,
        created_at: datetime | None = None,
    ) -> int:
        with self._write() as conn:
            cursor = conn.execute(
                """
                INSERT INTO actions
                    (platform, action, target_id, target_author, rule_name, text, dry_run, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    action,
                    target_id,
                    target_author,
                    rule_name,
                    text,
                    1 if dry_run else 0,
                    to_iso(created_at or utcnow()),
                ),
            )
            return int(cursor.lastrowid or 0)

    def has_acted(
        self,
        action: str,
        target_id: str,
        *,
        platform: str = PLATFORM_X,
        include_dry_run: bool = False,
    ) -> bool:
        """Wurde auf diesen Beitrag bereits mit dieser Aktion reagiert?"""
        sql = "SELECT 1 FROM actions WHERE platform = ? AND action = ? AND target_id = ?"
        params: list[object] = [platform, action, str(target_id)]
        if not include_dry_run:
            sql += " AND dry_run = 0"
        sql += " LIMIT 1"
        return self.conn.execute(sql, params).fetchone() is not None

    def acted_targets(
        self,
        action: str,
        target_ids: Sequence[str],
        *,
        platform: str = PLATFORM_X,
        include_dry_run: bool = False,
    ) -> set[str]:
        """Batch-Variante von :meth:`has_acted` - eine Abfrage statt N."""
        ids = [str(t) for t in target_ids if t]
        if not ids:
            return set()
        found: set[str] = set()
        # SQLite begrenzt die Zahl der Platzhalter; in Bloecken abfragen.
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            sql = (
                "SELECT DISTINCT target_id FROM actions "
                f"WHERE platform = ? AND action = ? AND target_id IN ({placeholders})"
            )
            if not include_dry_run:
                sql += " AND dry_run = 0"
            rows = self.conn.execute(sql, [platform, action, *chunk]).fetchall()
            found.update(str(row["target_id"]) for row in rows)
        return found

    def count_actions(
        self,
        action: str,
        since: datetime,
        *,
        platform: str = PLATFORM_X,
        include_dry_run: bool = False,
    ) -> int:
        sql = "SELECT COUNT(*) AS n FROM actions WHERE platform = ? AND action = ? AND created_at >= ?"
        params: list[object] = [platform, action, to_iso(since)]
        if not include_dry_run:
            sql += " AND dry_run = 0"
        row = self.conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def last_action_at(
        self,
        action: str | None = None,
        *,
        platform: str | None = PLATFORM_X,
        include_dry_run: bool = False,
    ) -> datetime | None:
        """Zeitpunkt der letzten Aktion. ``platform=None`` fragt ueber alle."""
        sql = "SELECT MAX(created_at) AS last FROM actions WHERE 1 = 1"
        params: list[object] = []
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if action is not None:
            sql += " AND action = ?"
            params.append(action)
        if not include_dry_run:
            sql += " AND dry_run = 0"
        row = self.conn.execute(sql, params).fetchone()
        if not row or not row["last"]:
            return None
        return from_iso(row["last"])

    def recent_texts(
        self,
        actions: Sequence[str] = ("post", "reply"),
        limit: int = 60,
        *,
        platform: str = PLATFORM_X,
    ) -> list[str]:
        """Zuletzt erzeugte Texte - Grundlage der Wiederholungspruefung.

        Je Plattform getrennt: X und Discord sind verschiedene Publika, dort
        darf derselbe Gedanke durchaus zweimal auftauchen.
        """
        if limit <= 0 or not actions:
            return []
        placeholders = ",".join("?" * len(actions))
        rows = self.conn.execute(
            f"""
            SELECT text FROM actions
            WHERE platform = ? AND action IN ({placeholders})
              AND text IS NOT NULL AND text != ''
            ORDER BY id DESC LIMIT ?
            """,
            [platform, *actions, limit],
        ).fetchall()
        return [str(row["text"]) for row in rows]

    # -- Gesehene Beitraege -------------------------------------------------
    def mark_seen(
        self,
        item_id: str,
        *,
        platform: str = PLATFORM_X,
        author: str | None = None,
        rule_name: str | None = None,
        decision: str | None = None,
        first_seen: datetime | None = None,
    ) -> None:
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO seen_items (platform, item_id, author, rule_name, decision, first_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, item_id) DO UPDATE SET
                    decision  = excluded.decision,
                    rule_name = COALESCE(excluded.rule_name, seen_items.rule_name),
                    author    = COALESCE(excluded.author, seen_items.author)
                """,
                (platform, str(item_id), author, rule_name, decision, to_iso(first_seen or utcnow())),
            )

    def is_seen(self, item_id: str, *, platform: str = PLATFORM_X) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_items WHERE platform = ? AND item_id = ? LIMIT 1",
            (platform, str(item_id)),
        ).fetchone()
        return row is not None

    def seen_ids(self, item_ids: Sequence[str], *, platform: str = PLATFORM_X) -> set[str]:
        ids = [str(t) for t in item_ids if t]
        if not ids:
            return set()
        found: set[str] = set()
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT item_id FROM seen_items WHERE platform = ? AND item_id IN ({placeholders})",
                [platform, *chunk],
            ).fetchall()
            found.update(str(row["item_id"]) for row in rows)
        return found

    # -- Schluessel/Wert ----------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, to_iso(utcnow())),
            )

    # -- Auswertung ---------------------------------------------------------
    def summary(
        self, since: datetime | None = None, *, platform: str | None = None
    ) -> dict[str, dict[str, int]]:
        """Aktionen gruppiert nach Typ, getrennt nach echt und Probelauf.

        ``platform=None`` fasst alle Plattformen zusammen.
        """
        sql = "SELECT action, dry_run, COUNT(*) AS n FROM actions WHERE 1 = 1"
        params: list[object] = []
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(to_iso(since))
        sql += " GROUP BY action, dry_run"
        result: dict[str, dict[str, int]] = {}
        for row in self.conn.execute(sql, params).fetchall():
            bucket = result.setdefault(str(row["action"]), {"live": 0, "dry_run": 0})
            bucket["dry_run" if row["dry_run"] else "live"] += int(row["n"])
        return result

    def recent_actions(self, limit: int = 20, *, platform: str | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT platform, action, target_id, target_author, rule_name, text, dry_run, created_at "
            "FROM actions WHERE 1 = 1"
        )
        params: list[object] = []
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        # Nach Zeitstempel, nicht nach Einfuegereihenfolge: nachtraeglich
        # eingespielte Eintraege sollen an der richtigen Stelle stehen.
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, limit))
        return list(self.conn.execute(sql, params).fetchall())

    def prune(self, older_than: datetime) -> int:
        """Alte Eintraege loeschen, damit die Datei nicht unbegrenzt waechst."""
        cutoff = to_iso(older_than)
        with self._write() as conn:
            deleted = conn.execute("DELETE FROM seen_items WHERE first_seen < ?", (cutoff,)).rowcount
            deleted += conn.execute("DELETE FROM actions WHERE created_at < ?", (cutoff,)).rowcount
        return int(deleted)

    def actions_since(
        self,
        since: datetime,
        *,
        platform: str | None = None,
        include_dry_run: bool = False,
    ) -> list[tuple[str, datetime]]:
        """Aktionsart und Zeitpunkt aller Eintraege ab ``since``.

        Eine einzige Abfrage als Grundlage fuer Verlaufsdarstellungen - das
        Einsortieren in Tagesscheiben passiert danach in Python, weil SQLite
        keine Zeitzonen kennt.
        """
        sql = "SELECT action, created_at FROM actions WHERE created_at >= ?"
        params: list[object] = [to_iso(since)]
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if not include_dry_run:
            sql += " AND dry_run = 0"
        sql += " ORDER BY created_at ASC"
        return [(str(row["action"]), from_iso(str(row["created_at"]))) for row in self.conn.execute(sql, params)]

    def list_actions(
        self,
        *,
        action: str | None = None,
        platform: str | None = None,
        include_dry_run: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[sqlite3.Row], int]:
        """Seitenweises Protokoll samt Gesamtzahl - fuer die Weboberflaeche."""
        where = "WHERE 1 = 1"
        params: list[object] = []
        if platform:
            where += " AND platform = ?"
            params.append(platform)
        if action:
            where += " AND action = ?"
            params.append(action)
        if not include_dry_run:
            where += " AND dry_run = 0"

        total_row = self.conn.execute(f"SELECT COUNT(*) AS n FROM actions {where}", params).fetchone()
        total = int(total_row["n"]) if total_row else 0

        rows = self.conn.execute(
            f"SELECT platform, action, target_id, target_author, rule_name, text, dry_run, created_at "
            f"FROM actions {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, max(1, limit), max(0, offset)],
        ).fetchall()
        return list(rows), total

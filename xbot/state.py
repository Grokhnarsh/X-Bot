"""Persistenter Zustand des Bots (SQLite).

Der Store erfuellt drei Aufgaben:

1. **Dedupe** - jeder Tweet wird hoechstens einmal geliked, geteilt, beantwortet.
2. **Zaehlwerk** - alle Aktionen landen in einem Log, aus dem die Limits
   berechnet werden. Es gibt bewusst keine separaten Zaehlerspalten, die aus
   dem Tritt geraten koennten.
3. **Textgedaechtnis** - bereits veroeffentlichte Texte, um Wiederholungen
   zu erkennen.

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

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    action        TEXT    NOT NULL,
    target_id     TEXT,
    target_author TEXT,
    rule_name     TEXT,
    text          TEXT,
    dry_run       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_action_created ON actions (action, created_at);
CREATE INDEX IF NOT EXISTS idx_actions_target        ON actions (action, target_id);
CREATE INDEX IF NOT EXISTS idx_actions_created       ON actions (created_at);

CREATE TABLE IF NOT EXISTS seen_tweets (
    tweet_id   TEXT PRIMARY KEY,
    author     TEXT,
    rule_name  TEXT,
    decision   TEXT,
    first_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_first_seen ON seen_tweets (first_seen);

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
            conn.executescript(SCHEMA)
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
                INSERT INTO actions (action, target_id, target_author, rule_name, text, dry_run, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
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

    def has_acted(self, action: str, target_id: str, *, include_dry_run: bool = False) -> bool:
        """Wurde auf diesen Tweet bereits mit dieser Aktion reagiert?"""
        sql = "SELECT 1 FROM actions WHERE action = ? AND target_id = ?"
        params: list[object] = [action, str(target_id)]
        if not include_dry_run:
            sql += " AND dry_run = 0"
        sql += " LIMIT 1"
        return self.conn.execute(sql, params).fetchone() is not None

    def acted_targets(self, action: str, target_ids: Sequence[str], *, include_dry_run: bool = False) -> set[str]:
        """Batch-Variante von :meth:`has_acted` - eine Abfrage statt N."""
        ids = [str(t) for t in target_ids if t]
        if not ids:
            return set()
        found: set[str] = set()
        # SQLite begrenzt die Zahl der Platzhalter; in Bloecken abfragen.
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            sql = f"SELECT DISTINCT target_id FROM actions WHERE action = ? AND target_id IN ({placeholders})"
            if not include_dry_run:
                sql += " AND dry_run = 0"
            rows = self.conn.execute(sql, [action, *chunk]).fetchall()
            found.update(str(row["target_id"]) for row in rows)
        return found

    def count_actions(self, action: str, since: datetime, *, include_dry_run: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM actions WHERE action = ? AND created_at >= ?"
        params: list[object] = [action, to_iso(since)]
        if not include_dry_run:
            sql += " AND dry_run = 0"
        row = self.conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def last_action_at(self, action: str | None = None, *, include_dry_run: bool = False) -> datetime | None:
        sql = "SELECT MAX(created_at) AS last FROM actions WHERE 1 = 1"
        params: list[object] = []
        if action is not None:
            sql += " AND action = ?"
            params.append(action)
        if not include_dry_run:
            sql += " AND dry_run = 0"
        row = self.conn.execute(sql, params).fetchone()
        if not row or not row["last"]:
            return None
        return from_iso(row["last"])

    def recent_texts(self, actions: Sequence[str] = ("post", "reply"), limit: int = 60) -> list[str]:
        """Zuletzt erzeugte Texte - Grundlage der Wiederholungspruefung."""
        if limit <= 0 or not actions:
            return []
        placeholders = ",".join("?" * len(actions))
        rows = self.conn.execute(
            f"""
            SELECT text FROM actions
            WHERE action IN ({placeholders}) AND text IS NOT NULL AND text != ''
            ORDER BY id DESC LIMIT ?
            """,
            [*actions, limit],
        ).fetchall()
        return [str(row["text"]) for row in rows]

    # -- Gesehene Tweets ----------------------------------------------------
    def mark_seen(
        self,
        tweet_id: str,
        *,
        author: str | None = None,
        rule_name: str | None = None,
        decision: str | None = None,
        first_seen: datetime | None = None,
    ) -> None:
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO seen_tweets (tweet_id, author, rule_name, decision, first_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tweet_id) DO UPDATE SET
                    decision  = excluded.decision,
                    rule_name = COALESCE(excluded.rule_name, seen_tweets.rule_name),
                    author    = COALESCE(excluded.author, seen_tweets.author)
                """,
                (str(tweet_id), author, rule_name, decision, to_iso(first_seen or utcnow())),
            )

    def is_seen(self, tweet_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM seen_tweets WHERE tweet_id = ? LIMIT 1", (str(tweet_id),)).fetchone()
        return row is not None

    def seen_ids(self, tweet_ids: Sequence[str]) -> set[str]:
        ids = [str(t) for t in tweet_ids if t]
        if not ids:
            return set()
        found: set[str] = set()
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT tweet_id FROM seen_tweets WHERE tweet_id IN ({placeholders})", chunk
            ).fetchall()
            found.update(str(row["tweet_id"]) for row in rows)
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
    def summary(self, since: datetime | None = None) -> dict[str, dict[str, int]]:
        """Aktionen gruppiert nach Typ, getrennt nach echt und Probelauf."""
        sql = "SELECT action, dry_run, COUNT(*) AS n FROM actions"
        params: list[object] = []
        if since is not None:
            sql += " WHERE created_at >= ?"
            params.append(to_iso(since))
        sql += " GROUP BY action, dry_run"
        result: dict[str, dict[str, int]] = {}
        for row in self.conn.execute(sql, params).fetchall():
            bucket = result.setdefault(str(row["action"]), {"live": 0, "dry_run": 0})
            bucket["dry_run" if row["dry_run"] else "live"] += int(row["n"])
        return result

    def recent_actions(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT action, target_id, target_author, rule_name, text, dry_run, created_at "
                # Nach Zeitstempel, nicht nach Einfuegereihenfolge: nachtraeglich
                # eingespielte Eintraege sollen an der richtigen Stelle stehen.
                "FROM actions ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        )

    def prune(self, older_than: datetime) -> int:
        """Alte Eintraege loeschen, damit die Datei nicht unbegrenzt waechst."""
        cutoff = to_iso(older_than)
        with self._write() as conn:
            deleted = conn.execute("DELETE FROM seen_tweets WHERE first_seen < ?", (cutoff,)).rowcount
            deleted += conn.execute("DELETE FROM actions WHERE created_at < ?", (cutoff,)).rowcount
        return int(deleted)

    def actions_since(self, since: datetime, *, include_dry_run: bool = False) -> list[tuple[str, datetime]]:
        """Aktionsart und Zeitpunkt aller Eintraege ab ``since``.

        Eine einzige Abfrage als Grundlage fuer Verlaufsdarstellungen - das
        Einsortieren in Tagesscheiben passiert danach in Python, weil SQLite
        keine Zeitzonen kennt.
        """
        sql = "SELECT action, created_at FROM actions WHERE created_at >= ?"
        params: list[object] = [to_iso(since)]
        if not include_dry_run:
            sql += " AND dry_run = 0"
        sql += " ORDER BY created_at ASC"
        return [(str(row["action"]), from_iso(str(row["created_at"]))) for row in self.conn.execute(sql, params)]

    def list_actions(
        self,
        *,
        action: str | None = None,
        include_dry_run: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[sqlite3.Row], int]:
        """Seitenweises Protokoll samt Gesamtzahl - fuer die Weboberflaeche."""
        where = "WHERE 1 = 1"
        params: list[object] = []
        if action:
            where += " AND action = ?"
            params.append(action)
        if not include_dry_run:
            where += " AND dry_run = 0"

        total_row = self.conn.execute(f"SELECT COUNT(*) AS n FROM actions {where}", params).fetchone()
        total = int(total_row["n"]) if total_row else 0

        rows = self.conn.execute(
            f"SELECT action, target_id, target_author, rule_name, text, dry_run, created_at "
            f"FROM actions {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, max(1, limit), max(0, offset)],
        ).fetchall()
        return list(rows), total

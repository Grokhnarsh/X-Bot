"""Gedaechtnis und Grenzwerte - der Schutz vor Doppelaktionen und Spam."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from xbot.config import Limits
from xbot.quota import DISCORD_ACTIONS, QuotaGuard, X_ACTIONS, local_midnight_utc
from xbot.state import (
    PLATFORM_DISCORD,
    PLATFORM_X,
    SCHEMA_VERSION,
    SCHEMA_VERSION_KEY,
    Store,
    from_iso,
    to_iso,
    utcnow,
)

BERLIN = ZoneInfo("Europe/Berlin")


class TestStore:
    def test_legt_verzeichnis_an(self, tmp_path):
        store = Store(tmp_path / "tief" / "verschachtelt" / "x.db")
        store.connect()
        assert (tmp_path / "tief" / "verschachtelt" / "x.db").exists()
        store.close()

    def test_dedupe_ignoriert_probelaeufe_im_echtbetrieb(self, store):
        store.record_action("like", target_id="1", dry_run=True)
        assert store.has_acted("like", "1") is False
        assert store.has_acted("like", "1", include_dry_run=True) is True

    def test_dedupe_greift_bei_echten_aktionen(self, store):
        store.record_action("like", target_id="1")
        assert store.has_acted("like", "1") is True
        assert store.has_acted("repost", "1") is False

    def test_stapelabfrage(self, store):
        for i in range(5):
            store.record_action("like", target_id=str(i))
        assert store.acted_targets("like", ["1", "3", "99"]) == {"1", "3"}
        assert store.acted_targets("like", []) == set()

    def test_stapelabfrage_ueber_platzhalter_grenze(self, store):
        ids = [str(i) for i in range(900)]
        for tweet_id in ids:
            store.record_action("like", target_id=tweet_id)
        assert len(store.acted_targets("like", ids)) == 900

    def test_zaehlt_im_zeitfenster(self, store):
        jetzt = utcnow()
        store.record_action("like", target_id="alt", created_at=jetzt - timedelta(hours=3))
        store.record_action("like", target_id="neu", created_at=jetzt - timedelta(minutes=5))
        assert store.count_actions("like", jetzt - timedelta(hours=1)) == 1
        assert store.count_actions("like", jetzt - timedelta(hours=5)) == 2

    def test_gesehene_tweets(self, store):
        store.mark_seen("1", decision="gefiltert: zu kurz")
        store.mark_seen("2", decision="bearbeitet")
        assert store.is_seen("1") is True
        assert store.seen_ids(["1", "2", "3"]) == {"1", "2"}

    def test_gesehen_aktualisiert_statt_zu_scheitern(self, store):
        store.mark_seen("1", author="a", decision="erst")
        store.mark_seen("1", decision="dann")
        row = store.conn.execute(
            "SELECT author, decision FROM seen_items WHERE platform = 'x' AND item_id = '1'"
        ).fetchone()
        assert row["decision"] == "dann"
        assert row["author"] == "a"  # bleibt erhalten

    def test_textverlauf(self, store):
        store.record_action("post", text="erster")
        store.record_action("like", target_id="1", text=None)
        store.record_action("reply", target_id="2", text="zweiter")
        assert store.recent_texts() == ["zweiter", "erster"]
        assert store.recent_texts(("post",)) == ["erster"]
        assert store.recent_texts(limit=0) == []

    def test_schluessel_wert(self, store):
        assert store.get_state("fehlt", "standard") == "standard"
        store.set_state("k", "v1")
        store.set_state("k", "v2")
        assert store.get_state("k") == "v2"

    def test_zusammenfassung(self, store):
        store.record_action("like", target_id="1")
        store.record_action("like", target_id="2", dry_run=True)
        assert store.summary()["like"] == {"live": 1, "dry_run": 1}

    def test_aufraeumen(self, store):
        alt = utcnow() - timedelta(days=100)
        store.record_action("like", target_id="1", created_at=alt)
        store.mark_seen("1", first_seen=alt)
        store.record_action("like", target_id="2")
        assert store.prune(utcnow() - timedelta(days=30)) == 2
        assert store.has_acted("like", "2") is True

    def test_zeitstempel_roundtrip(self):
        jetzt = utcnow()
        assert abs((from_iso(to_iso(jetzt)) - jetzt).total_seconds()) < 0.001

    def test_zeitstempel_sind_sortierbar(self):
        frueh = to_iso(datetime(2026, 1, 1, 9, tzinfo=timezone.utc))
        spaet = to_iso(datetime(2026, 1, 1, 10, tzinfo=timezone.utc))
        assert frueh < spaet


class TestQuota:
    def test_frischer_start_erlaubt(self, store):
        guard = QuotaGuard(Limits(), store, BERLIN)
        assert guard.check("like").allowed is True

    def test_stundenlimit(self, store):
        guard = QuotaGuard(Limits(like_per_hour=2, like_per_day=99, min_seconds_between_actions=0), store, BERLIN)
        jetzt = utcnow()
        for i in range(2):
            store.record_action("like", target_id=str(i), created_at=jetzt - timedelta(minutes=10))
        entscheidung = guard.check("like", jetzt)
        assert entscheidung.allowed is False
        assert "Stundenlimit" in entscheidung.reason
        assert entscheidung.retry_after_seconds > 0

    def test_altes_faellt_aus_dem_stundenfenster(self, store):
        guard = QuotaGuard(Limits(like_per_hour=1, like_per_day=99, min_seconds_between_actions=0), store, BERLIN)
        jetzt = utcnow()
        store.record_action("like", target_id="alt", created_at=jetzt - timedelta(hours=2))
        assert guard.check("like", jetzt).allowed is True

    def test_tageslimit(self, store):
        guard = QuotaGuard(Limits(like_per_hour=99, like_per_day=2, min_seconds_between_actions=0), store, BERLIN)
        jetzt = utcnow()
        for i in range(2):
            store.record_action("like", target_id=str(i), created_at=jetzt - timedelta(hours=5))
        entscheidung = guard.check("like", jetzt)
        assert entscheidung.allowed is False
        assert "Tageslimit" in entscheidung.reason

    def test_limit_null_sperrt(self, store):
        guard = QuotaGuard(Limits(repost_per_hour=0), store, BERLIN)
        assert "deaktiviert" in guard.check("repost").reason

    def test_mindestabstand(self, store):
        guard = QuotaGuard(Limits(min_seconds_between_actions=60), store, BERLIN)
        jetzt = utcnow()
        store.record_action("post", created_at=jetzt - timedelta(seconds=10))
        entscheidung = guard.check("like", jetzt)
        assert entscheidung.allowed is False
        assert "Mindestabstand" in entscheidung.reason
        assert 45 <= entscheidung.retry_after_seconds <= 60

    def test_probelauf_zaehlt_nur_im_probelauf(self, store):
        jetzt = utcnow()
        for i in range(3):
            store.record_action("like", target_id=str(i), dry_run=True, created_at=jetzt - timedelta(minutes=5))
        limits = Limits(like_per_hour=2, min_seconds_between_actions=0)
        assert QuotaGuard(limits, store, BERLIN, dry_run=True).check("like", jetzt).allowed is False
        assert QuotaGuard(limits, store, BERLIN, dry_run=False).check("like", jetzt).allowed is True

    def test_unbekannte_aktion(self, store):
        assert QuotaGuard(Limits(), store, BERLIN).check("tanzen").allowed is False

    def test_auslastung(self, store):
        guard = QuotaGuard(Limits(like_per_hour=10, like_per_day=20, min_seconds_between_actions=0), store, BERLIN)
        store.record_action("like", target_id="1")
        nutzung = guard.usage("like")
        assert (nutzung.used_hour, nutzung.remaining_hour, nutzung.remaining) == (1, 9, 9)

    def test_tagesgrenze_folgt_der_zeitzone(self):
        # 00:30 Berliner Zeit ist noch 22:30 UTC am Vortag.
        moment = datetime(2026, 6, 15, 22, 30, tzinfo=timezone.utc)
        mitternacht = local_midnight_utc(moment, BERLIN)
        assert mitternacht == datetime(2026, 6, 15, 22, 0, tzinfo=timezone.utc)

    @pytest.mark.parametrize("aktion", ["post", "like", "repost", "reply"])
    def test_alle_aktionen_haben_limits(self, store, aktion):
        assert QuotaGuard(Limits(), store, BERLIN).usage(aktion).limit_day > 0


# ---------------------------------------------------------------------------
# Plattformtrennung und Migration
# ---------------------------------------------------------------------------
ALTES_SCHEMA = """
CREATE TABLE actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, target_id TEXT,
    target_author TEXT, rule_name TEXT, text TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE seen_tweets (
    tweet_id TEXT PRIMARY KEY, author TEXT, rule_name TEXT, decision TEXT, first_seen TEXT NOT NULL);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


@pytest.fixture
def alte_datenbank(tmp_path):
    """Eine Datenbank im Schema der Fassung ohne Discord."""
    import sqlite3

    pfad = tmp_path / "alt.db"
    conn = sqlite3.connect(pfad)
    conn.executescript(ALTES_SCHEMA)
    conn.execute(
        "INSERT INTO actions (action, target_id, target_author, text, dry_run, created_at) "
        "VALUES ('like', '111', 'alice', NULL, 0, '2026-09-20T10:00:00.000000Z')"
    )
    conn.execute(
        "INSERT INTO actions (action, target_id, text, dry_run, created_at) "
        "VALUES ('post', NULL, 'Ein alter Beitrag', 0, '2026-09-20T11:00:00.000000Z')"
    )
    conn.execute(
        "INSERT INTO seen_tweets VALUES ('999', 'bob', 'Regel A', 'gefiltert', '2026-09-20T09:00:00.000000Z')"
    )
    conn.commit()
    conn.close()
    return pfad


class TestMigration:
    def test_altdaten_wandern_verlustfrei(self, alte_datenbank):
        store = Store(alte_datenbank)
        store.connect()
        try:
            # Altbestand bleibt erhalten und gilt als X.
            assert store.has_acted("like", "111") is True
            assert store.recent_texts() == ["Ein alter Beitrag"]
            assert store.is_seen("999") is True
            zeilen = store.conn.execute("SELECT platform FROM actions").fetchall()
            assert {row["platform"] for row in zeilen} == {"x"}
        finally:
            store.close()

    def test_alte_tabelle_verschwindet(self, alte_datenbank):
        store = Store(alte_datenbank)
        store.connect()
        try:
            namen = {
                row["name"]
                for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert "seen_items" in namen
            assert "seen_tweets" not in namen
        finally:
            store.close()

    def test_schemaversion_wird_vermerkt(self, alte_datenbank):
        store = Store(alte_datenbank)
        store.connect()
        try:
            assert store.get_state(SCHEMA_VERSION_KEY) == str(SCHEMA_VERSION)
        finally:
            store.close()

    def test_erneutes_oeffnen_ist_unschaedlich(self, alte_datenbank):
        for _ in range(3):
            store = Store(alte_datenbank)
            store.connect()
            anzahl = store.conn.execute("SELECT COUNT(*) AS n FROM seen_items").fetchone()["n"]
            store.close()
        assert anzahl == 1

    def test_neue_datenbank_braucht_keine_migration(self, tmp_path):
        store = Store(tmp_path / "neu.db")
        store.connect()
        try:
            assert store.get_state(SCHEMA_VERSION_KEY) == str(SCHEMA_VERSION)
            store.record_action("react", platform=PLATFORM_DISCORD, target_id="1")
            assert store.has_acted("react", "1", platform=PLATFORM_DISCORD) is True
        finally:
            store.close()


class TestPlattformtrennung:
    def test_gleiche_id_auf_beiden_plattformen(self, store):
        """X und Discord vergeben beide Schneeflocken - Kollision muss moeglich sein."""
        store.record_action("reply", platform=PLATFORM_X, target_id="777")
        assert store.has_acted("reply", "777", platform=PLATFORM_X) is True
        assert store.has_acted("reply", "777", platform=PLATFORM_DISCORD) is False

        store.record_action("reply", platform=PLATFORM_DISCORD, target_id="777")
        assert store.has_acted("reply", "777", platform=PLATFORM_DISCORD) is True

    def test_gesehene_beitraege_getrennt(self, store):
        store.mark_seen("777", platform=PLATFORM_X, decision="gefiltert")
        assert store.is_seen("777", platform=PLATFORM_X) is True
        assert store.is_seen("777", platform=PLATFORM_DISCORD) is False
        store.mark_seen("777", platform=PLATFORM_DISCORD, decision="bearbeitet")
        zeilen = store.conn.execute("SELECT platform, decision FROM seen_items ORDER BY platform").fetchall()
        assert [(r["platform"], r["decision"]) for r in zeilen] == [
            ("discord", "bearbeitet"),
            ("x", "gefiltert"),
        ]

    def test_zaehler_sind_getrennt(self, store):
        jetzt = utcnow()
        fenster = jetzt - timedelta(hours=1)
        store.record_action("like", platform=PLATFORM_X, target_id="1")
        for i in range(3):
            store.record_action("react", platform=PLATFORM_DISCORD, target_id=f"d{i}")
        assert store.count_actions("like", fenster, platform=PLATFORM_X) == 1
        assert store.count_actions("react", fenster, platform=PLATFORM_DISCORD) == 3
        assert store.count_actions("react", fenster, platform=PLATFORM_X) == 0

    def test_textgedaechtnis_getrennt(self, store):
        store.record_action("post", platform=PLATFORM_X, text="Nur auf X")
        store.record_action("post", platform=PLATFORM_DISCORD, text="Nur in Discord")
        assert store.recent_texts(platform=PLATFORM_X) == ["Nur auf X"]
        assert store.recent_texts(platform=PLATFORM_DISCORD) == ["Nur in Discord"]

    def test_stapelabfrage_respektiert_die_plattform(self, store):
        for i in range(3):
            store.record_action("like", platform=PLATFORM_X, target_id=str(i))
            store.record_action("react", platform=PLATFORM_DISCORD, target_id=str(i))
        assert store.acted_targets("like", ["0", "1", "2"], platform=PLATFORM_X) == {"0", "1", "2"}
        assert store.acted_targets("like", ["0", "1", "2"], platform=PLATFORM_DISCORD) == set()

    def test_auswertung_ueber_alle_plattformen(self, store):
        store.record_action("like", platform=PLATFORM_X, target_id="1")
        store.record_action("react", platform=PLATFORM_DISCORD, target_id="2")
        assert set(store.summary()) == {"like", "react"}
        assert set(store.summary(platform=PLATFORM_X)) == {"like"}

    def test_protokoll_filtert_nach_plattform(self, store):
        store.record_action("like", platform=PLATFORM_X, target_id="1")
        store.record_action("react", platform=PLATFORM_DISCORD, target_id="2")
        alle, gesamt = store.list_actions()
        assert gesamt == 2
        nur_discord, anzahl = store.list_actions(platform=PLATFORM_DISCORD)
        assert anzahl == 1 and nur_discord[0]["action"] == "react"


class TestQuotaProPlattform:
    def test_aktionssatz_je_plattform(self, store):
        x = QuotaGuard(Limits(), store, BERLIN, platform=PLATFORM_X)
        d = QuotaGuard(Limits(), store, BERLIN, platform=PLATFORM_DISCORD)
        assert x.actions == X_ACTIONS
        assert d.actions == DISCORD_ACTIONS
        assert "unbekannte Aktion" in d.check("repost").reason
        assert "unbekannte Aktion" in x.check("react").reason

    def test_limits_bremsen_nur_die_eigene_plattform(self, store):
        limits = Limits(like_per_hour=1, min_seconds_between_actions=0)
        x = QuotaGuard(limits, store, BERLIN, platform=PLATFORM_X)
        d = QuotaGuard(limits, store, BERLIN, platform=PLATFORM_DISCORD)
        store.record_action("like", platform=PLATFORM_X, target_id="1")
        assert x.check("like").allowed is False
        assert d.check("post").allowed is True

    def test_mindestabstand_gilt_je_plattform(self, store):
        limits = Limits(min_seconds_between_actions=60)
        jetzt = utcnow()
        store.record_action("like", platform=PLATFORM_X, target_id="1", created_at=jetzt)
        x = QuotaGuard(limits, store, BERLIN, platform=PLATFORM_X)
        d = QuotaGuard(limits, store, BERLIN, platform=PLATFORM_DISCORD)
        assert "Mindestabstand" in x.check("post", jetzt).reason
        assert d.check("post", jetzt).allowed is True

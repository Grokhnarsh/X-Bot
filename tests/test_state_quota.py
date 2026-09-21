"""Gedaechtnis und Grenzwerte - der Schutz vor Doppelaktionen und Spam."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from xbot.config import Limits
from xbot.quota import QuotaGuard, local_midnight_utc
from xbot.state import Store, from_iso, to_iso, utcnow

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
        row = store.conn.execute("SELECT author, decision FROM seen_tweets WHERE tweet_id='1'").fetchone()
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

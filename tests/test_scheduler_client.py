"""Taktgeber, API-Wrapper und Kommandozeile."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
import tweepy

from xbot.client import XClient, build_search_query
from xbot.config import Credentials
from xbot.errors import CredentialsError
from xbot.models import Author, Tweet
from xbot.scheduler import Job, Scheduler

START = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


class Uhr:
    """Simulierte Zeit - Tests sollen nicht wirklich warten."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=max(seconds, 1))


# ---------------------------------------------------------------------------
class TestScheduler:
    def test_haelt_die_intervalle_ein(self):
        uhr = Uhr()
        ausgeloest: list[str] = []
        jobs = [
            Job("langsam", interval_minutes=5, jitter_minutes=0, run=lambda: ausgeloest.append("langsam")),
            Job("schnell", interval_minutes=2, jitter_minutes=0, run=lambda: ausgeloest.append("schnell")),
        ]
        Scheduler(jobs, clock=uhr, sleeper=uhr.sleep, handle_signals=False).run(max_cycles=6)
        assert ausgeloest.count("schnell") > ausgeloest.count("langsam")

    def test_startet_sofort(self):
        uhr = Uhr()
        ausgeloest: list[datetime] = []
        job = Job("x", interval_minutes=60, jitter_minutes=0, run=lambda: ausgeloest.append(uhr.now))
        Scheduler([job], clock=uhr, sleeper=uhr.sleep, handle_signals=False).run(max_cycles=1)
        assert ausgeloest == [START]

    def test_kann_den_sofortstart_auslassen(self):
        uhr = Uhr()
        ausgeloest: list[datetime] = []
        job = Job("x", interval_minutes=1, jitter_minutes=0, run=lambda: ausgeloest.append(uhr.now))
        Scheduler([job], clock=uhr, sleeper=uhr.sleep, handle_signals=False).run(
            initial_run=False, max_cycles=1
        )
        assert ausgeloest and ausgeloest[0] > START

    def test_ein_fehler_beendet_den_bot_nicht(self):
        uhr = Uhr()
        gesund: list[int] = []

        def kaputt():
            raise RuntimeError("Absturz")

        jobs = [
            Job("kaputt", interval_minutes=1, jitter_minutes=0, run=kaputt),
            Job("heil", interval_minutes=1, jitter_minutes=0, run=lambda: gesund.append(1)),
        ]
        Scheduler(jobs, clock=uhr, sleeper=uhr.sleep, handle_signals=False).run(max_cycles=4)
        assert jobs[0].failures >= 1
        assert len(gesund) >= 1

    def test_abgeschaltete_aufgaben_laufen_nie(self):
        uhr = Uhr()
        ausgeloest: list[int] = []
        jobs = [
            Job("aus", interval_minutes=1, run=lambda: ausgeloest.append(1), enabled=False),
            Job("an", interval_minutes=1, jitter_minutes=0, run=lambda: None),
        ]
        Scheduler(jobs, clock=uhr, sleeper=uhr.sleep, handle_signals=False).run(max_cycles=2)
        assert ausgeloest == []

    def test_ohne_aktive_aufgabe_kehrt_zurueck(self):
        job = Job("aus", interval_minutes=1, run=lambda: None, enabled=False)
        Scheduler([job], clock=Uhr(), sleeper=lambda _: None, handle_signals=False).run(max_cycles=1)

    def test_streuung_bleibt_im_rahmen(self):
        job = Job("x", interval_minutes=30, jitter_minutes=10, run=lambda: None, rng=random.Random(4))
        for _ in range(20):
            minuten = (job.schedule(START) - START).total_seconds() / 60
            assert 20 <= minuten <= 40

    def test_stopp_beendet_die_schleife(self):
        uhr = Uhr()
        scheduler = Scheduler(
            [Job("x", interval_minutes=1, jitter_minutes=0, run=lambda: scheduler.stop())],
            clock=uhr,
            sleeper=uhr.sleep,
            handle_signals=False,
        )
        scheduler.run(max_cycles=10)


# ---------------------------------------------------------------------------
class TestSuchanfrageBauen:
    def test_einzelner_hashtag(self):
        assert build_search_query(["#python"]) == "#python -is:retweet -is:reply"

    def test_mehrere_mit_sprachen(self):
        query = build_search_query(["#a", "#b"], languages=["de", "en"])
        assert "(#a OR #b)" in query and "(lang:de OR lang:en)" in query

    def test_ergaenzt_das_rautezeichen(self):
        assert build_search_query(["python"]).startswith("#python")

    def test_ohne_hashtags(self):
        assert build_search_query([]) == ""


class TestXClient:
    def test_probelauf_sendet_nichts(self):
        client = XClient(Credentials(), dry_run=True)
        assert client.post("Text").dry_run is True
        assert client.like("1").dry_run is True
        assert client.repost("1").dry_run is True
        assert client.post("Antwort", in_reply_to="9").action == "reply"

    def test_leerer_text_wird_abgelehnt(self):
        ergebnis = XClient(Credentials(), dry_run=True).post("   ")
        assert ergebnis.ok is False and "Leerer Text" in ergebnis.error

    def test_ohne_zugangsdaten_klare_meldung(self):
        with pytest.raises(CredentialsError, match="Keine X-Zugangsdaten"):
            _ = XClient(Credentials(), dry_run=False).client

    def test_verify_nennt_fehlende_felder(self):
        client = XClient(Credentials(bearer_token="nur-bearer"), dry_run=True)
        with pytest.raises(CredentialsError, match="X_API_KEY"):
            client.verify()

    def test_fehlermeldungen_sind_verstaendlich(self):
        from xbot.client import _describe

        class FakeResponse:
            status_code = 403
            reason = "Forbidden"
            text = ""

            def json(self):
                return {}

        assert "Schluessel oder Token" in _describe(tweepy.Unauthorized(FakeResponse()))
        assert "Read and write" in _describe(tweepy.Forbidden(FakeResponse()))
        assert "Rate Limit" in _describe(tweepy.TooManyRequests(FakeResponse()))
        assert "geloescht" in _describe(tweepy.NotFound(FakeResponse()))


class TestModelle:
    def test_zerlegt_eine_api_antwort(self):
        roh = {
            "id": "1",
            "text": "Text mit #Python und @jemand",
            "author_id": "42",
            "lang": "de",
            "public_metrics": {"like_count": 7, "retweet_count": 2, "reply_count": 1, "quote_count": 0},
            "entities": {
                "hashtags": [{"tag": "Python"}],
                "mentions": [{"username": "jemand"}],
                "urls": [{"expanded_url": "https://example.com"}],
            },
            "referenced_tweets": [{"type": "quoted", "id": "9"}],
            "created_at": "2026-09-21T07:00:00.000Z",
        }
        users = {"42": Author(id="42", username="alice", followers=1200)}
        tweet = Tweet.from_api(roh, users)
        assert tweet.hashtags == ("#python",)
        assert tweet.mentions == ("jemand",)
        assert tweet.is_quote is True and tweet.is_retweet is False
        assert tweet.author.username == "alice"
        assert tweet.url == "https://x.com/alice/status/1"

    def test_erkennt_retweets_ohne_referenz(self):
        assert Tweet.from_api({"id": "1", "text": "RT @x: hallo"}).is_retweet is True

    def test_vertraegt_luecken(self):
        tweet = Tweet.from_api({"id": "1", "text": "nackt"})
        assert tweet.lang == "" and tweet.hashtags == () and tweet.author.followers == 0

    def test_vorschau_kuerzt(self):
        tweet = Tweet(id="1", text="wort " * 50)
        assert len(tweet.preview) <= 90


# ---------------------------------------------------------------------------
class TestCLI:
    def test_globale_flags_vor_und_nach_dem_befehl(self):
        from xbot.cli import build_parser

        parser = build_parser()
        for argv in (["-q", "doctor"], ["doctor", "-q"]):
            assert parser.parse_args(argv).quiet is True
        for argv in (["--live", "post"], ["post", "--live"]):
            assert parser.parse_args(argv).live is True
        for argv in (["-c", "x.yaml", "stats"], ["stats", "-c", "x.yaml"]):
            assert parser.parse_args(argv).config == "x.yaml"

    def test_init_legt_dateien_an(self, tmp_path, monkeypatch, capsys):
        from xbot.cli import main

        monkeypatch.chdir(tmp_path)
        assert main(["init"]) == 0
        assert (tmp_path / "config.yaml").exists()
        assert (tmp_path / ".env").exists()
        # Zweiter Aufruf ueberschreibt nichts.
        (tmp_path / "config.yaml").write_text("markiert", encoding="utf-8")
        assert main(["init"]) == 0
        assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == "markiert"

    def test_fehlende_konfiguration_meldet_sauber(self, tmp_path, monkeypatch, capsys):
        from xbot.cli import main

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("XBOT_CONFIG", raising=False)
        assert main(["doctor"]) == 2
        assert "Konfigurationsfehler" in capsys.readouterr().err

    def test_live_und_dry_run_zusammen_ist_ein_fehler(self, tmp_path, monkeypatch, capsys):
        from xbot.cli import main

        monkeypatch.chdir(tmp_path)
        main(["init"])
        assert main(["--live", "--dry-run", "doctor"]) == 2
        assert "schliessen sich" in capsys.readouterr().err

    def test_doctor_und_preview_laufen_durch(self, tmp_path, monkeypatch, capsys):
        import shutil
        from pathlib import Path

        from xbot.cli import main

        root = Path(__file__).resolve().parent.parent
        monkeypatch.chdir(tmp_path)
        main(["init"])
        (tmp_path / "content").mkdir()
        shutil.copy(root / "content" / "templates.yaml", tmp_path / "content" / "templates.yaml")

        assert main(["-q", "doctor"]) in (0, 1)   # ohne Zugangsdaten sind offene Punkte normal
        assert "Pruefung der Einrichtung" in capsys.readouterr().out

        assert main(["-q", "preview", "-n", "2"]) == 0
        ausgabe = capsys.readouterr().out
        assert "[template" in ausgabe

        assert main(["-q", "stats"]) == 0
        assert "Aktionen der letzten" in capsys.readouterr().out


class TestSuche:
    """Der Suchpfad baut Parameter und verknuepft Tweets mit ihren Autoren."""

    class FakeResponse:
        def __init__(self, data, includes=None):
            self.data = data
            self.includes = includes or {}
            self.errors = None
            self.meta = {}

    class FakeTweepy:
        def __init__(self, response=None, raises=None):
            self.response = response
            self.raises = raises
            self.params: dict = {}

        def search_recent_tweets(self, **params):
            self.params = params
            if self.raises:
                raise self.raises
            return self.response

    def _client(self, fake, *, bearer="bearer-token"):
        client = XClient(Credentials(bearer_token=bearer), dry_run=True)
        client._client = fake
        return client

    def test_verknuepft_tweets_mit_autoren(self):
        antwort = self.FakeResponse(
            data=[{"id": "1", "text": "Hallo #python", "author_id": "42",
                   "entities": {"hashtags": [{"tag": "python"}]}}],
            includes={"users": [{"id": "42", "username": "alice",
                                 "public_metrics": {"followers_count": 500}}]},
        )
        fake = self.FakeTweepy(antwort)
        tweets = self._client(fake).search("#python")
        assert len(tweets) == 1
        assert tweets[0].author.username == "alice"
        assert tweets[0].author.followers == 500

    def test_setzt_die_noetigen_felder(self):
        fake = self.FakeTweepy(self.FakeResponse(data=[]))
        self._client(fake).search("#python", max_results=30, lookback_minutes=60)
        assert fake.params["max_results"] == 30
        assert "public_metrics" in fake.params["tweet_fields"]
        assert "entities" in fake.params["tweet_fields"]
        assert fake.params["expansions"] == ["author_id"]
        assert "start_time" in fake.params
        assert fake.params["user_auth"] is False   # Bearer Token vorhanden

    def test_ohne_bearer_ueber_nutzerkontext(self):
        fake = self.FakeTweepy(self.FakeResponse(data=[]))
        client = XClient(
            Credentials(api_key="a", api_secret="b", access_token="c", access_token_secret="d"),
            dry_run=True,
        )
        client._client = fake
        client.search("#python")
        assert fake.params["user_auth"] is True

    def test_begrenzt_die_trefferzahl_auf_das_erlaubte(self):
        fake = self.FakeTweepy(self.FakeResponse(data=[]))
        self._client(fake).search("#a", max_results=999)
        assert fake.params["max_results"] == 100
        self._client(fake).search("#a", max_results=1)
        assert fake.params["max_results"] == 10

    def test_since_id_ersetzt_den_startzeitpunkt(self):
        fake = self.FakeTweepy(self.FakeResponse(data=[]))
        self._client(fake).search("#a", since_id="123")
        assert fake.params["since_id"] == "123"
        assert "start_time" not in fake.params

    def test_leere_anfrage_spart_den_api_aufruf(self):
        fake = self.FakeTweepy(self.FakeResponse(data=[]))
        assert self._client(fake).search("  ") == []
        assert fake.params == {}

    def test_leere_antwort(self):
        assert self._client(self.FakeTweepy(self.FakeResponse(data=None))).search("#a") == []

    def test_403_erklaert_die_zugriffsstufe(self):
        from xbot.client import XClientError

        class FakeHTTP:
            status_code = 403
            reason = "Forbidden"
            text = ""

            def json(self):
                return {}

        fake = self.FakeTweepy(raises=tweepy.Forbidden(FakeHTTP()))
        with pytest.raises(XClientError, match="Basic"):
            self._client(fake).search("#a")

    def test_lookback_wird_auf_sieben_tage_begrenzt(self):
        from xbot.client import MAX_LOOKBACK_MINUTES

        fake = self.FakeTweepy(self.FakeResponse(data=[]))
        self._client(fake).search("#a", lookback_minutes=99_999)
        alter = datetime.now(timezone.utc) - fake.params["start_time"]
        assert alter <= timedelta(minutes=MAX_LOOKBACK_MINUTES + 1)


class TestBotFassade:
    def test_doctor_meldet_fehlende_zugangsdaten(self, config):
        from xbot.bot import Bot

        with Bot(config) as bot:
            checks = {c.name: c for c in bot.doctor()}
        assert checks["X-Zugangsdaten"].ok is False
        assert checks["Vorlagen"].ok is True
        assert checks["Regeln"].ok is True
        assert "PROBELAUF" in checks["Betriebsmodus"].detail

    def test_doctor_warnt_im_echtbetrieb(self, config):
        from dataclasses import replace

        from xbot.bot import Bot

        live = replace(config, bot=replace(config.bot, dry_run=False))
        with Bot(live) as bot:
            betrieb = next(c for c in bot.doctor() if c.name == "Betriebsmodus")
        assert betrieb.warning is True and "ECHTBETRIEB" in betrieb.detail

    def test_vorschau_speichert_nichts(self, config):
        from xbot.bot import Bot

        with Bot(config) as bot:
            texte = list(bot.preview(count=2))
            assert len(texte) == 2
            assert bot.store.summary() == {}

    def test_statistik(self, config):
        from xbot.bot import Bot

        with Bot(config) as bot:
            bot.store.record_action("like", target_id="1")
            daten = bot.stats(days=7)
        assert daten["summary"]["like"]["live"] == 1
        assert "like" in daten["quota"]

    def test_aufgaben_folgen_der_konfiguration(self, config):
        from dataclasses import replace

        from xbot.bot import Bot

        aus = replace(config, posting=replace(config.posting, enabled=False))
        with Bot(aus) as bot:
            jobs = {job.name: job for job in bot.build_jobs()}
        assert jobs["Beitrag veroeffentlichen"].enabled is False
        assert jobs["Hashtags beobachten"].enabled is True

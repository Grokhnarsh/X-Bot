"""Die beiden Ablaeufe: eigene Beitraege und Reaktionen auf Hashtags."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from xbot.actions import EngagementEngine, Poster
from xbot.actions.engage import build_queries
from xbot.content import ContentGenerator
from xbot.errors import XBotError
from xbot.models import Author
from xbot.quota import QuotaGuard
from xbot.state import Store

from .conftest import FakeXClient, make_tweet

BERLIN = ZoneInfo("Europe/Berlin")


def build_engine(config, store: Store, client: FakeXClient, *, sleeper=lambda _: None) -> EngagementEngine:
    quota = QuotaGuard(config.engagement.limits, store, config.bot.tzinfo, dry_run=client.dry_run)
    generator = ContentGenerator(config, store)
    return EngagementEngine(config, client, store, quota, generator, sleeper=sleeper)


def build_poster(config, store: Store, client: FakeXClient) -> Poster:
    quota = QuotaGuard(config.engagement.limits, store, config.bot.tzinfo, dry_run=client.dry_run)
    return Poster(config, client, store, quota, ContentGenerator(config, store))


# ---------------------------------------------------------------------------
# Suchanfragen
# ---------------------------------------------------------------------------
class TestSuchanfragen:
    def test_buendelt_in_eine_anfrage(self):
        queries = build_queries(["#a", "#b", "#c"], languages=["de"], own_handle="bot")
        assert len(queries) == 1
        assert "(#a OR #b OR #c)" in queries[0]
        assert "-from:bot" in queries[0] and "lang:de" in queries[0]

    def test_teilt_zu_lange_anfragen(self):
        viele = [f"#thema{i:02d}" for i in range(60)]
        queries = build_queries(viele, languages=["de", "en"])
        assert len(queries) > 1
        assert all(len(q) <= 450 for q in queries)

    def test_ohne_hashtags(self):
        assert build_queries([]) == []

    def test_operatoren_abschaltbar(self):
        query = build_queries(["#a"], exclude_retweets=False, exclude_replies=False)[0]
        assert "-is:retweet" not in query and "-is:reply" not in query


# ---------------------------------------------------------------------------
# Engagement
# ---------------------------------------------------------------------------
class TestEngagement:
    def test_fuehrt_die_regelaktionen_aus(self, fast_config, store):
        client = FakeXClient([make_tweet("1", hashtags=("#ki",), like_count=20)])
        report = build_engine(fast_config, store, client).run_cycle()
        assert report.actions["like"] == 1
        assert report.actions["repost"] == 1
        assert ("like", "1") in client.calls

    def test_antwortet_wenn_die_regel_es_vorsieht(self, fast_config, store):
        client = FakeXClient([make_tweet("1", hashtags=("#python",), like_count=5)])
        report = build_engine(fast_config, store, client).run_cycle()
        assert report.actions["reply"] == 1
        aktion = next(c for c in client.calls if c[0] == "reply")
        assert aktion[1] == "1" and aktion[2]

    def test_haelt_das_aktionsbudget_ein(self, fast_config, store):
        eng = replace(fast_config, engagement=replace(fast_config.engagement, max_actions_per_cycle=1))
        tweets = [make_tweet(str(i), hashtags=("#ki",), like_count=20) for i in range(1, 6)]
        report = build_engine(eng, store, FakeXClient(tweets)).run_cycle()
        assert report.total_actions == 1

    def test_hoechstens_ein_autor_pro_durchlauf(self, fast_config, store):
        autor = Author(id="a", username="vielposter", followers=900)
        tweets = [make_tweet(str(i), hashtags=("#ki",), like_count=20, author=autor) for i in range(1, 4)]
        report = build_engine(fast_config, store, FakeXClient(tweets)).run_cycle()
        assert report.skipped["schon eine Aktion fuer diesen Autor in diesem Durchlauf"] == 2

    def test_reagiert_nicht_zweimal_auf_denselben_tweet(self, fast_config, store):
        client = FakeXClient([make_tweet("1", hashtags=("#ki",), like_count=20)])
        engine = build_engine(fast_config, store, client)
        engine.run_cycle()
        vorher = len(client.calls)
        zweiter = engine.run_cycle()
        assert zweiter.total_actions == 0
        assert len(client.calls) == vorher

    def test_gefilterte_tweets_loesen_keine_aktion_aus(self, fast_config, store):
        tweets = [
            make_tweet("1", hashtags=("#ki",), like_count=20, is_retweet=True),
            make_tweet("2", hashtags=("#ki",), like_count=20, text="Casino Bonus jetzt sichern, schnell sein!"),
            make_tweet("3", hashtags=("#ki",), like_count=20, lang="fr"),
        ]
        report = build_engine(fast_config, store, FakeXClient(tweets)).run_cycle()
        assert report.total_actions == 0
        assert report.candidates == 0

    def test_eigene_beitraege_werden_uebersprungen(self, fast_config, store):
        eigen = make_tweet("1", hashtags=("#ki",), like_count=20,
                           author=Author(id="s", username="meinbot", followers=100))
        report = build_engine(fast_config, store, FakeXClient([eigen])).run_cycle()
        assert report.total_actions == 0

    def test_bestes_zuerst(self, fast_config, store):
        eng = replace(fast_config, engagement=replace(fast_config.engagement, max_actions_per_cycle=1))
        tweets = [
            make_tweet("schwach", hashtags=("#devops",), like_count=99),   # Gewicht 0.5
            make_tweet("stark", hashtags=("#ki",), like_count=20),         # Gewicht 1.0
        ]
        client = FakeXClient(tweets)
        build_engine(eng, store, client).run_cycle()
        assert client.calls[0] == ("like", "stark")

    def test_limit_stoppt_die_aktion(self, fast_config, store):
        eng = replace(
            fast_config,
            engagement=replace(
                fast_config.engagement,
                limits=replace(fast_config.engagement.limits, like_per_hour=1, min_seconds_between_actions=0),
            ),
        )
        tweets = [make_tweet(str(i), hashtags=("#devops",), like_count=20) for i in range(1, 4)]
        report = build_engine(eng, store, FakeXClient(tweets)).run_cycle()
        assert report.actions["like"] == 1
        assert any("Stundenlimit" in reason for reason in report.skipped)

    def test_kurze_wartezeit_wird_ausgesessen(self, fast_config, store):
        eng = replace(
            fast_config,
            engagement=replace(
                fast_config.engagement,
                limits=replace(fast_config.engagement.limits, min_seconds_between_actions=5),
            ),
        )
        geschlafen: list[float] = []
        tweets = [make_tweet(str(i), hashtags=("#devops",), like_count=20) for i in range(1, 3)]
        report = build_engine(eng, store, FakeXClient(tweets), sleeper=geschlafen.append).run_cycle()
        assert geschlafen  # es wurde gewartet statt einfach zu ueberspringen
        assert report.actions["like"] >= 1

    def test_api_fehler_wird_gemeldet_nicht_verschluckt(self, fast_config, store):
        client = FakeXClient([make_tweet("1", hashtags=("#ki",), like_count=20)], fail="like")
        report = build_engine(fast_config, store, client).run_cycle()
        assert report.errors
        assert report.actions["like"] == 0

    def test_suchfehler_beendet_den_durchlauf_sauber(self, fast_config, store):
        class KaputterClient(FakeXClient):
            def search(self, query, **kwargs):
                raise XBotError("403 Verboten")

        report = build_engine(fast_config, store, KaputterClient()).run_cycle()
        assert report.errors and report.total_actions == 0

    def test_abgeschaltet(self, config, store):
        aus = replace(config, engagement=replace(config.engagement, enabled=False))
        report = build_engine(aus, store, FakeXClient()).run_cycle()
        assert report.errors == ["engagement.enabled ist false"]

    def test_probelauf_sendet_nicht_wirklich(self, fast_config, store):
        client = FakeXClient([make_tweet("1", hashtags=("#ki",), like_count=20)], dry_run=True)
        report = build_engine(fast_config, store, client).run_cycle()
        assert report.actions["like"] == 1
        assert store.summary()["like"] == {"live": 0, "dry_run": 1}


# ---------------------------------------------------------------------------
# Eigene Beitraege
# ---------------------------------------------------------------------------
class TestPoster:
    def test_veroeffentlicht(self, fast_config, store):
        client = FakeXClient()
        report = build_poster(fast_config, store, client).run(force=True)
        assert report.posted is True and report.failed is False
        assert client.calls[0][0] == "post"

    def test_haengt_hashtags_an(self, fast_config, store):
        report = build_poster(fast_config, store, FakeXClient()).run(force=True)
        assert "#" in report.text

    def test_ohne_hashtags_wenn_abgeschaltet(self, fast_config, store):
        ohne = replace(fast_config, posting=replace(fast_config.posting, include_hashtags=False))
        assert "#" not in build_poster(ohne, store, FakeXClient()).run(force=True).text

    def test_haelt_die_zeichengrenze(self, fast_config, store):
        from xbot.content.text import tweet_length

        report = build_poster(fast_config, store, FakeXClient()).run(force=True)
        assert tweet_length(report.text) <= fast_config.content.max_chars

    @pytest.mark.parametrize(
        "stunde, erwartet", [(2, False), (8, True), (14, True), (21, True), (22, False), (23, False)]
    )
    def test_zeitfenster(self, fast_config, store, stunde, erwartet):
        poster = build_poster(fast_config, store, FakeXClient())
        moment = datetime(2026, 9, 21, stunde, 0, tzinfo=BERLIN).astimezone(timezone.utc)
        assert poster.window_open(moment)[0] is erwartet

    def test_wochentag(self, fast_config, store):
        nur_montag = replace(fast_config, posting=replace(fast_config.posting, active_weekdays=(0,)))
        poster = build_poster(nur_montag, store, FakeXClient())
        montag = datetime(2026, 9, 21, 12, tzinfo=BERLIN).astimezone(timezone.utc)   # Montag
        dienstag = datetime(2026, 9, 22, 12, tzinfo=BERLIN).astimezone(timezone.utc)
        assert poster.window_open(montag)[0] is True
        assert poster.window_open(dienstag)[0] is False

    def test_ausserhalb_des_fensters_ist_kein_fehler(self, fast_config, store):
        nacht = datetime(2026, 9, 21, 3, tzinfo=BERLIN).astimezone(timezone.utc)
        report = build_poster(fast_config, store, FakeXClient()).run(now=nacht)
        assert report.posted is False and report.failed is False

    def test_force_umgeht_das_fenster_nicht_die_limits(self, fast_config, store):
        eng = replace(
            fast_config,
            engagement=replace(
                fast_config.engagement,
                limits=replace(fast_config.engagement.limits, post_per_day=0),
            ),
        )
        report = build_poster(eng, store, FakeXClient()).run(force=True)
        assert report.posted is False and report.failed is False
        assert "deaktiviert" in report.reason

    def test_abgeschaltet(self, fast_config, store):
        aus = replace(fast_config, posting=replace(fast_config.posting, enabled=False))
        assert build_poster(aus, store, FakeXClient()).run().reason == "posting.enabled ist false"

    def test_api_fehler_ist_ein_fehlschlag(self, fast_config, store):
        report = build_poster(fast_config, store, FakeXClient(fail="post")).run(force=True)
        assert report.posted is False and report.failed is True

    def test_texterstellung_kaputt_ist_ein_fehlschlag(self, fast_config, store, tmp_path):
        kaputt = replace(fast_config, content=replace(fast_config.content, templates_file=str(tmp_path / "weg.yaml")))
        report = build_poster(kaputt, store, FakeXClient()).run(force=True)
        assert report.failed is True and "Texterstellung" in report.reason

    def test_wird_protokolliert(self, fast_config, store):
        build_poster(fast_config, store, FakeXClient()).run(force=True)
        assert store.summary()["post"]["live"] == 1
        assert store.recent_texts(("post",))

"""Filter entscheiden, was der Bot niemals anfasst."""

from __future__ import annotations

from dataclasses import replace

import pytest

from xbot.config import FilterSettings, Rule
from xbot.filters import TweetFilter, first_matching_rule, rule_accepts, substantive_length
from xbot.models import Author

from .conftest import make_tweet


@pytest.fixture
def standard() -> FilterSettings:
    return FilterSettings(
        languages=("de", "en"),
        blocked_keywords=("gewinnspiel", "casino"),
        min_tweet_length=30,
        min_author_followers=30,
    )


class TestTweetFilter:
    def test_unauffaelliger_tweet_kommt_durch(self, standard):
        assert TweetFilter(standard).check(make_tweet()).passed is True

    @pytest.mark.parametrize(
        "ueberschreibung, fragment",
        [
            ({"is_retweet": True}, "Retweet"),
            ({"is_reply": True}, "Antwort"),
            ({"possibly_sensitive": True}, "sensibel"),
            ({"lang": "fr"}, "Sprache"),
            ({"text": "Grosses Gewinnspiel fuer alle, jetzt schnell mitmachen und gewinnen!"}, "gesperrten Begriff"),
            ({"hashtags": tuple(f"#t{i}" for i in range(9))}, "Hashtags"),
            ({"mentions": tuple(f"u{i}" for i in range(9))}, "Erwaehnungen"),
            ({"text": "Kurz."}, "Zeichen Inhalt"),
            ({"id": ""}, "Tweet-ID"),
        ],
    )
    def test_ablehnungsgruende(self, standard, ueberschreibung, fragment):
        verdict = TweetFilter(standard).check(make_tweet(**ueberschreibung))
        assert verdict.passed is False
        assert fragment in verdict.reason

    def test_eigene_beitraege_werden_uebersprungen(self, standard):
        tweet = make_tweet(author=Author(id="1", username="MeinBot", followers=900))
        assert "eigener Beitrag" in TweetFilter(standard, own_username="@meinbot").check(tweet).reason

    def test_sperrliste(self, standard):
        settings = replace(standard, blocked_users=("spammer",))
        tweet = make_tweet(author=Author(id="1", username="Spammer", followers=900))
        assert "Sperrliste" in TweetFilter(settings).check(tweet).reason

    def test_positivliste(self, standard):
        settings = replace(standard, allowed_users=("freund",))
        assert TweetFilter(settings).check(make_tweet()).passed is False
        erlaubt = make_tweet(author=Author(id="1", username="Freund", followers=900))
        assert TweetFilter(settings).check(erlaubt).passed is True

    def test_zu_wenig_follower(self, standard):
        tweet = make_tweet(author=Author(id="1", username="neu", followers=3))
        assert "Follower" in TweetFilter(standard).check(tweet).reason

    def test_obergrenze_follower(self, standard):
        settings = replace(standard, max_author_followers=1000)
        tweet = make_tweet(author=Author(id="1", username="gross", followers=50_000))
        assert "hoechstens" in TweetFilter(settings).check(tweet).reason

    def test_unbekanntes_profil_scheitert_nicht_an_null_follower(self, standard):
        # Fehlt die Nutzer-Erweiterung der API, darf das nicht alles blockieren.
        assert TweetFilter(standard).check(make_tweet(author=Author(id="a1"))).passed is True

    def test_verifizierung_verlangt(self, standard):
        settings = replace(standard, require_verified=True)
        assert "verifiziert" in TweetFilter(settings).check(make_tweet()).reason
        verifiziert = make_tweet(author=Author(id="1", username="v", followers=900, verified=True))
        assert TweetFilter(settings).check(verifiziert).passed is True

    def test_links_optional_sperren(self, standard):
        settings = replace(standard, skip_links=True)
        tweet = make_tweet(urls=("https://example.com",))
        assert "Link" in TweetFilter(settings).check(tweet).reason
        assert TweetFilter(standard).check(tweet).passed is True

    def test_zitate_optional_sperren(self, standard):
        assert TweetFilter(standard).check(make_tweet(is_quote=True)).passed is True
        settings = replace(standard, skip_quotes=True)
        assert "Zitat" in TweetFilter(settings).check(make_tweet(is_quote=True)).reason


def test_substanzlaenge_ignoriert_hashtags_und_links():
    assert substantive_length("Kurz. #python @a https://x.com/y") == 5
    assert substantive_length("Ein ganz normaler Satz.") == 23


class TestRegelzuordnung:
    def test_mindestresonanz(self):
        rule = Rule(name="r", hashtags=("#python",), actions=("like",), min_likes=10)
        assert rule_accepts(rule, make_tweet(like_count=3)).passed is False
        assert rule_accepts(rule, make_tweet(like_count=20)).passed is True

    def test_mindest_reposts(self):
        rule = Rule(name="r", hashtags=("#python",), actions=("like",), min_reposts=5)
        assert "Reposts" in rule_accepts(rule, make_tweet(repost_count=1)).reason

    def test_regelsprache(self):
        rule = Rule(name="r", hashtags=("#python",), actions=("like",), languages=("en",))
        assert rule_accepts(rule, make_tweet(lang="de")).passed is False

    def test_erste_passende_regel_gewinnt(self, config):
        rule, _ = first_matching_rule(config.rules, make_tweet(hashtags=("#python",), like_count=5))
        assert rule.name == "Python-Community"

    def test_begruendung_nennt_die_passende_regel(self, config):
        # #devops trifft nur Regel 3, die 5 Likes verlangt.
        rule, grund = first_matching_rule(config.rules, make_tweet(hashtags=("#devops",), like_count=1))
        assert rule is None
        assert "Regel verlangt 5" in grund

    def test_ohne_passenden_hashtag(self, config):
        rule, grund = first_matching_rule(config.rules, make_tweet(hashtags=("#voellig-anders",)))
        assert rule is None and grund == "kein passender Hashtag"

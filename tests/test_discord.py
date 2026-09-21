"""Tests der Discord-Erweiterung: Modelle, Client, Filter und Ablaeufe.

Kein Test spricht mit dem Netz. Der Client wird gegen eine gefaelschte
``requests.Session`` gefuehrt, die Ablaeufe gegen ``FakeDiscordClient``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from xbot.config import Config, ConfigError, DiscordRule, DiscordSettings
from xbot.content import ContentGenerator
from xbot.content.templates import TemplateLibrary
from xbot.discord.client import BASE_URL, DiscordClient, DiscordClientError, _describe
from xbot.discord.engage import DiscordEngagementEngine, cursor_key
from xbot.discord.filters import DiscordMessageFilter, first_matching_rule, substantive_length
from xbot.discord.models import DiscordAuthor, DiscordMessage
from xbot.discord.post import CHANNEL_CURSOR_KEY, DiscordPoster
from xbot.errors import CredentialsError
from xbot.quota import QuotaGuard
from xbot.state import PLATFORM_DISCORD, PLATFORM_X, utcnow

from .conftest import FakeDiscordClient, make_message


# ---------------------------------------------------------------------------
# Testdoubles fuer requests
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int = 200, payload=None, reason: str = "OK") -> None:
        self.status_code = status
        self._payload = payload
        self.reason = reason

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("keine gueltige Antwort")
        return self._payload


class FakeSession:
    def __init__(self, antworten) -> None:
        self.antworten = list(antworten)
        self.aufrufe: list[tuple] = []
        self.headers: dict[str, str] = {}
        self.geschlossen = False

    def request(self, method, url, params=None, json=None, timeout=None):
        self.aufrufe.append((method, url, params, json))
        if not self.antworten:
            raise AssertionError(f"unerwartete Anfrage: {method} {url}")
        antwort = self.antworten.pop(0)
        if isinstance(antwort, Exception):
            raise antwort
        return antwort

    def close(self) -> None:
        self.geschlossen = True


def client(antworten=(), **kwargs) -> DiscordClient:
    return DiscordClient("token", session=FakeSession(antworten), **kwargs)


# ---------------------------------------------------------------------------
# Modelle
# ---------------------------------------------------------------------------
class TestModelle:
    def test_liest_eine_vollstaendige_nachricht(self):
        nachricht = DiscordMessage.from_api(
            {
                "id": "123",
                "channel_id": "999",
                "content": "Schau mal https://example.com und https://zwei.de",
                "author": {"id": "7", "username": "eva", "global_name": "Eva B.", "bot": False},
                "timestamp": "2026-09-21T10:11:12.000000+00:00",
                "mentions": [{"id": "1"}, {"id": "2"}],
                "type": 19,
                "attachments": [{}, {}],
                "reactions": [
                    {"count": 3, "me": True, "emoji": {"name": "\U0001F440"}},
                    {"count": 2, "me": False, "emoji": {"name": "\U0001F525"}},
                ],
            },
            guild_id="42",
        )
        assert nachricht.author.label == "Eva B."
        assert nachricht.urls == ("https://example.com", "https://zwei.de")
        assert nachricht.mentions == ("1", "2")
        assert nachricht.is_reply and nachricht.attachments == 2
        assert nachricht.reaction_count == 5
        assert nachricht.has_reacted("\U0001F440")
        assert not nachricht.has_reacted("\U0001F525")
        assert nachricht.url == "https://discord.com/channels/42/999/123"
        assert nachricht.created_at.tzinfo is not None

    def test_haelt_luecken_aus(self):
        """Fehlt das Message-Content-Intent, kommt fast nichts zurueck."""
        nachricht = DiscordMessage.from_api({"id": "5"})
        assert nachricht.content == "" and nachricht.author.label == "?"
        assert nachricht.urls == () and not nachricht.is_reply
        assert DiscordMessage.from_api(None).id == ""
        assert DiscordAuthor.from_api("kein Mapping").label == "?"

    def test_antwort_auch_ohne_typ(self):
        assert DiscordMessage.from_api({"id": "1", "message_reference": {"message_id": "9"}}).is_reply

    def test_ungueltiger_zeitstempel_wird_zu_none(self):
        assert DiscordMessage.from_api({"id": "1", "timestamp": "gestern"}).created_at is None

    def test_vorschau_kuerzt(self):
        lang = make_message(content="Wort " * 60)
        assert len(lang.preview) == 90 and lang.preview.endswith("...")

    def test_eigenname_faellt_auf_benutzernamen_zurueck(self):
        assert DiscordAuthor.from_api({"id": "1", "username": "ohne_anzeigenamen"}).label == "ohne_anzeigenamen"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class TestClient:
    def test_probelauf_sendet_nichts(self):
        c = client([], dry_run=True)
        assert c.post("Hallo", "999").dry_run
        assert c.react("999", "1", "\U0001F440").dry_run
        assert c.session.aufrufe == []

    def test_liest_auch_im_probelauf(self):
        c = client([FakeResponse(200, [{"id": "7", "content": "x"}])], dry_run=True)
        assert [m.id for m in c.fetch_messages("999")] == ["7"]

    def test_sortiert_aufsteigend(self):
        rohdaten = [{"id": "300"}, {"id": "100"}, {"id": "200"}]
        c = client([FakeResponse(200, rohdaten)], dry_run=True)
        assert [m.id for m in c.fetch_messages("999")] == ["100", "200", "300"]

    def test_begrenzt_die_anzahl(self):
        c = client([FakeResponse(200, [])], dry_run=True)
        c.fetch_messages("999", limit=500)
        assert c.session.aufrufe[0][2] == {"limit": 100}

    def test_reicht_das_lesezeichen_durch(self):
        c = client([FakeResponse(200, [])], dry_run=True)
        c.fetch_messages("999", after="42")
        assert c.session.aufrufe[0][2]["after"] == "42"

    def test_antwort_nutzt_message_reference(self):
        c = client([FakeResponse(200, {"id": "neu"})], dry_run=False)
        ergebnis = c.post("Antwort", "999", reply_to="111")
        assert ergebnis.ok and ergebnis.action == "reply" and ergebnis.result_id == "neu"
        _, url, _, nutzlast = c.session.aufrufe[0]
        assert url == f"{BASE_URL}/channels/999/messages"
        assert nutzlast["message_reference"] == {"message_id": "111"}

    def test_reaktion_kodiert_das_emoji(self):
        c = client([FakeResponse(204, None)], dry_run=False)
        assert c.react("999", "111", "\U0001F440").ok
        methode, url, _, _ = c.session.aufrufe[0]
        assert methode == "PUT"
        assert url.endswith("/channels/999/messages/111/reactions/%F0%9F%91%80/@me")

    def test_zu_langer_text_wird_abgelehnt(self):
        ergebnis = client([], dry_run=False).post("x" * 2001, "999")
        assert not ergebnis.ok and "2000" in ergebnis.error

    @pytest.mark.parametrize(
        "args, erwartet",
        [
            (("", "999"), "Leerer Text"),
            (("Text", ""), "Keine Kanal-ID"),
        ],
    )
    def test_leere_eingaben(self, args, erwartet):
        ergebnis = client([], dry_run=False).post(*args)
        assert not ergebnis.ok and erwartet in ergebnis.error

    def test_reaktion_ohne_emoji(self):
        assert "Kein Emoji" in client([], dry_run=False).react("9", "1", "").error

    def test_fehler_wird_zum_ergebnis_nicht_zur_ausnahme(self):
        c = client([FakeResponse(403, {"code": 50013, "message": "Missing Permissions"})], dry_run=False)
        ergebnis = c.post("Hallo", "999")
        assert not ergebnis.ok and "403" in ergebnis.error and "Add Reactions" in ergebnis.error

    def test_lesefehler_wirft(self):
        c = client([FakeResponse(403, {"code": 50001})], dry_run=True)
        with pytest.raises(DiscordClientError):
            c.fetch_messages("999")

    def test_verify_liefert_das_eigene_konto(self):
        c = client([FakeResponse(200, {"id": "BOT", "username": "testbot", "bot": True})])
        konto = c.verify()
        assert konto.id == "BOT" and konto.is_bot
        # Zweiter Aufruf fragt nicht erneut.
        assert c.verify() is konto and len(c.session.aufrufe) == 1

    def test_verify_meldet_falschen_token(self):
        c = client([FakeResponse(401, {})])
        with pytest.raises(CredentialsError, match="DISCORD_BOT_TOKEN"):
            c.verify()

    def test_ohne_token_kein_verify(self):
        with pytest.raises(CredentialsError, match="DISCORD_BOT_TOKEN"):
            DiscordClient("").verify()

    def test_wartet_einmal_bei_rate_limit(self, monkeypatch):
        geschlafen: list[float] = []
        monkeypatch.setattr("xbot.discord.client.time.sleep", geschlafen.append)
        c = client(
            [FakeResponse(429, {"retry_after": 2}), FakeResponse(200, {"id": "neu"})], dry_run=False
        )
        assert c.post("Hallo", "999").ok
        assert geschlafen == [2.0] and len(c.session.aufrufe) == 2

    def test_wartet_nicht_endlos(self, monkeypatch):
        geschlafen: list[float] = []
        monkeypatch.setattr("xbot.discord.client.time.sleep", geschlafen.append)
        c = client([FakeResponse(429, {"retry_after": 600})], dry_run=False)
        assert not c.post("Hallo", "999").ok
        assert geschlafen == []

    def test_netzfehler_wird_uebersetzt(self):
        import requests

        c = client([requests.ConnectionError("kein Netz")], dry_run=True)
        with pytest.raises(DiscordClientError, match="nicht erreichbar"):
            c.fetch_messages("999")

    @pytest.mark.parametrize(
        "status, payload, stichwort",
        [
            (401, {}, "DISCORD_BOT_TOKEN"),
            (403, {"code": 50013}, "View Channel"),
            (404, {}, "Entwicklermodus"),
            (429, {}, "Rate Limit"),
            (503, {}, "Serverfehler"),
            (400, {"message": "Unbekannt"}, "Unbekannt"),
        ],
    )
    def test_fehlermeldungen_sind_deutsch_und_konkret(self, status, payload, stichwort):
        assert stichwort in _describe(FakeResponse(status, payload), kontext="Kanal 9")

    def test_close_gibt_die_sitzung_frei(self):
        c = client([])
        sitzung = c.session
        c.close()
        assert sitzung.geschlossen


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------
class TestFilter:
    @pytest.fixture
    def pruefer(self, discord_config):
        return DiscordMessageFilter(discord_config.discord.filters, own_user_id="BOT")

    def test_gute_nachricht_kommt_durch(self, pruefer):
        assert pruefer.check(make_message())

    @pytest.mark.parametrize(
        "aenderung, grund",
        [
            (dict(id=""), "ohne Nachrichten-ID"),
            (dict(author=DiscordAuthor(id="BOT")), "eigene Nachricht"),
            (dict(author=DiscordAuthor(id="9", username="mee6", is_bot=True)), "ist ein Bot"),
            (dict(mention_everyone=True), "@everyone"),
            (dict(content="kurz"), "Zeichen Inhalt"),
            (dict(content="", author=DiscordAuthor(id="9", username="eva")), "MESSAGE CONTENT INTENT"),
            (dict(mentions=("1", "2", "3", "4", "5")), "Erwaehnungen"),
        ],
    )
    def test_ablehnungen_mit_begruendung(self, pruefer, aenderung, grund):
        verdict = pruefer.check(make_message(**aenderung))
        assert not verdict and grund in verdict.reason

    def test_gesperrter_begriff(self, discord_config):
        filters = replace(discord_config.discord.filters, blocked_keywords=("gewinnspiel",))
        verdict = DiscordMessageFilter(filters).check(
            make_message(content="Grosses GEWINNSPIEL fuer alle Mitglieder hier im Kanal!")
        )
        assert not verdict and "gewinnspiel" in verdict.reason

    def test_sperrliste_trifft_name_und_id(self, discord_config):
        filters = replace(discord_config.discord.filters, blocked_users=("nutzer100", "u200"))
        pruefer = DiscordMessageFilter(filters)
        assert not pruefer.check(make_message("100"))
        assert not pruefer.check(make_message("200"))
        assert pruefer.check(make_message("300"))

    def test_positivliste_schliesst_alle_anderen_aus(self, discord_config):
        filters = replace(discord_config.discord.filters, allowed_users=("nutzer100",))
        pruefer = DiscordMessageFilter(filters)
        assert pruefer.check(make_message("100"))
        assert not pruefer.check(make_message("200"))

    def test_bots_duerfen_erlaubt_werden(self, discord_config):
        filters = replace(discord_config.discord.filters, skip_bots=False)
        bot = make_message(author=DiscordAuthor(id="9", username="mee6", is_bot=True))
        assert DiscordMessageFilter(filters).check(bot)

    def test_links_und_antworten_optional(self, discord_config):
        streng = replace(discord_config.discord.filters, skip_links=True, skip_replies=True)
        pruefer = DiscordMessageFilter(streng)
        assert not pruefer.check(make_message(content="Guter Artikel dazu https://example.com/lang",
                                              urls=("https://example.com/lang",)))
        assert not pruefer.check(make_message(is_reply=True))

    def test_substantielle_laenge_ignoriert_beiwerk(self):
        assert substantive_length("<@123> <#456> https://example.com ```code```") == 0
        assert substantive_length("Hallo Welt") == 10


class TestRegelzuordnung:
    def test_erste_passende_regel_gewinnt(self, discord_config):
        regeln = discord_config.discord.rules
        rule, _ = first_matching_rule(regeln, make_message(content="python und docker im Einsatz"))
        assert rule.name == "Python"

    def test_kein_treffer_wird_begruendet(self, discord_config):
        rule, warum = first_matching_rule(discord_config.discord.rules, make_message(content="Ganz anderes Thema hier"))
        assert rule is None and warum == "kein passendes Schluesselwort"

    def test_mindestlaenge_der_regel_erklaert_die_ablehnung(self):
        regeln = (DiscordRule(name="Lang", keywords=("python",), actions=("react",), min_length=100),)
        rule, warum = first_matching_rule(regeln, make_message(content="python"))
        assert rule is None and "Regel verlangt 100" in warum

    def test_kanalbindung(self):
        regeln = (DiscordRule(name="Nur dort", keywords=("hilfe",), actions=("react",), channels=("777",)),)
        assert first_matching_rule(regeln, make_message(content="hilfe", channel_id="555"))[0] is None
        assert first_matching_rule(regeln, make_message(content="hilfe", channel_id="777"))[0].name == "Nur dort"

    def test_alle_woerter_noetig(self):
        regel = DiscordRule(name="Beides", keywords=("docker", "compose"), actions=("react",), match="all")
        assert regel.matches("docker compose hoch")
        assert not regel.matches("nur docker")

    def test_ohne_regel(self):
        assert first_matching_rule((), make_message()) == (None, "keine Regel definiert")


# ---------------------------------------------------------------------------
# Eigene Beitraege
# ---------------------------------------------------------------------------
class TestPoster:
    @pytest.fixture
    def poster(self, discord_config, store, fake_discord_client):
        quota = QuotaGuard(
            discord_config.discord.engagement.limits,
            store,
            discord_config.bot.tzinfo,
            platform=PLATFORM_DISCORD,
        )
        generator = ContentGenerator(
            discord_config, store, library=TemplateLibrary.load(discord_config.content.templates_file)
        )
        return DiscordPoster(discord_config, fake_discord_client, store, quota, generator)

    def test_sendet_in_den_ersten_kanal(self, poster, fake_discord_client):
        bericht = poster.run()
        assert bericht.posted and bericht.channel_id == "111000000000000000"
        assert fake_discord_client.calls[0][0] == "post"

    def test_geht_die_kanaele_reihum_durch(self, poster):
        assert poster.run().channel_id == "111000000000000000"
        assert poster.run().channel_id == "222000000000000000"
        assert poster.next_channel() == "111000000000000000"

    def test_zeiger_bleibt_bei_einem_fehlschlag(self, poster, store):
        poster.client.fail = "post"
        bericht = poster.run()
        assert not bericht.posted and bericht.failed
        assert store.get_state(CHANNEL_CURSOR_KEY) is None

    def test_ausdruecklicher_kanal_schlaegt_die_reihenfolge(self, poster):
        assert poster.run(channel="999000000000000000").channel_id == "999000000000000000"

    def test_abgeschaltet_heisst_kein_beitrag(self, poster):
        poster.config = replace(poster.config, discord=replace(poster.config.discord, enabled=False))
        bericht = poster.run()
        assert not bericht.posted and "discord.enabled" in bericht.reason
        assert not bericht.failed          # kein Fehler, nur nicht zustaendig

    def test_zeitfenster_wird_beachtet(self, poster):
        zu = replace(poster.config.discord.posting, active_weekdays=())
        poster.config = replace(poster.config, discord=replace(poster.config.discord, posting=zu))
        bericht = poster.run()
        assert not bericht.posted and "Wochentag" in bericht.reason
        assert poster.run(force=True).posted

    def test_limit_gilt_auch_bei_force(self, poster):
        grenzen = replace(poster.config.discord.engagement.limits, post_per_hour=0)
        poster.config = replace(
            poster.config,
            discord=replace(
                poster.config.discord,
                engagement=replace(poster.config.discord.engagement, limits=grenzen),
            ),
        )
        poster.quota.limits = grenzen
        bericht = poster.run(force=True)
        assert not bericht.posted and "deaktiviert" in bericht.reason

    def test_zaehlt_auf_das_discord_konto(self, poster, store):
        poster.run()
        seit = utcnow() - timedelta(hours=1)
        assert store.count_actions("post", seit, platform=PLATFORM_DISCORD) == 1
        assert store.count_actions("post", seit, platform=PLATFORM_X) == 0

    def test_haelt_die_zeichengrenze_ein(self, poster):
        poster.config = replace(poster.config, discord=replace(poster.config.discord, max_chars=40))
        poster.generator.config = poster.config
        assert len(poster.run().text) <= 40


# ---------------------------------------------------------------------------
# Auf Nachrichten reagieren
# ---------------------------------------------------------------------------
class TestEngagement:
    def engine(self, config, store, client):
        quota = QuotaGuard(
            config.discord.engagement.limits, store, config.bot.tzinfo, platform=PLATFORM_DISCORD
        )
        generator = ContentGenerator(
            config, store, library=TemplateLibrary.load(config.content.templates_file)
        )
        return DiscordEngagementEngine(config, client, store, quota, generator, sleeper=lambda s: None)

    def test_reagiert_und_antwortet(self, discord_config, store):
        client = FakeDiscordClient([make_message("100")])
        bericht = self.engine(discord_config, store, client).run_cycle()
        assert bericht.actions["react"] == 1 and bericht.actions["reply"] == 1
        assert [c[0] for c in client.calls] == ["react", "reply"]

    def test_hoechstens_eine_nachricht_je_verfasser(self, discord_config, store):
        autor = DiscordAuthor(id="u1", username="eva", display_name="Eva")
        nachrichten = [make_message("100", author=autor), make_message("101", author=autor)]
        bericht = self.engine(discord_config, store, FakeDiscordClient(nachrichten)).run_cycle()
        assert bericht.total_actions == 2        # nur die erste Nachricht
        assert any("Verfasser" in grund for grund in bericht.skipped)

    def test_budget_begrenzt_den_durchlauf(self, discord_config, store):
        eng = replace(discord_config.discord.engagement, max_actions_per_cycle=1)
        config = replace(discord_config, discord=replace(discord_config.discord, engagement=eng))
        bericht = self.engine(config, store, FakeDiscordClient([make_message("100")])).run_cycle()
        assert bericht.total_actions == 1

    def test_regelgewicht_bestimmt_die_reihenfolge(self, discord_config, store):
        nachrichten = [
            make_message("100", content="Wir nutzen docker fuer alle Dienste im Betrieb."),
            make_message("101", content="Welche python Version nutzt ihr in der Produktion?"),
        ]
        eng = replace(discord_config.discord.engagement, max_actions_per_cycle=1)
        config = replace(discord_config, discord=replace(discord_config.discord, engagement=eng))
        client = FakeDiscordClient(nachrichten)
        self.engine(config, store, client).run_cycle()
        # Die Python-Regel wiegt schwerer, obwohl die Docker-Nachricht zuerst kam.
        assert client.calls[0][1] == "101"

    def test_bereits_bewertete_nachricht_faellt_raus(self, discord_config, store):
        client = FakeDiscordClient([make_message("100")])
        engine = self.engine(discord_config, store, client)
        engine.run_cycle()
        # Lesezeichen zuruecksetzen, damit dieselbe Nachricht erneut ankommt.
        store.set_state(cursor_key("555000000000000000"), "")
        zweiter = engine.run_cycle()
        assert zweiter.total_actions == 0
        assert zweiter.skipped["bereits bewertet"] == 1

    def test_lesezeichen_waechst(self, discord_config, store):
        client = FakeDiscordClient([make_message("100"), make_message("101")])
        engine = self.engine(discord_config, store, client)
        engine.run_cycle()
        assert store.get_state(cursor_key("555000000000000000")) == "101"
        engine.run_cycle()
        assert client.fetches[-1][2] == "101"

    def test_zu_alte_nachrichten_werden_uebersprungen(self, discord_config, store):
        alt = make_message("100", created_at=utcnow() - timedelta(days=3))
        bericht = self.engine(discord_config, store, FakeDiscordClient([alt])).run_cycle()
        assert bericht.total_actions == 0
        assert bericht.skipped["aelter als das Zeitfenster"] == 1

    def test_eigene_nachricht_wird_nie_bearbeitet(self, discord_config, store):
        eigen = make_message("100", author=DiscordAuthor(id="BOT", username="testbot", is_bot=True))
        bericht = self.engine(discord_config, store, FakeDiscordClient([eigen])).run_cycle()
        assert bericht.total_actions == 0

    def test_vorhandene_eigene_reaktion_wird_nicht_wiederholt(self, discord_config, store):
        schon = make_message("100", own_reactions=("\U0001F440",))
        bericht = self.engine(discord_config, store, FakeDiscordClient([schon])).run_cycle()
        assert "react" not in bericht.actions
        assert bericht.skipped["react: Reaktion steht schon dran"] == 1

    def test_lesefehler_landet_im_bericht(self, discord_config, store):
        client = FakeDiscordClient(read_error="403 Verboten - kein Zugriff")
        bericht = self.engine(discord_config, store, client).run_cycle()
        assert bericht.errors and "403" in bericht.errors[0]
        assert "fehlgeschlagen" in bericht.describe()

    def test_schreibfehler_zaehlt_nicht_als_aktion(self, discord_config, store):
        client = FakeDiscordClient([make_message("100")], fail="react")
        bericht = self.engine(discord_config, store, client).run_cycle()
        assert "react" not in bericht.actions
        assert bericht.errors

    @pytest.mark.parametrize(
        "schalter, grund",
        [
            ({"enabled": False}, "discord.enabled"),
            ({"rules": ()}, "keine Discord-Regeln"),
        ],
    )
    def test_abschaltungen_werden_gemeldet(self, discord_config, store, schalter, grund):
        config = replace(discord_config, discord=replace(discord_config.discord, **schalter))
        bericht = self.engine(config, store, FakeDiscordClient([make_message()])).run_cycle()
        assert bericht.errors and grund in bericht.errors[0]

    def test_probelauf_schreibt_nur_probelauf_eintraege(self, discord_config, store):
        client = FakeDiscordClient([make_message("100")], dry_run=True)
        self.engine(discord_config, store, client).run_cycle()
        seit = utcnow() - timedelta(hours=1)
        assert store.count_actions("react", seit, platform=PLATFORM_DISCORD) == 0
        assert store.count_actions("react", seit, platform=PLATFORM_DISCORD, include_dry_run=True) == 1

    def test_zaehler_bleiben_von_x_getrennt(self, discord_config, store):
        self.engine(discord_config, store, FakeDiscordClient([make_message("100")])).run_cycle()
        seit = utcnow() - timedelta(hours=1)
        assert store.count_actions("reply", seit, platform=PLATFORM_DISCORD) == 1
        assert store.count_actions("reply", seit, platform=PLATFORM_X) == 0
        assert not store.is_seen("100", platform=PLATFORM_X)
        assert store.is_seen("100", platform=PLATFORM_DISCORD)

    def test_bericht_beschreibt_den_durchlauf(self, discord_config, store):
        bericht = self.engine(discord_config, store, FakeDiscordClient([make_message("100")])).run_cycle()
        text = bericht.describe()
        assert "1 Kanal" in text and "react" in text and "reply" in text


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
class TestKonfigurationsfehler:
    def test_kanalname_statt_id(self):
        with pytest.raises(ConfigError, match="Entwicklermodus"):
            DiscordSettings.parse({"posting": {"channels": ["#allgemein"]}})

    def test_engagement_ohne_regeln(self):
        with pytest.raises(ConfigError, match="keine Regel definiert"):
            DiscordSettings.parse(
                {"enabled": True, "engagement": {"watch_channels": ["1234567"]}, "posting": {"enabled": False}}
            )

    def test_engagement_ohne_kanal(self):
        with pytest.raises(ConfigError, match="kein Kanal"):
            DiscordSettings.parse(
                {
                    "enabled": True,
                    "posting": {"enabled": False},
                    "rules": [{"name": "R", "keywords": ["a"], "actions": ["react"]}],
                }
            )

    def test_posting_ohne_zielkanal(self):
        with pytest.raises(ConfigError, match="kein Zielkanal"):
            DiscordSettings.parse({"enabled": True, "engagement": {"enabled": False}})

    def test_unbekannte_aktion(self):
        with pytest.raises(ConfigError, match="unbekannt"):
            DiscordRule.parse({"name": "R", "keywords": ["a"], "actions": ["repost"]}, 0)

    def test_regel_ohne_schluesselwort(self):
        with pytest.raises(ConfigError, match="mindestens ein Schluesselwort"):
            DiscordRule.parse({"name": "R", "keywords": [], "actions": ["react"]}, 0)

    def test_beispielkonfiguration_laesst_discord_aus(self, raw_config):
        """Wer die Vorlage kopiert, bekommt Discord nicht ungefragt."""
        assert Config.parse(raw_config).discord.enabled is False


# ---------------------------------------------------------------------------
# Bot und Kommandozeile
# ---------------------------------------------------------------------------
class TestBotverdrahtung:
    def test_zeitplan_bekommt_discord_nur_wenn_eingeschaltet(self, config, discord_config):
        from xbot.bot import Bot

        with Bot(config) as bot:
            assert not any("Discord" in job.name for job in bot.build_jobs())
        with Bot(discord_config) as bot:
            namen = [job.name for job in bot.build_jobs()]
            assert "Discord-Beitrag veroeffentlichen" in namen
            assert "Discord-Kanaele beobachten" in namen

    def test_eigener_waechter_je_plattform(self, discord_config):
        from xbot.bot import Bot

        with Bot(discord_config) as bot:
            assert bot.quota.platform == PLATFORM_X
            assert bot.discord_quota.platform == PLATFORM_DISCORD
            assert bot.discord_quota.actions == ("post", "react", "reply")
            assert bot.quota is not bot.discord_quota

    def test_statistik_weist_beide_plattformen_aus(self, discord_config):
        from xbot.bot import Bot

        with Bot(discord_config) as bot:
            daten = bot.stats()
            assert daten["discord_enabled"] is True
            assert set(daten["discord_quota"]) == {"post", "react", "reply"}
            assert set(daten["quota"]) == {"post", "like", "repost", "reply"}

    def test_selbsttest_meldet_fehlenden_token(self, discord_config):
        from xbot.bot import Bot

        with Bot(discord_config) as bot:
            offen = [c for c in bot.doctor() if not c.ok and "Discord" in c.name]
            assert offen and "DISCORD_BOT_TOKEN" in offen[0].detail

    def test_selbsttest_bleibt_kurz_wenn_discord_aus_ist(self, config):
        from xbot.bot import Bot

        with Bot(config) as bot:
            zeilen = [c for c in bot.doctor() if c.name.startswith("Discord")]
            assert len(zeilen) == 1 and zeilen[0].ok
            assert "abgeschaltet" in zeilen[0].detail

    def test_abgeschaltet_heisst_kein_durchlauf(self, config):
        from xbot.bot import Bot

        with Bot(config) as bot:
            bericht = bot.discord_engage_once()
            assert bericht.errors and "discord.enabled" in bericht.errors[0]


class TestKommandozeile:
    def test_unterbefehle_werden_erkannt(self):
        from xbot.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["discord", "post", "--channel", "123", "--force"])
        assert args.channel == "123" and args.force is True
        assert parser.parse_args(["discord", "engage"]).show_skips == 8

    def test_globale_flags_gelten_auch_hier(self):
        from xbot.cli import build_parser

        parser = build_parser()
        for argv in (["-q", "discord", "engage"], ["discord", "engage", "-q"]):
            assert parser.parse_args(argv).quiet is True

    def test_meldet_abgeschaltetes_discord(self, tmp_path, monkeypatch, capsys):
        import shutil
        from pathlib import Path

        from xbot.cli import main

        wurzel = Path(__file__).resolve().parent.parent
        monkeypatch.chdir(tmp_path)
        shutil.copy(wurzel / "config.example.yaml", tmp_path / "config.yaml")
        monkeypatch.delenv("XBOT_CONFIG", raising=False)

        assert main(["-q", "discord", "engage"]) == 2
        assert "abgeschaltet" in capsys.readouterr().out

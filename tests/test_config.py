"""Die Konfiguration muss Tippfehler beim Start melden, nicht im Betrieb."""

from __future__ import annotations

import pytest

from xbot.config import Config, Credentials, Rule, load_config, normalise_hashtag
from xbot.errors import ConfigError


def test_beispielkonfiguration_laedt(config):
    assert config.bot.timezone == "Europe/Berlin"
    assert len(config.rules) == 3
    assert "#python" in config.monitored_hashtags


def test_hashtags_werden_vereinheitlicht():
    assert normalise_hashtag("KI") == "#ki"
    assert normalise_hashtag("#Python") == "#python"
    assert normalise_hashtag("  #DevOps ") == "#devops"
    assert normalise_hashtag("#") == ""


@pytest.mark.parametrize(
    "data, fragment",
    [
        ({"bot": {"timezone": "Mars/Olympus"}}, "Zeitzone"),
        ({"posting": {"active_hours": [22, 8]}}, "kleiner als Endstunde"),
        ({"posting": {"active_hours": [1, 2, 3]}}, "genau zwei Werte"),
        ({"posting": {"active_weekdays": [9]}}, "zwischen 0"),
        ({"content": {"provider": "magie"}}, "provider"),
        ({"content": {"effort": "turbo"}}, "effort"),
        ({"content": {"max_chars": 500}}, "hoechstens 280"),
        ({"bot": {"dry_run": "vielleicht"}}, "true/false"),
        ({"filters": {"min_author_followers": 100, "max_author_followers": 10}}, "max_author_followers"),
        ({"rules": [{"name": "x", "hashtags": [], "actions": ["like"]}]}, "mindestens ein Hashtag"),
        ({"rules": [{"name": "x", "hashtags": ["#a"], "actions": ["tanzen"]}]}, "unbekannt"),
        ({"rules": [{"name": "x", "hashtags": ["#a"], "match": "irgendwie"}]}, "match"),
        ({"logging": {"level": "LAUT"}}, "Level"),
    ],
)
def test_fehlerhafte_werte_werden_abgelehnt(raw_config, data, fragment):
    merged = {**raw_config}
    for key, value in data.items():
        merged[key] = {**merged.get(key, {}), **value} if isinstance(value, dict) else value
    with pytest.raises(ConfigError) as exc:
        Config.parse(merged, credentials=Credentials())
    assert fragment in str(exc.value)


def test_engagement_ohne_regeln_wird_abgelehnt(raw_config):
    merged = {**raw_config, "rules": []}
    with pytest.raises(ConfigError, match="keine Regel"):
        Config.parse(merged, credentials=Credentials())


def test_engagement_abgeschaltet_erlaubt_leere_regeln(raw_config):
    merged = {**raw_config, "rules": [], "engagement": {**raw_config["engagement"], "enabled": False}}
    assert Config.parse(merged, credentials=Credentials()).rules == ()


def test_dry_run_wird_per_umgebungsvariable_ueberschrieben(tmp_path, raw_config):
    import yaml

    path = tmp_path / "config.yaml"
    raw_config["bot"]["dry_run"] = True
    path.write_text(yaml.safe_dump(raw_config), encoding="utf-8")

    assert load_config(path, env={}).bot.dry_run is True
    assert load_config(path, env={"XBOT_DRY_RUN": "false"}).bot.dry_run is False
    assert load_config(path, env={"XBOT_DRY_RUN": "true"}).bot.dry_run is True
    with pytest.raises(ConfigError, match="XBOT_DRY_RUN"):
        load_config(path, env={"XBOT_DRY_RUN": "irgendwas"})


def test_fehlende_datei_nennt_die_vorlage(tmp_path):
    with pytest.raises(ConfigError, match="nicht gefunden"):
        load_config(tmp_path / "weg.yaml", env={})


def test_regel_match_modi():
    rule_any = Rule(name="a", hashtags=("#a", "#b"), actions=("like",), match="any")
    rule_all = Rule(name="b", hashtags=("#a", "#b"), actions=("like",), match="all")
    assert rule_any.matches(["#a"]) is True
    assert rule_all.matches(["#a"]) is False
    assert rule_all.matches(["#a", "#b", "#c"]) is True


def test_zugangsdaten_verraten_nichts_im_repr():
    creds = Credentials(api_key="geheim", anthropic_api_key="auch-geheim")
    assert "geheim" not in repr(creds)


def test_zugangsdaten_erkennen_fehlende_felder():
    creds = Credentials(api_key="a", api_secret="b")
    assert creds.has_write_access is False
    assert creds.missing_write_fields() == ["X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    voll = Credentials(api_key="a", api_secret="b", access_token="c", access_token_secret="d")
    assert voll.has_write_access is True
    assert voll.has_search_access is True


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
class TestDiscordKonfiguration:
    def test_beispielkonfiguration_enthaelt_discord(self, config):
        assert config.discord.enabled is False          # aus im Auslieferungszustand
        assert len(config.discord.rules) == 2
        assert "traceback" in config.discord.watched_keywords

    def test_fehlt_der_abschnitt_bleibt_discord_aus(self):
        """Bestandsnutzer ohne discord-Abschnitt sollen nichts merken."""
        cfg = Config.parse({"engagement": {"enabled": False}}, credentials=Credentials())
        assert cfg.discord.enabled is False
        assert cfg.discord.rules == ()
        assert cfg.discord.posting.channels == ()

    @pytest.mark.parametrize(
        "rule, fragment",
        [
            ({"name": "x", "keywords": [], "actions": ["react"]}, "mindestens ein Schluesselwort"),
            ({"name": "x", "keywords": ["a"], "actions": ["tanzen"]}, "unbekannt"),
            ({"name": "x", "keywords": ["a"], "actions": ["react"], "match": "irgendwie"}, "match"),
            ({"name": "x", "keywords": ["a"], "actions": ["react"], "channels": ["allgemein"]}, "keine Kanal-ID"),
        ],
    )
    def test_fehlerhafte_regeln(self, raw_config, rule, fragment):
        merged = {**raw_config, "discord": {**raw_config["discord"], "rules": [rule]}}
        with pytest.raises(ConfigError) as exc:
            Config.parse(merged, credentials=Credentials())
        assert fragment in str(exc.value)

    def test_kanalnamen_statt_id_wird_erkannt(self, raw_config):
        """Der haeufigste Einrichtungsfehler: Name statt ID eingetragen."""
        merged = {
            **raw_config,
            "discord": {
                **raw_config["discord"],
                "engagement": {**raw_config["discord"]["engagement"], "watch_channels": ["#allgemein"]},
            },
        }
        with pytest.raises(ConfigError, match="keine Kanal-ID"):
            Config.parse(merged, credentials=Credentials())

    def test_eingeschaltet_ohne_kanal_wird_abgelehnt(self, raw_config):
        merged = {
            **raw_config,
            "discord": {
                **raw_config["discord"],
                "enabled": True,
                "posting": {**raw_config["discord"]["posting"], "enabled": False},
                "engagement": {**raw_config["discord"]["engagement"], "watch_channels": []},
            },
        }
        with pytest.raises(ConfigError, match="kein Kanal"):
            Config.parse(merged, credentials=Credentials())

    def test_eingeschaltet_ohne_zielkanal_wird_abgelehnt(self, raw_config):
        merged = {
            **raw_config,
            "discord": {
                **raw_config["discord"],
                "enabled": True,
                "posting": {**raw_config["discord"]["posting"], "enabled": True, "channels": []},
                "engagement": {**raw_config["discord"]["engagement"], "enabled": False},
            },
        }
        with pytest.raises(ConfigError, match="kein Zielkanal"):
            Config.parse(merged, credentials=Credentials())

    def test_vollstaendige_konfiguration_laedt(self, raw_config):
        merged = {
            **raw_config,
            "discord": {
                **raw_config["discord"],
                "enabled": True,
                "posting": {**raw_config["discord"]["posting"], "channels": ["123456789012345678"]},
                "engagement": {
                    **raw_config["discord"]["engagement"],
                    "watch_channels": ["234567890123456789"],
                },
            },
        }
        cfg = Config.parse(merged, credentials=Credentials())
        assert cfg.discord.enabled is True
        assert cfg.discord.posting.channels == ("123456789012345678",)
        assert cfg.discord.all_watch_channels == ("234567890123456789",)

    def test_regel_kanaele_zaehlen_zu_den_beobachteten(self, raw_config):
        merged = {
            **raw_config,
            "discord": {
                **raw_config["discord"],
                "rules": [
                    {
                        "name": "Nur ein Kanal",
                        "keywords": ["hilfe"],
                        "actions": ["react"],
                        "channels": ["345678901234567890"],
                    }
                ],
            },
        }
        cfg = Config.parse(merged, credentials=Credentials())
        assert "345678901234567890" in cfg.discord.all_watch_channels

    def test_regeltreffer(self):
        from xbot.config import DiscordRule

        regel = DiscordRule(name="r", keywords=("traceback", "fehler"), actions=("react",), match="any")
        assert regel.matches("Hier ist mein Traceback") is True
        assert regel.matches("alles gut") is False

        alle = DiscordRule(name="r", keywords=("a", "b"), actions=("react",), match="all")
        assert alle.matches("nur a") is False
        assert alle.matches("a und b") is True

    def test_regel_beschraenkt_auf_kanal(self):
        from xbot.config import DiscordRule

        regel = DiscordRule(name="r", keywords=("hilfe",), actions=("react",), channels=("111",))
        assert regel.matches("hilfe", channel_id="111") is True
        assert regel.matches("hilfe", channel_id="222") is False

    def test_discord_limits_zugriff(self):
        from xbot.config import DiscordLimits

        limits = DiscordLimits(react_per_day=99)
        assert limits.per_day("react") == 99
        assert limits.per_hour("reply") == 3

    def test_zugangsdaten_kennen_den_token(self):
        leer = Credentials()
        assert leer.has_discord is False
        mit = Credentials(discord_bot_token="abc")
        assert mit.has_discord is True
        assert "abc" not in repr(mit)

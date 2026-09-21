"""Weboberflaeche: Zugangsschutz, Seiten, Steuerung und Schreibzugriffe."""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

import pytest

pytest.importorskip("flask", reason="Weboberflaeche nicht installiert")
pytest.importorskip("ruamel.yaml", reason="Weboberflaeche nicht installiert")

from xbot.config import load_config           # noqa: E402
from xbot.errors import ConfigError           # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "testpasswort-123"
CSRF_PATTERN = re.compile(r'name="csrf-token" content="([^"]+)"')


# ---------------------------------------------------------------------------
# Umgebung
# ---------------------------------------------------------------------------
@pytest.fixture
def workdir(tmp_path, monkeypatch) -> Path:
    """Ein vollstaendiges Arbeitsverzeichnis wie nach 'xbot init'."""
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    (tmp_path / "content").mkdir()
    shutil.copy(ROOT / "content" / "templates.yaml", tmp_path / "content" / "templates.yaml")
    monkeypatch.chdir(tmp_path)
    for name in ("XBOT_DRY_RUN", "XBOT_CONFIG", "XBOT_WEB_SECRET", "X_API_KEY", "X_BEARER_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XBOT_WEB_PASSWORD", PASSWORD)
    return tmp_path


@pytest.fixture
def app(workdir):
    from xbot.web import create_app

    application = create_app(workdir / "config.yaml", env_path=workdir / ".env", setup_logging=False)
    yield application
    application.extensions["xbot_runner"].shutdown()


@pytest.fixture
def anon(app):
    """Client ohne Anmeldung."""
    return app.test_client()


@pytest.fixture
def client(app):
    """Angemeldeter Client."""
    test_client = app.test_client()
    token = CSRF_PATTERN.search(test_client.get("/login").get_data(as_text=True)).group(1)
    response = test_client.post("/login", data={"password": PASSWORD, "_csrf": token})
    assert response.status_code == 302
    return test_client


def csrf(test_client, path: str = "/") -> str:
    return CSRF_PATTERN.search(test_client.get(path).get_data(as_text=True)).group(1)


def wait_for_task(test_client, task_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = test_client.get(f"/api/task/{task_id}").get_json()
        if state["done"]:
            return state
        time.sleep(0.15)
    raise AssertionError(f"Auftrag {task_id} wurde nicht fertig")


# ---------------------------------------------------------------------------
class TestZugangsschutz:
    def test_seiten_verlangen_anmeldung(self, anon):
        for path in ["/", "/rules", "/settings", "/content", "/activity", "/setup", "/logs"]:
            response = anon.get(path)
            assert response.status_code == 302
            assert "/login" in response.headers["Location"]

    def test_api_antwortet_mit_401(self, anon):
        assert anon.get("/api/status").status_code == 401

    def test_falsches_passwort(self, anon):
        token = csrf(anon, "/login")
        response = anon.post("/login", data={"password": "falsch", "_csrf": token})
        assert response.status_code == 200
        assert "stimmt nicht" in response.get_data(as_text=True)

    def test_anmeldung_und_abmeldung(self, client):
        assert client.get("/").status_code == 200
        client.post("/logout", data={"_csrf": csrf(client)})
        assert client.get("/").status_code == 302

    def test_bremse_nach_fehlversuchen(self, anon, app):
        from xbot.web.auth import MAX_ATTEMPTS

        token = csrf(anon, "/login")
        for _ in range(MAX_ATTEMPTS):
            anon.post("/login", data={"password": "falsch", "_csrf": token})
        response = anon.post("/login", data={"password": PASSWORD, "_csrf": token})
        assert "Zu viele Fehlversuche" in response.get_data(as_text=True)

    def test_weiterleitung_bleibt_intern(self, anon):
        """Ein fremdes Ziel in ?next darf nicht nach aussen fuehren."""
        token = csrf(anon, "/login")
        response = anon.post(
            "/login?next=https://boeser-host.example/", data={"password": PASSWORD, "_csrf": token}
        )
        assert response.headers["Location"] in ("/", "http://localhost/")

    def test_sicherheitsheader(self, client):
        headers = client.get("/").headers
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "no-store" in headers["Cache-Control"]


class TestCSRF:
    def test_api_ohne_token_wird_abgelehnt(self, client):
        response = client.post("/api/control", json={"command": "start"})
        assert response.status_code == 403

    def test_formular_ohne_token_wird_abgelehnt(self, client):
        assert client.post("/settings/save", data={"str:bot.language": "en"}).status_code == 403

    def test_mit_token_geht_es(self, client):
        response = client.post(
            "/api/control", json={"command": "start"}, headers={"X-CSRF-Token": csrf(client)}
        )
        assert response.status_code == 200
        assert response.get_json()["running"] is True


class TestSeiten:
    @pytest.mark.parametrize(
        "path, marker",
        [
            ("/", "Steuerung"),
            ("/rules", "Hashtag-Regeln"),
            ("/settings", "Sicherheitsfilter"),
            ("/content", "Vorlagendatei"),
            ("/activity", "Aktivität"),
            ("/setup", "Zugangsdaten"),
            ("/logs", "Protokoll"),
        ],
    )
    def test_seite_rendert(self, client, path, marker):
        response = client.get(path)
        assert response.status_code == 200
        assert marker in response.get_data(as_text=True)

    def test_unbekannte_seite(self, client):
        assert client.get("/gibt-es-nicht").status_code == 404

    def test_kaputte_konfiguration_zeigt_reparaturseite(self, client, workdir):
        """Auch eine von aussen kaputt gemachte Datei muss im Browser reparierbar sein."""
        (workdir / "config.yaml").write_text(
            "bot:\n  timezone: Mars/Olympus\nengagement:\n  enabled: false\n", encoding="utf-8"
        )
        response = client.get("/settings")
        assert response.status_code == 500
        body = response.get_data(as_text=True)
        assert "Zeitzone" in body
        # Die Seite bietet die Korrektur direkt an.
        assert 'name="raw"' in body

    def test_kaputtes_yaml_zeigt_reparaturseite(self, client, workdir):
        (workdir / "config.yaml").write_text("bot: [offen\n", encoding="utf-8")
        response = client.get("/")
        assert response.status_code == 500
        assert "YAML" in response.get_data(as_text=True)

    def test_aenderung_von_aussen_wird_bemerkt(self, client, workdir):
        """Eine Bearbeitung im Editor soll die Oberflaeche sofort erreichen."""
        import time

        assert "Europe/Berlin" in client.get("/").get_data(as_text=True)
        text = (workdir / "config.yaml").read_text(encoding="utf-8")
        time.sleep(0.01)
        (workdir / "config.yaml").write_text(
            text.replace('timezone: "Europe/Berlin"', 'timezone: "Europe/Lisbon"'), encoding="utf-8"
        )
        assert "Europe/Lisbon" in client.get("/").get_data(as_text=True)


class TestSteuerung:
    def test_start_und_stopp(self, client):
        token = csrf(client)
        client.post("/api/control", json={"command": "start"}, headers={"X-CSRF-Token": token})
        assert client.get("/api/status").get_json()["running"] is True
        client.post("/api/control", json={"command": "stop"}, headers={"X-CSRF-Token": token})
        assert client.get("/api/status").get_json()["running"] is False

    def test_betriebsmodus_umschalten(self, client, workdir):
        token = csrf(client)
        response = client.post(
            "/api/control", json={"command": "mode", "dry_run": False}, headers={"X-CSRF-Token": token}
        )
        assert response.get_json()["dry_run"] is False
        assert load_config(workdir / "config.yaml").bot.dry_run is False

    def test_modus_zieht_die_umgebungsvariable_mit(self, client, workdir, monkeypatch):
        """XBOT_DRY_RUN sticht die Datei - sonst bliebe der Schalter wirkungslos."""
        monkeypatch.setenv("XBOT_DRY_RUN", "true")
        client.post(
            "/api/control",
            json={"command": "mode", "dry_run": False},
            headers={"X-CSRF-Token": csrf(client)},
        )
        import os

        assert os.environ["XBOT_DRY_RUN"] == "false"

    def test_unbekannter_befehl(self, client):
        response = client.post(
            "/api/control", json={"command": "tanzen"}, headers={"X-CSRF-Token": csrf(client)}
        )
        assert response.status_code == 400


class TestAuftraege:
    def test_vorschau_liefert_texte(self, client):
        response = client.post("/api/run/preview", json={}, headers={"X-CSRF-Token": csrf(client)})
        task = response.get_json()["task"]
        state = wait_for_task(client, task["id"])
        assert state["ok"] is True
        assert len(state["detail"]) == 3

    def test_pruefung_meldet_fehlende_zugangsdaten(self, client):
        response = client.post("/api/run/doctor", json={}, headers={"X-CSRF-Token": csrf(client)})
        state = wait_for_task(client, response.get_json()["task"]["id"])
        assert state["ok"] is False
        assert any("X-Zugangsdaten" in line for line in state["detail"])

    def test_beitrag_im_probelauf(self, client):
        response = client.post("/api/run/post", json={}, headers={"X-CSRF-Token": csrf(client)})
        state = wait_for_task(client, response.get_json()["task"]["id"])
        assert state["ok"] is True
        assert "PROBELAUF" in state["summary"]

    def test_unbekannter_auftrag(self, client):
        response = client.post("/api/run/fliegen", json={}, headers={"X-CSRF-Token": csrf(client)})
        assert response.status_code == 400

    def test_unbekannte_auftragsnummer(self, client):
        assert client.get("/api/task/gibtesnicht").status_code == 404


class TestEinstellungenSchreiben:
    def test_formular_wird_uebernommen(self, client, workdir):
        client.post(
            "/settings/save",
            data={
                "_csrf": csrf(client),
                "str:bot.language": "en",
                "lines:bot.topics": "Erstes Thema\r\nZweites Thema",
                "int:engagement.limits.like_per_day": "42",
                "intlist:posting.active_hours": ["9", "20"],
                "intlist:posting.active_weekdays": ["", "0", "2"],
                "bool:engagement.enabled": ["0"],
            },
            follow_redirects=True,
        )
        config = load_config(workdir / "config.yaml")
        assert config.bot.language == "en"
        assert config.bot.topics == ("Erstes Thema", "Zweites Thema")
        assert config.engagement.limits.like_per_day == 42
        assert config.posting.active_hours == (9, 20)
        assert config.posting.active_weekdays == (0, 2)
        assert config.engagement.enabled is False

    def test_ungueltige_eingabe_laesst_die_datei_unveraendert(self, client, workdir):
        vorher = (workdir / "config.yaml").read_text(encoding="utf-8")
        response = client.post(
            "/settings/save",
            data={"_csrf": csrf(client), "intlist:posting.active_hours": ["22", "8"]},
            follow_redirects=True,
        )
        assert "kleiner als Endstunde" in response.get_data(as_text=True)
        assert (workdir / "config.yaml").read_text(encoding="utf-8") == vorher

    def test_kommentare_bleiben_erhalten(self, client, workdir):
        """Die config.yaml ist durchkommentiert - das soll sie bleiben."""
        def kommentare(text: str) -> list[str]:
            return [z.strip() for z in text.splitlines() if z.strip().startswith("#")]

        vorher = kommentare((workdir / "config.yaml").read_text(encoding="utf-8"))
        client.post(
            "/settings/save",
            data={
                "_csrf": csrf(client),
                "lines:bot.topics": "Nur ein Thema",
                "csv:posting.hashtag_pool": "#eins, #zwei",
                "lines:filters.blocked_keywords": "spam",
            },
            follow_redirects=True,
        )
        nachher = kommentare((workdir / "config.yaml").read_text(encoding="utf-8"))
        assert sorted(vorher) == sorted(nachher)

    def test_sicherungskopie(self, client, workdir):
        client.post("/settings/save", data={"_csrf": csrf(client), "str:bot.language": "en"},
                    follow_redirects=True)
        assert (workdir / "config.yaml.bak").exists()

    def test_rohes_yaml(self, client, workdir):
        text = (workdir / "config.yaml").read_text(encoding="utf-8").replace('language: "de"', 'language: "fr"')
        client.post("/settings/raw", data={"_csrf": csrf(client), "raw": text}, follow_redirects=True)
        assert load_config(workdir / "config.yaml").bot.language == "fr"

    def test_kaputtes_rohes_yaml_wird_abgelehnt(self, client, workdir):
        vorher = (workdir / "config.yaml").read_text(encoding="utf-8")
        response = client.post(
            "/settings/raw", data={"_csrf": csrf(client), "raw": "bot: [offen"}, follow_redirects=True
        )
        assert "YAML" in response.get_data(as_text=True)
        assert (workdir / "config.yaml").read_text(encoding="utf-8") == vorher


class TestRegeln:
    def _anlegen(self, client, **overrides):
        data = {
            "_csrf": csrf(client),
            "index": "",
            "str:rule.name": "Browserregel",
            "tags:rule.hashtags": "flask, #webdev",
            "str:rule.match": "any",
            "strlist:rule.actions": ["", "like", "repost"],
            "csv:rule.languages": "de",
            "int:rule.min_likes": "5",
            "int:rule.min_reposts": "0",
            "float:rule.weight": "2.0",
            "text:rule.reply_instruction": "",
        }
        data.update(overrides)
        return client.post("/rules/save", data=data, follow_redirects=True)

    def test_anlegen(self, client, workdir):
        self._anlegen(client)
        rule = load_config(workdir / "config.yaml").rules[-1]
        assert rule.name == "Browserregel"
        assert rule.hashtags == ("#flask", "#webdev")
        assert rule.actions == ("like", "repost")
        assert rule.min_likes == 5 and rule.weight == 2.0

    def test_aendern(self, client, workdir):
        self._anlegen(client)
        index = len(load_config(workdir / "config.yaml").rules) - 1
        self._anlegen(client, index=str(index), **{"str:rule.name": "Umbenannt", "str:rule.match": "all"})
        rule = load_config(workdir / "config.yaml").rules[index]
        assert rule.name == "Umbenannt" and rule.match == "all"

    def test_loeschen(self, client, workdir):
        self._anlegen(client)
        vorher = len(load_config(workdir / "config.yaml").rules)
        client.post(
            "/rules/delete",
            data={"_csrf": csrf(client), "index": str(vorher - 1)},
            follow_redirects=True,
        )
        assert len(load_config(workdir / "config.yaml").rules) == vorher - 1

    def test_ohne_aktion_wird_abgelehnt(self, client, workdir):
        vorher = len(load_config(workdir / "config.yaml").rules)
        response = self._anlegen(client, **{"strlist:rule.actions": [""]})
        assert "mindestens eine Aktion" in response.get_data(as_text=True)
        assert len(load_config(workdir / "config.yaml").rules) == vorher

    def test_ohne_hashtag_wird_abgelehnt(self, client):
        response = self._anlegen(client, **{"tags:rule.hashtags": ""})
        assert "mindestens einen Hashtag" in response.get_data(as_text=True)

    def test_unbekannter_index(self, client):
        response = client.post(
            "/rules/delete", data={"_csrf": csrf(client), "index": "999"}, follow_redirects=True
        )
        assert "gibt es nicht mehr" in response.get_data(as_text=True)


class TestZugangsdaten:
    def test_speichern_und_maskieren(self, client, workdir):
        client.post(
            "/setup/credentials",
            data={"_csrf": csrf(client), "X_API_KEY": "geheimer-schluessel", "X_API_SECRET": ""},
            follow_redirects=True,
        )
        inhalt = (workdir / ".env").read_text(encoding="utf-8")
        assert "X_API_KEY=geheimer-schluessel" in inhalt
        # Der Wert taucht nie wieder in der Oberflaeche auf.
        seite = client.get("/setup").get_data(as_text=True)
        assert "geheimer-schluessel" not in seite
        assert "gesetzt" in seite

    def test_dateirechte(self, client, workdir):
        client.post(
            "/setup/credentials",
            data={"_csrf": csrf(client), "X_API_KEY": "abc"},
            follow_redirects=True,
        )
        assert oct((workdir / ".env").stat().st_mode & 0o777) == "0o600"

    def test_leeres_feld_laesst_den_wert_stehen(self, client, workdir):
        token = csrf(client)
        client.post("/setup/credentials", data={"_csrf": token, "X_API_KEY": "erst"}, follow_redirects=True)
        client.post("/setup/credentials", data={"_csrf": token, "X_API_KEY": ""}, follow_redirects=True)
        assert "X_API_KEY=erst" in (workdir / ".env").read_text(encoding="utf-8")


class TestInhalte:
    def test_vorlagen_speichern(self, client, workdir):
        text = "variables:\n  ding: ['A']\nposts:\n  - 'Ein {ding}'\nreplies:\n  - 'Hallo'\n"
        client.post("/content/save", data={"_csrf": csrf(client), "raw": text}, follow_redirects=True)
        assert (workdir / "content" / "templates.yaml").read_text(encoding="utf-8") == text

    def test_kaputte_vorlagen_werden_abgelehnt(self, client, workdir):
        pfad = workdir / "content" / "templates.yaml"
        vorher = pfad.read_text(encoding="utf-8")
        response = client.post(
            "/content/save",
            data={"_csrf": csrf(client), "raw": "posts:\n  - 'Text mit {unbekannt}'\n"},
            follow_redirects=True,
        )
        assert "unbekannte Platzhalter" in response.get_data(as_text=True)
        assert pfad.read_text(encoding="utf-8") == vorher


class TestStatusUndProtokoll:
    def test_status_enthaelt_alles_fuer_die_anzeige(self, client):
        data = client.get("/api/status").get_json()
        for key in ("running", "dry_run", "rules", "hashtags", "jobs", "snapshot"):
            assert key in data
        snapshot = data["snapshot"]
        assert set(snapshot["today"]) == {"post", "like", "repost", "reply"}
        assert len(snapshot["history"]) == 14

    def test_protokoll_endpunkt(self, client):
        data = client.get("/api/logs?lines=50").get_json()
        assert "lines" in data and isinstance(data["lines"], list)

    def test_aktivitaet_filtert(self, client, workdir):
        from xbot.state import Store

        with Store(load_config(workdir / "config.yaml").storage.database) as store:
            store.record_action("like", target_id="1", target_author="alice")
            store.record_action("post", text="Ein Beitrag")

        alle = client.get("/activity").get_data(as_text=True)
        assert "alice" in alle and "Ein Beitrag" in alle

        nur_posts = client.get("/activity?action=post").get_data(as_text=True)
        assert "Ein Beitrag" in nur_posts and "alice" not in nur_posts


# ---------------------------------------------------------------------------
# Helfer ohne Webserver
# ---------------------------------------------------------------------------
class TestHelfer:
    def test_protokoll_ende_lesen(self, tmp_path):
        from xbot.web.support import tail_file

        pfad = tmp_path / "x.log"
        pfad.write_text("\n".join(f"Zeile {i}" for i in range(500)), encoding="utf-8")
        assert tail_file(pfad, 3) == ["Zeile 497", "Zeile 498", "Zeile 499"]
        assert tail_file(tmp_path / "fehlt.log") == []

    def test_grosse_datei_wird_nicht_ganz_gelesen(self, tmp_path):
        from xbot.web.support import tail_file

        pfad = tmp_path / "gross.log"
        pfad.write_text("x" * 2_000_000 + "\nLETZTE", encoding="utf-8")
        assert tail_file(pfad, 1) == ["LETZTE"]

    @pytest.mark.parametrize(
        "sekunden, erwartet",
        [(-5, "jetzt"), (30, "30 Sek."), (720, "12 Min."), (11520, "3 Std. 12 Min."), (190800, "2 Tg. 5 Std.")],
    )
    def test_restzeit_formatieren(self, sekunden, erwartet):
        from datetime import datetime, timedelta, timezone

        from xbot.web.support import humanize_delta

        jetzt = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        assert erwartet in humanize_delta(jetzt + timedelta(seconds=sekunden), now=jetzt)

    def test_warnung_bei_oeffentlicher_bindung(self):
        from xbot.web.server import warn_if_public

        assert warn_if_public("127.0.0.1", False) == []
        hinweise = warn_if_public("0.0.0.0", True)
        assert len(hinweise) == 3
        assert any("HTTPS" in h for h in hinweise)
        assert any("XBOT_WEB_PASSWORD" in h for h in hinweise)

    def test_passwort_wird_erzeugt_wenn_keines_gesetzt_ist(self, monkeypatch):
        from xbot.web.app import resolve_password

        monkeypatch.delenv("XBOT_WEB_PASSWORD", raising=False)
        passwort, erzeugt = resolve_password()
        assert erzeugt is True and len(passwort) >= 12
        assert resolve_password("eigenes") == ("eigenes", False)

    def test_zeitpunkt_in_ortszeit(self, config):
        from datetime import datetime, timezone

        from xbot.web.support import format_local

        moment = datetime(2026, 6, 15, 10, 30, tzinfo=timezone.utc)
        # Europe/Berlin liegt im Juni zwei Stunden vor UTC.
        assert format_local(moment, config, "%H:%M") == "12:30"
        assert format_local(None, config) == "-"

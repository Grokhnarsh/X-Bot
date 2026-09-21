"""Was am Ende gesendet wird, entscheidet sich hier."""

from __future__ import annotations

import pytest

from xbot.content.text import (
    append_hashtags,
    extract_hashtags,
    is_duplicate,
    normalise_for_comparison,
    sanitize,
    similarity,
    truncate,
    tweet_length,
)


class TestLaenge:
    def test_urls_zaehlen_pauschal_23_zeichen(self):
        kurz = tweet_length("Siehe https://x.de")
        lang = tweet_length("Siehe https://sehr-lange-domain.example.com/ein/langer/pfad")
        assert kurz == lang == len("Siehe ") + 23

    def test_umlaute_zaehlen_einfach(self):
        assert tweet_length("Grüße") == 5


class TestSanitize:
    @pytest.mark.parametrize(
        "roh, erwartet",
        [
            ('Hier ist ein Vorschlag:\n"Ein Beitrag."', "Ein Beitrag."),
            ("Post: Direkt am Anfang", "Direkt am Anfang"),
            ("Hier kommt der Beitrag: Inhalt", "Inhalt"),
            ("```\nCode-Block\n```", "Code-Block"),
            ("```text\nMit Sprache\n```", "Mit Sprache"),
            ('"Nur in Anfuehrungszeichen"', "Nur in Anfuehrungszeichen"),
            ("Zeile\n\n\n\nZeile", "Zeile\n\nZeile"),
            ("  Viel   Leerraum  ", "Viel Leerraum"),
            ("", ""),
        ],
    )
    def test_saeubert(self, roh, erwartet):
        assert sanitize(roh) == erwartet

    @pytest.mark.parametrize(
        "text",
        [
            "Drei Dinge:\n- a\n- b",
            "Merke: Tests zuerst.",
            "Fazit nach zwei Jahren: es lohnt sich.",
            'Er sagte "Hallo" und ging.',
        ],
    )
    def test_laesst_echten_inhalt_unangetastet(self, text):
        assert sanitize(text) == text


class TestTruncate:
    def test_kurzer_text_bleibt(self):
        assert truncate("Passt.", 40) == "Passt."

    def test_schneidet_am_satzende(self):
        text = "Erster Satz ist fertig. Zweiter Satz laeuft noch weiter und weiter."
        assert truncate(text, 40) == "Erster Satz ist fertig."

    def test_schneidet_am_wortende_mit_auslassung(self):
        text = "wort " * 40
        gekuerzt = truncate(text, 30)
        assert gekuerzt.endswith("…")
        assert tweet_length(gekuerzt) <= 30

    def test_haelt_grenze_auch_ohne_leerzeichen(self):
        gekuerzt = truncate("A" * 100, 20)
        assert tweet_length(gekuerzt) <= 20

    def test_grenze_null(self):
        assert truncate("egal", 0) == ""


class TestAehnlichkeit:
    def test_identisch(self):
        assert similarity("Tests sind wichtig", "Tests sind wichtig") == 1.0

    def test_ignoriert_hashtags_und_satzzeichen(self):
        assert similarity("Tests sind wichtig! #python", "tests sind wichtig") == 1.0

    def test_verschieden(self):
        assert similarity("Tests sind wichtig", "Docker startet Container") < 0.5

    def test_leer(self):
        assert similarity("", "irgendwas") == 0.0

    def test_normalisierung_entfernt_urls(self):
        assert "http" not in normalise_for_comparison("Siehe https://x.de dazu")

    def test_duplikat_erkennung(self):
        verlauf = ["Tests sind wichtig fuer guten Code"]
        assert is_duplicate("Tests sind wichtig fuer guten Code!", verlauf, 0.75) is True
        assert is_duplicate("Docker startet Container schnell", verlauf, 0.75) is False

    def test_leerer_verlauf(self):
        assert is_duplicate("irgendwas", [], 0.75) is False

    def test_schwelle_eins_verlangt_gleichheit(self):
        assert is_duplicate("Test a", ["Test b"], 1.0) is False
        assert is_duplicate("Test a!", ["test a"], 1.0) is True


class TestHashtags:
    def test_haengt_an(self):
        assert append_hashtags("Kurz.", ["#Python", "#KI"], max_chars=280, max_tags=2) == "Kurz.\n\n#Python #KI"

    def test_ueberspringt_vorhandene(self):
        ergebnis = append_hashtags("Mit #python drin.", ["#Python", "#KI"], max_chars=280, max_tags=2)
        assert ergebnis == "Mit #python drin.\n\n#KI"

    def test_achtet_auf_die_laenge(self):
        text = "X" * 276
        assert append_hashtags(text, ["#Python"], max_chars=280, max_tags=2) == text

    def test_nimmt_was_passt(self):
        # 270 + "\n\n#Python" = 279 passt gerade noch, " #KI" waere zu viel.
        ergebnis = append_hashtags("X" * 270, ["#Python", "#KI"], max_chars=280, max_tags=2)
        assert tweet_length(ergebnis) <= 280
        assert "#Python" in ergebnis and "#KI" not in ergebnis

    def test_null_tags(self):
        assert append_hashtags("Kurz.", ["#a"], max_chars=280, max_tags=0) == "Kurz."

    def test_ergaenzt_rautezeichen(self):
        assert append_hashtags("Kurz.", ["Python"], max_chars=280, max_tags=1) == "Kurz.\n\n#Python"

    def test_extrahiert(self):
        assert extract_hashtags("Ein #Test mit #ZWEI tags") == {"#test", "#zwei"}

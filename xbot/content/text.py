"""Textwerkzeuge: saeubern, kuerzen, Wiederholungen erkennen.

Alles hier sind reine Funktionen ohne Netzwerkzugriff - dadurch laesst sich
die heikelste Stelle des Bots (was am Ende wirklich gesendet wird) vollstaendig
testen.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, Sequence

#: X zaehlt jede URL pauschal als 23 Zeichen (t.co-Verkuerzung).
URL_WEIGHT = 23
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

#: Einleitungen, die Sprachmodelle gern voranstellen ("Hier ist ein Vorschlag:").
#: Bewusst an Schluesselwoerter gebunden, damit eine echte Aufzaehlungs-
#: Einleitung wie "Drei Dinge:" unangetastet bleibt.
PREAMBLE_PATTERN = re.compile(
    r"^\s*(?:hier (?:ist|sind|w[aä]e?re|kommt)|vorschlag|beitrag|post|tweet|entwurf|option\s*\d*)"
    r"\b[^\n:]{0,40}[:–-]\s*",
    re.IGNORECASE,
)

QUOTE_PAIRS = (
    ('"', '"'),
    ("'", "'"),
    ("„", "“"),
    ("“", "”"),
    ("«", "»"),
)

#: Ab diesem Anteil der verfuegbaren Laenge lohnt es sich, am Satzende zu
#: kuerzen statt mitten im Satz.
SENTENCE_CUT_RATIO = 0.5


def tweet_length(text: str) -> int:
    """Laenge so, wie X sie zaehlt - URLs pauschal mit 23 Zeichen."""
    without_urls = URL_PATTERN.sub("", text)
    url_count = len(URL_PATTERN.findall(text))
    # Unicode-Normalisierung, damit zusammengesetzte Umlaute nicht doppelt zaehlen.
    normalised = unicodedata.normalize("NFC", without_urls)
    return len(normalised) + url_count * URL_WEIGHT


def strip_wrapping_quotes(text: str) -> str:
    """Entfernt Anfuehrungszeichen, die den gesamten Text umschliessen."""
    text = text.strip()
    for opening, closing in QUOTE_PAIRS:
        if len(text) >= 2 and text.startswith(opening) and text.endswith(closing):
            inner = text[1:-1].strip()
            # Nur entfernen, wenn es wirklich eine Klammerung war und nicht
            # ein Zitat innerhalb des Textes.
            if opening not in inner and closing not in inner:
                return inner
    return text


def sanitize(raw: str) -> str:
    """Macht aus einer Modellantwort einen sendefaehigen Beitrag."""
    if not raw:
        return ""

    text = raw.strip()

    # Markdown-Codeblock entfernen, falls das Modell einen gesetzt hat.
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Eine einleitende Zeile wie "Hier ist ein Vorschlag:" verwerfen.
    lines = text.splitlines()
    if len(lines) > 1 and len(lines[0]) < 80 and PREAMBLE_PATTERN.match(lines[0]):
        text = "\n".join(lines[1:]).strip()
    else:
        text = PREAMBLE_PATTERN.sub("", text, count=1).strip()

    text = strip_wrapping_quotes(text)

    # Zeilenenden saeubern, hoechstens eine Leerzeile am Stueck.
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip()


def truncate(text: str, max_chars: int) -> str:
    """Kuerzt auf die X-Laenge, moeglichst an einer Satz- oder Wortgrenze."""
    if max_chars <= 0:
        return ""
    if tweet_length(text) <= max_chars:
        return text

    # Zeichenweise zurueckgehen, bis die gewichtete Laenge passt.
    cut = text
    while cut and tweet_length(cut) > max_chars:
        cut = cut[:-1]

    # Bevorzugt am Satzende abschneiden, wenn dabei nicht zu viel verloren geht.
    sentence_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sentence_end == -1 and cut and cut[-1] in ".!?":
        sentence_end = len(cut) - 1
    if sentence_end > len(cut) * SENTENCE_CUT_RATIO:
        return cut[: sentence_end + 1].strip()

    # Sonst an der letzten Wortgrenze, mit Auslassungszeichen.
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    cut = cut.rstrip(" ,;:-–")
    while cut and tweet_length(cut + "…") > max_chars:
        cut = cut[:-1].rstrip()
    return (cut + "…") if cut else ""


def normalise_for_comparison(text: str) -> str:
    """Vergleichsform: ohne Hashtags, URLs, Satzzeichen und Gross-/Kleinschreibung."""
    text = URL_PATTERN.sub(" ", text.lower())
    text = re.sub(r"[#@]\w+", " ", text, flags=re.UNICODE)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def similarity(left: str, right: str) -> float:
    """0.0 (voellig verschieden) bis 1.0 (identisch)."""
    a = normalise_for_comparison(left)
    b = normalise_for_comparison(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def is_duplicate(text: str, history: Sequence[str], threshold: float = 0.75) -> bool:
    """Ist der Text einem der letzten Beitraege zu aehnlich?"""
    if not text or not history:
        return False
    if threshold >= 1.0:
        normalised = normalise_for_comparison(text)
        return any(normalise_for_comparison(old) == normalised for old in history)
    return any(similarity(text, old) >= threshold for old in history)


def extract_hashtags(text: str) -> set[str]:
    return {match.lower() for match in re.findall(r"#\w+", text, flags=re.UNICODE)}


def append_hashtags(text: str, tags: Iterable[str], *, max_chars: int, max_tags: int = 2) -> str:
    """Haengt Hashtags in einer eigenen Zeile an, soweit die Laenge das zulaesst."""
    if max_tags <= 0:
        return text

    present = extract_hashtags(text)
    candidates: list[str] = []
    for tag in tags:
        tag = tag.strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = f"#{tag}"
        lowered = tag.lower()
        if lowered in present or lowered in {c.lower() for c in candidates}:
            continue
        candidates.append(tag)
        if len(candidates) >= max_tags:
            break

    if not candidates:
        return text

    accepted: list[str] = []
    result = text
    for tag in candidates:
        candidate = "{}\n\n{}".format(text, " ".join([*accepted, tag]))
        if tweet_length(candidate) <= max_chars:
            accepted.append(tag)
            result = candidate
    return result

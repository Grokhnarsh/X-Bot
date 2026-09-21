"""Konfiguration und Zugangsdaten aus dem Web bearbeiten.

Zwei Regeln bestimmen diese Schicht:

1. **Nie eine kaputte Datei hinterlassen.** Jede Änderung wird erst gegen
   ``Config.parse`` geprüft und dann atomar geschrieben (Temporaerdatei und
   Umbenennen), mit einer Sicherungskopie der vorherigen Fassung.
2. **Kommentare bleiben erhalten.** Die ``config.yaml`` ist durchkommentiert und
   soll das nach einer Bearbeitung im Browser auch bleiben. Dafuer sorgt der
   Round-Trip-Modus von ruamel.yaml.
"""

from __future__ import annotations

import io
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableSequence

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from ..config import Config, Credentials
from ..errors import ConfigError

#: Reihenfolge und Beschreibung der Zugangsdaten in der Oberfläche.
ENV_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("X_API_KEY", "API Key", "X Developer Portal -> Keys and tokens -> API Key"),
    ("X_API_SECRET", "API Key Secret", "gehört zum API Key"),
    ("X_ACCESS_TOKEN", "Access Token", "NACH dem Umstellen auf 'Read and write' erzeugen"),
    ("X_ACCESS_TOKEN_SECRET", "Access Token Secret", "gehört zum Access Token"),
    ("X_BEARER_TOKEN", "Bearer Token", "für die Hashtag-Suche"),
    ("ANTHROPIC_API_KEY", "Anthropic API Key", "optional - ohne Key werden Vorlagen genutzt"),
)

#: Eintrag, der aus der .env entfernt werden soll.
CLEAR = "__CLEAR__"


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096          # keine automatischen Zeilenumbrueche in langen Texten
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _atomic_write(path: Path, text: str, *, mode: int | None = None) -> None:
    """Erst vollständig schreiben, dann umbenennen - nie eine halbe Datei."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# config.yaml
# ---------------------------------------------------------------------------
class ConfigFile:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # -- Lesen --------------------------------------------------------------
    def read_text(self) -> str:
        if not self.path.exists():
            raise ConfigError(f"Konfigurationsdatei '{self.path}' nicht gefunden.")
        return self.path.read_text(encoding="utf-8")

    def load_tree(self) -> Any:
        """YAML als bearbeitbarer Baum, samt Kommentaren."""
        try:
            with self.path.open(encoding="utf-8") as fh:
                tree = _yaml().load(fh)
        except FileNotFoundError:
            raise ConfigError(f"Konfigurationsdatei '{self.path}' nicht gefunden.") from None
        except YAMLError as exc:
            raise ConfigError(f"'{self.path}' ist kein gültiges YAML: {exc}") from None
        if tree is None:
            raise ConfigError(f"'{self.path}' ist leer.")
        return tree

    @staticmethod
    def dump_tree(tree: Any) -> str:
        buffer = io.StringIO()
        _yaml().dump(tree, buffer)
        return buffer.getvalue()

    # -- Schreiben ----------------------------------------------------------
    def save_tree(self, tree: Any) -> Config:
        """Baum prüfen und schreiben. Bei einem Fehler bleibt die Datei, wie sie war."""
        config = Config.parse(tree, credentials=Credentials.from_env())
        self._backup()
        _atomic_write(self.path, self.dump_tree(tree))
        return config

    def save_text(self, text: str) -> Config:
        """Rohen YAML-Text prüfen und schreiben."""
        try:
            tree = _yaml().load(text)
        except YAMLError as exc:
            raise ConfigError(f"Kein gültiges YAML: {exc}") from None
        if tree is None:
            raise ConfigError("Der Text ist leer.")
        return self.save_tree(tree)

    def apply(self, changes: Mapping[str, Any]) -> Config:
        """Verschachtelte Änderungen einpflegen, ohne den Rest anzufassen.

        ``changes`` ist nach Abschnitten geordnet, etwa
        ``{"bot": {"language": "en"}, "engagement": {"limits": {"like_per_day": 40}}}``.
        Listen werden komplett ersetzt, Einzelwerte an Ort und Stelle getauscht -
        so bleibt der Kommentar über der Zeile erhalten.
        """
        tree = self.load_tree()
        _merge(tree, changes)
        return self.save_tree(tree)

    def _backup(self) -> None:
        if self.path.exists():
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            backup.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")


@contextmanager
def preserve_trailing_comment(seq: Any) -> Iterator[Any]:
    """Haelt den Kommentarblock fest, der einer Liste folgt.

    ruamel hängt einen Kommentar, der nach einer Liste steht, an deren
    letzten Eintrag. Wird die Liste ersetzt oder gekuerzt, verschwindet damit
    die Abschnittsueberschrift des nächsten Blocks - in einer
    durchkommentierten config.yaml fällt das sofort auf. Deshalb wird der
    Kommentar vor der Änderung abgenommen und danach wieder an den neuen
    letzten Eintrag gehaengt.
    """
    comments = getattr(seq, "ca", None)
    trailing = None
    if comments is not None and len(seq):
        trailing = comments.items.pop(len(seq) - 1, None)
    try:
        yield seq
    finally:
        if trailing is not None and comments is not None and len(seq):
            comments.items[len(seq) - 1] = trailing


def _merge(target: Any, changes: Mapping[str, Any]) -> None:
    for key, value in changes.items():
        if isinstance(value, Mapping):
            branch = target.get(key)
            if not isinstance(branch, Mapping):
                branch = {}
                target[key] = branch
            _merge(branch, value)
        elif isinstance(value, list) and isinstance(target.get(key), MutableSequence):
            # In der vorhandenen Liste arbeiten, damit ihr Kommentar bleibt.
            existing = target[key]
            with preserve_trailing_comment(existing):
                existing.clear()
                existing.extend(value)
        else:
            target[key] = value


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EnvField:
    key: str
    label: str
    hint: str
    is_set: bool
    length: int


class EnvFile:
    """Verwaltet die ``.env``.

    Gespeicherte Werte werden nie an die Oberfläche zurueckgegeben - angezeigt
    wird nur, ob ein Wert gesetzt ist und wie lang er ist.
    """

    def __init__(self, path: str | Path = ".env") -> None:
        self.path = Path(path)

    def read(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if not self.path.exists():
            return values
        for line in self.path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    def fields(self, fields: Iterable[tuple[str, str, str]] = ENV_FIELDS) -> list[EnvField]:
        stored = self.read()
        out: list[EnvField] = []
        for key, label, hint in fields:
            # Die Umgebung sticht die Datei - so wie beim Bot selbst.
            value = os.environ.get(key) or stored.get(key, "")
            out.append(EnvField(key=key, label=label, hint=hint, is_set=bool(value), length=len(value)))
        return out

    def update(self, values: Mapping[str, str]) -> list[str]:
        """Werte setzen oder löschen. Ein leerer Wert lässt den bisherigen stehen.

        Gibt die Namen der geaenderten Schlüssel zurück.
        """
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
        changed: list[str] = []

        for key, raw in values.items():
            new_value = raw.strip()
            if not new_value:
                continue                       # unverändert lassen
            if new_value == CLEAR:
                lines = [line for line in lines if not _is_assignment(line, key)]
                os.environ.pop(key, None)
                changed.append(key)
                continue

            replaced = False
            for index, line in enumerate(lines):
                if _is_assignment(line, key):
                    lines[index] = f"{key}={new_value}"
                    replaced = True
                    break
            if not replaced:
                lines.append(f"{key}={new_value}")
            # Damit die Änderung ohne Neustart greift.
            os.environ[key] = new_value
            changed.append(key)

        if changed:
            text = "\n".join(lines).rstrip("\n") + "\n"
            # Die Datei enthaelt Geheimnisse: nur der Eigentuemer darf sie lesen.
            _atomic_write(self.path, text, mode=stat.S_IRUSR | stat.S_IWUSR)
        return changed


def _is_assignment(line: str, key: str) -> bool:
    stripped = line.strip()
    return not stripped.startswith("#") and stripped.split("=", 1)[0].strip() == key

"""Textbausteine aus YAML.

Der Template-Weg ist der Rueckfallweg des Bots: er braucht keinen API-Key,
keine Netzverbindung und liefert immer ein Ergebnis.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Mapping, Sequence

import yaml

from ..errors import ContentError

_FORMATTER = Formatter()


def _placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in _FORMATTER.parse(template) if field}


@dataclass(frozen=True)
class TemplateLibrary:
    posts: tuple[str, ...] = ()
    replies: tuple[str, ...] = ()
    variables: Mapping[str, tuple[str, ...]] = None  # type: ignore[assignment]
    source: Path | None = None

    def __post_init__(self) -> None:
        if self.variables is None:
            object.__setattr__(self, "variables", {})

    # -- Laden --------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "TemplateLibrary":
        path = Path(path)
        if not path.exists():
            raise ContentError(
                f"Vorlagendatei '{path}' nicht gefunden. Lege sie an oder setze "
                "content.provider auf 'ai'."
            )
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ContentError(f"'{path}' ist kein gueltiges YAML: {exc}") from None
        if not isinstance(data, Mapping):
            raise ContentError(f"'{path}' muss ein YAML-Objekt enthalten.")

        def _texts(key: str) -> tuple[str, ...]:
            items = data.get(key) or []
            if isinstance(items, str):
                items = [items]
            if not isinstance(items, Sequence):
                raise ContentError(f"'{path}' -> {key}: erwartet eine Liste von Texten.")
            return tuple(str(item).strip() for item in items if str(item).strip())

        raw_vars = data.get("variables") or {}
        if not isinstance(raw_vars, Mapping):
            raise ContentError(f"'{path}' -> variables: erwartet ein Objekt.")
        variables: dict[str, tuple[str, ...]] = {}
        for name, values in raw_vars.items():
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, Sequence):
                raise ContentError(f"'{path}' -> variables.{name}: erwartet eine Liste.")
            options = tuple(str(v).strip() for v in values if str(v).strip())
            if options:
                variables[str(name)] = options

        library = cls(posts=_texts("posts"), replies=_texts("replies"), variables=variables, source=path)
        library.validate()
        return library

    def validate(self) -> None:
        """Prueft beim Laden, ob jeder Platzhalter aufloesbar ist."""
        known = set(self.variables)
        for kind, templates in (("posts", self.posts), ("replies", self.replies)):
            for index, template in enumerate(templates):
                unknown = _placeholders(template) - known
                if unknown:
                    raise ContentError(
                        f"{self.source or 'Vorlagen'} -> {kind}[{index}]: unbekannte Platzhalter "
                        f"{sorted(unknown)}. Ergaenze sie unter 'variables:'."
                    )

    # -- Erzeugen -----------------------------------------------------------
    def render(self, kind: str = "posts", *, rng: random.Random | None = None) -> str:
        templates = self.posts if kind == "posts" else self.replies
        if not templates:
            raise ContentError(
                f"Keine Vorlagen unter '{kind}' in {self.source or 'der Vorlagendatei'} gefunden."
            )
        rng = rng or random
        template = rng.choice(list(templates))
        values = {name: rng.choice(list(options)) for name, options in self.variables.items()}
        try:
            return template.format(**values).strip()
        except (KeyError, IndexError) as exc:
            raise ContentError(f"Vorlage konnte nicht gefuellt werden: {exc}") from None

    def render_post(self, *, rng: random.Random | None = None) -> str:
        return self.render("posts", rng=rng)

    def render_reply(self, *, rng: random.Random | None = None) -> str:
        return self.render("replies", rng=rng)

    def __len__(self) -> int:
        return len(self.posts) + len(self.replies)

    # -- Gezielter Zugriff fuer die Dublettensuche --------------------------
    def count(self, kind: str = "posts") -> int:
        return len(self.posts if kind == "posts" else self.replies)

    def render_index(self, kind: str, index: int, *, rng: random.Random | None = None) -> str:
        """Rendert genau eine Vorlage - erlaubt es, alle der Reihe nach zu probieren."""
        templates = self.posts if kind == "posts" else self.replies
        if not templates:
            raise ContentError(f"Keine Vorlagen unter '{kind}' vorhanden.")
        rng = rng or random
        template = templates[index % len(templates)]
        values = {name: rng.choice(list(options)) for name, options in self.variables.items()}
        try:
            return template.format(**values).strip()
        except (KeyError, IndexError) as exc:
            raise ContentError(f"Vorlage konnte nicht gefuellt werden: {exc}") from None

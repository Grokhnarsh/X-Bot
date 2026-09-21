"""Formulardaten in Konfigurationsaenderungen uebersetzen.

Der Typ eines Feldes steht im Feldnamen selbst: ``int:posting.interval_minutes``
bedeutet "ganze Zahl, gehört nach ``posting.interval_minutes``". Dadurch gibt es
keine zweite Liste, die mit den Vorlagen auseinanderlaufen könnte - Vorlage und
Auswertung können sich gar nicht widersprechen.

Unterstuetzte Typen:

==========  ===================================================================
``str``     einzeilige Zeichenkette
``text``    mehrzeiliger Text
``int``     ganze Zahl
``float``   Kommazahl
``bool``    Schalter (im Markup mit verstecktem ``0`` davor)
``lines``   Liste, eine Zeile je Eintrag
``csv``     Liste, durch Komma getrennt
``tags``    Liste von Hashtags, durch Komma, Leerzeichen oder Zeile getrennt
``intlist`` Liste ganzer Zahlen (mehrere Felder gleichen Namens)
``strlist`` Liste von Zeichenketten (mehrere Felder gleichen Namens)
==========  ===================================================================
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from ..errors import ConfigError

SEPARATORS = re.compile(r"[,\s]+")


def _coerce(kind: str, values: Sequence[str], name: str) -> Any:
    last = values[-1] if values else ""

    if kind == "str":
        return last.strip()
    if kind == "text":
        # Zeilenumbrueche bleiben, nur Rand wird entfernt.
        return last.replace("\r\n", "\n").strip()
    if kind == "int":
        try:
            return int(str(last).strip())
        except ValueError:
            raise ConfigError(f"'{name}': '{last}' ist keine ganze Zahl.") from None
    if kind == "float":
        try:
            return float(str(last).strip().replace(",", "."))
        except ValueError:
            raise ConfigError(f"'{name}': '{last}' ist keine Zahl.") from None
    if kind == "bool":
        return str(last).strip().lower() in ("1", "true", "on", "yes", "ja")
    if kind == "lines":
        return [line.strip() for line in last.replace("\r\n", "\n").split("\n") if line.strip()]
    if kind == "csv":
        return [part.strip() for part in last.split(",") if part.strip()]
    if kind == "tags":
        parts = [part.strip() for part in SEPARATORS.split(last) if part.strip()]
        return [part if part.startswith("#") else f"#{part}" for part in parts]
    if kind == "intlist":
        out: list[int] = []
        for value in values:
            value = str(value).strip()
            if not value:
                continue
            try:
                out.append(int(value))
            except ValueError:
                raise ConfigError(f"'{name}': '{value}' ist keine ganze Zahl.") from None
        return out
    if kind == "strlist":
        return [str(value).strip() for value in values if str(value).strip()]

    raise ConfigError(f"Unbekannter Feldtyp '{kind}' bei '{name}'.")


def _assign(tree: dict[str, Any], path: str, value: Any) -> None:
    parts = [part for part in path.split(".") if part]
    if not parts:
        return
    branch = tree
    for part in parts[:-1]:
        branch = branch.setdefault(part, {})
        if not isinstance(branch, dict):
            raise ConfigError(f"Widersprüchlicher Pfad '{path}'.")
    branch[parts[-1]] = value


def parse_form(form: Mapping[str, Any]) -> dict[str, Any]:
    """Alle typisierten Felder eines Formulars zu einem Aenderungsbaum."""
    changes: dict[str, Any] = {}
    getlist = getattr(form, "getlist", None)

    for raw_name in form.keys():
        if ":" not in raw_name:
            continue                         # z. B. _csrf oder Schaltflaechen
        kind, _, path = raw_name.partition(":")
        if not path:
            continue
        values = list(getlist(raw_name)) if getlist else [form[raw_name]]
        _assign(changes, path, _coerce(kind, values, path))

    return changes


def parse_rule_form(form: Mapping[str, Any]) -> dict[str, Any]:
    """Ein einzelnes Regelobjekt aus dem Regelformular."""
    parsed = parse_form(form)
    rule = parsed.get("rule", {})
    if not isinstance(rule, dict):
        raise ConfigError("Das Regelformular war unvollständig.")

    name = str(rule.get("name", "")).strip()
    if not name:
        raise ConfigError("Die Regel braucht einen Namen.")
    if not rule.get("hashtags"):
        raise ConfigError("Die Regel braucht mindestens einen Hashtag.")
    if not rule.get("actions"):
        raise ConfigError("Die Regel braucht mindestens eine Aktion.")

    # Nur gesetzte Felder uebernehmen - so bleibt die YAML schlank.
    out: dict[str, Any] = {
        "name": name,
        "hashtags": rule["hashtags"],
        "match": rule.get("match") or "any",
        "actions": rule["actions"],
    }
    if rule.get("languages"):
        out["languages"] = rule["languages"]
    for key in ("min_likes", "min_reposts"):
        if rule.get(key):
            out[key] = rule[key]
    if rule.get("weight") is not None and float(rule["weight"]) != 1.0:
        out["weight"] = rule["weight"]
    if str(rule.get("reply_instruction", "")).strip():
        out["reply_instruction"] = str(rule["reply_instruction"]).strip()
    return out


def parse_discord_rule_form(form: Mapping[str, Any]) -> dict[str, Any]:
    """Ein einzelnes Discord-Regelobjekt aus dem Regelformular."""
    parsed = parse_form(form)
    rule = parsed.get("rule", {})
    if not isinstance(rule, dict):
        raise ConfigError("Das Regelformular war unvollständig.")

    name = str(rule.get("name", "")).strip()
    if not name:
        raise ConfigError("Die Regel braucht einen Namen.")
    if not rule.get("keywords"):
        raise ConfigError("Die Regel braucht mindestens ein Schlüsselwort.")
    if not rule.get("actions"):
        raise ConfigError("Die Regel braucht mindestens eine Aktion.")

    # Nur gesetzte Felder uebernehmen - so bleibt die YAML schlank.
    out: dict[str, Any] = {
        "name": name,
        "keywords": rule["keywords"],
        "match": rule.get("match") or "any",
        "actions": rule["actions"],
    }
    emoji = str(rule.get("emoji", "")).strip()
    if emoji:
        out["emoji"] = emoji
    elif "react" in out["actions"]:
        raise ConfigError("Für die Aktion 'reagieren' wird ein Emoji gebraucht.")
    if rule.get("channels"):
        out["channels"] = rule["channels"]
    if rule.get("min_length"):
        out["min_length"] = rule["min_length"]
    if rule.get("weight") is not None and float(rule["weight"]) != 1.0:
        out["weight"] = rule["weight"]
    if str(rule.get("reply_instruction", "")).strip():
        out["reply_instruction"] = str(rule["reply_instruction"]).strip()
    return out

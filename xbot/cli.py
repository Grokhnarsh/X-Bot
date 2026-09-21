"""Kommandozeile des Bots.

    xbot init      Konfiguration anlegen
    xbot doctor    Einrichtung pruefen
    xbot preview   Texte erzeugen, ohne zu senden
    xbot post      einen Beitrag veroeffentlichen
    xbot engage    einen Durchlauf Hashtag-Monitoring
    xbot run       Dauerbetrieb
    xbot stats     Auswertung
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .bot import Bot, Check
from .config import Config, load_config
from .errors import ConfigError, CredentialsError, XBotError
from .logging_setup import configure_logging
from .state import from_iso

logger = logging.getLogger("xbot")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2

BANNER_LIVE = """
!!! ECHTBETRIEB !!!
Der Bot sendet ab jetzt wirklich auf X: Beitraege, Likes, Reposts, Antworten.
Zum Abbrechen: Strg+C
"""


# ---------------------------------------------------------------------------
# Ausgabehilfen
# ---------------------------------------------------------------------------
def _out(message: str = "") -> None:
    print(message, file=sys.stdout)


def _print_checks(checks: list[Check]) -> bool:
    width = max((len(c.name) for c in checks), default=10)
    for check in checks:
        _out(f"  [{check.symbol}] {check.name.ljust(width)}  {check.detail}")
    return all(check.ok for check in checks)


# ---------------------------------------------------------------------------
# Unterbefehle
# ---------------------------------------------------------------------------
def _find_example(name: str, *extra: "Path | None") -> "Path | None":
    """Sucht eine Vorlagendatei im Arbeitsverzeichnis und neben dem Paket."""
    candidates = [Path(name), Path.cwd() / name, Path(__file__).resolve().parent.parent / name]
    candidates.extend(folder / name for folder in extra if folder)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def cmd_init(args: argparse.Namespace) -> int:
    """Legt config.yaml und .env aus den Vorlagen an."""
    created: list[str] = []

    for example, target in (("config.example.yaml", args.config or "config.yaml"), (".env.example", ".env")):
        example_path = _find_example(example, Path(args.config).parent if args.config else None)
        target_path = Path(target)
        if example_path is None:
            _out(f"  FEHLER: Vorlage {example} nicht gefunden")
            return EXIT_ERROR
        if target_path.exists():
            _out(f"  uebersprungen: {target_path} existiert bereits")
            continue
        shutil.copyfile(example_path, target_path)
        created.append(str(target_path))
        _out(f"  angelegt: {target_path}")

    if created:
        _out("")
        _out("Naechste Schritte:")
        _out("  1. .env ausfuellen (Zugangsdaten aus dem X Developer Portal)")
        _out("  2. config.yaml anpassen (Regeln, Hashtags, Persona)")
        _out("  3. xbot doctor")
        _out("  4. xbot engage        (Probelauf, sendet nichts)")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _load(args)
    with Bot(config) as bot:
        _out("Pruefung der Einrichtung:")
        ok = _print_checks(bot.doctor())
        if args.check_ai:
            _out("")
            _out("KI-Test (erzeugt einen echten Text ueber die Claude-API):")
            check = bot.check_ai()
            _print_checks([check])
            ok = ok and check.ok
    _out("")
    _out("Alles bereit." if ok else "Es gibt offene Punkte - siehe die mit [x] markierten Zeilen.")
    return EXIT_OK if ok else EXIT_ERROR


def cmd_preview(args: argparse.Namespace) -> int:
    config = _load(args)
    with Bot(config) as bot:
        _out(f"{args.number} Textvorschlag/-vorschlaege (es wird nichts gesendet):")
        _out("")
        for index, generated in enumerate(bot.preview(count=args.number, topic=args.topic), start=1):
            _out(f"  {index}. [{generated.source}, {generated.length} Zeichen]")
            for line in generated.text.splitlines():
                _out(f"     {line}")
            _out("")
    return EXIT_OK


def cmd_post(args: argparse.Namespace) -> int:
    config = _load(args)
    with Bot(config) as bot:
        report = bot.post_once(topic=args.topic, force=args.force)
        _out(report.describe())
        if report.text:
            _out("")
            for line in report.text.splitlines():
                _out(f"  {line}")
        # Ein Ueberspringen wegen Zeitfenster oder Limit ist kein Fehlschlag -
        # sonst meldet jeder Cron-Lauf ausserhalb der Sendezeit einen Fehler.
        return EXIT_ERROR if report.failed else EXIT_OK


def cmd_engage(args: argparse.Namespace) -> int:
    config = _load(args)
    with Bot(config) as bot:
        report = bot.engage_once()
        _out(report.describe())
        skips = report.top_skips(args.show_skips)
        if skips:
            _out("")
            _out("Haeufigste Gruende fuer Ueberspringen:")
            for reason, count in skips:
                _out(f"  {count:3d}x  {reason}")
        if report.errors:
            _out("")
            _out("Fehler:")
            for error in report.errors[:5]:
                _out(f"  - {error}")
        return EXIT_ERROR if (report.errors and not report.total_actions) else EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    if not config.bot.dry_run:
        _out(BANNER_LIVE)
    with Bot(config) as bot:
        try:
            bot.run(initial_run=not args.no_initial_run, max_cycles=args.max_cycles)
        except KeyboardInterrupt:
            _out("")
            _out("Abgebrochen.")
        except CredentialsError as exc:
            _out(f"Anmeldung fehlgeschlagen: {exc}")
            return EXIT_ERROR
    return EXIT_OK


def cmd_stats(args: argparse.Namespace) -> int:
    config = _load(args)
    with Bot(config) as bot:
        data = bot.stats(days=args.days)

        _out(f"Modus: {'PROBELAUF' if data['dry_run'] else 'ECHTBETRIEB'}")
        _out(f"Datenbank: {data['database']}")
        _out("")

        summary = data["summary"]
        _out(f"Aktionen der letzten {data['days']} Tage:")
        if not summary:
            _out("  (noch keine)")
        else:
            for action in sorted(summary):
                counts = summary[action]
                _out(f"  {action:8} echt: {counts['live']:4d}   Probelauf: {counts['dry_run']:4d}")

        _out("")
        _out("Aktuelle Auslastung der Limits:")
        for action, usage in data["quota"].items():
            _out(
                f"  {action:8} Stunde {usage.used_hour:3d}/{usage.limit_hour:<3d}"
                f"   Tag {usage.used_day:3d}/{usage.limit_day:<3d}   frei: {usage.remaining}"
            )

        recent = data["recent"]
        if recent:
            _out("")
            _out("Zuletzt:")
            for row in recent:
                stamp = from_iso(row["created_at"]).astimezone(config.bot.tzinfo).strftime("%d.%m. %H:%M")
                marker = "[P]" if row["dry_run"] else "   "
                target = row["target_author"] or row["target_id"] or ""
                text = (row["text"] or "").replace("\n", " ")
                if len(text) > 60:
                    text = text[:57] + "..."
                detail = f"@{target}" if row["target_author"] else target
                _out(f"  {marker} {stamp}  {row['action']:7} {detail:18} {text}")
    return EXIT_OK


def cmd_web(args: argparse.Namespace) -> int:
    """Startet die Weboberflaeche.

    Anders als die uebrigen Befehle laeuft dieser auch bei einer kaputten
    config.yaml an - die Oberflaeche zeigt dann eine Seite zum Reparieren,
    was ohne Terminal sonst nicht moeglich waere.
    """
    try:
        from .web import create_app
        from .web.server import serve
    except ImportError as exc:
        print(
            "Die Weboberflaeche braucht zusaetzliche Pakete.\n"
            "  pip install -r requirements-web.txt\n"
            f"(fehlt: {exc.name})",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if args.live and args.dry_run:
        print("--live und --dry-run schliessen sich gegenseitig aus.", file=sys.stderr)
        return EXIT_CONFIG
    # Der Modus wird ueber die Umgebung gesetzt, damit der Schalter in der
    # Oberflaeche spaeter dieselbe Stelle veraendert.
    if args.live:
        os.environ["XBOT_DRY_RUN"] = "false"
    elif args.dry_run:
        os.environ["XBOT_DRY_RUN"] = "true"

    app = create_app(args.config, password=args.password)
    try:
        serve(app, host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        app.extensions["xbot_runner"].shutdown()
    return EXIT_OK


# ---------------------------------------------------------------------------
# Gemeinsames
# ---------------------------------------------------------------------------
def _load(args: argparse.Namespace) -> Config:
    config = load_config(args.config)
    if args.live and args.dry_run:
        raise ConfigError("--live und --dry-run schliessen sich gegenseitig aus.")
    if args.live:
        config = replace(config, bot=replace(config.bot, dry_run=False))
    elif args.dry_run:
        config = replace(config, bot=replace(config.bot, dry_run=True))
    configure_logging(config.logging, verbose=args.verbose, quiet=args.quiet)
    return config


def _add_global_flags(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """Flags, die vor und nach dem Unterbefehl stehen duerfen."""
    default = argparse.SUPPRESS if suppress else None
    flag_default = argparse.SUPPRESS if suppress else False
    parser.add_argument("-c", "--config", default=default, help="Pfad zur config.yaml (Standard: config.yaml)")
    parser.add_argument("--dry-run", action="store_true", default=flag_default, help="nichts senden, nur protokollieren")
    parser.add_argument("--live", action="store_true", default=flag_default, help="wirklich auf X senden")
    parser.add_argument("-v", "--verbose", action="store_true", default=flag_default, help="ausfuehrliche Ausgabe")
    parser.add_argument("-q", "--quiet", action="store_true", default=flag_default, help="keine Protokollausgabe auf der Konsole")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xbot",
        description="Automatisiert Beitraege, Likes, Reposts und Antworten auf X.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Beispiele:\n"
            "  xbot init                    Konfiguration anlegen\n"
            "  xbot doctor                  Einrichtung pruefen\n"
            "  xbot preview -n 5            fuenf Textvorschlaege ansehen\n"
            "  xbot engage                  einmal auf Hashtags reagieren (Probelauf)\n"
            "  xbot run --live              Dauerbetrieb, sendet wirklich\n"
            "  xbot web                     Weboberflaeche auf http://127.0.0.1:8080\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"X-Bot {__version__}")
    _add_global_flags(parser)

    # Dieselben Flags noch einmal fuer jeden Unterbefehl, damit sowohl
    # "xbot -q doctor" als auch "xbot doctor -q" funktioniert. Die Kopien
    # nutzen SUPPRESS und ueberschreiben deshalb nichts, wenn sie fehlen.
    common = argparse.ArgumentParser(add_help=False)
    _add_global_flags(common, suppress=True)

    sub = parser.add_subparsers(dest="command", required=True, metavar="BEFEHL")

    p = sub.add_parser("init", parents=[common], help="config.yaml und .env aus den Vorlagen anlegen")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", parents=[common], help="Konfiguration, Zugangsdaten und Verbindung pruefen")
    p.add_argument("--check-ai", action="store_true", help="zusaetzlich einen echten Text ueber die Claude-API erzeugen")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("preview", parents=[common], help="Texte erzeugen, ohne zu senden")
    p.add_argument("-n", "--number", type=int, default=3, help="Anzahl der Vorschlaege (Standard: 3)")
    p.add_argument("--topic", default=None, help="Thema vorgeben")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("post", parents=[common], help="einen eigenen Beitrag veroeffentlichen")
    p.add_argument("--topic", default=None, help="Thema vorgeben")
    p.add_argument("--force", action="store_true", help="Zeitfenster und posting.enabled ignorieren (Limits gelten weiter)")
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("engage", parents=[common], help="einmal auf die konfigurierten Hashtags reagieren")
    p.add_argument("--show-skips", type=int, default=8, help="wie viele Ueberspringungsgruende angezeigt werden")
    p.set_defaults(func=cmd_engage)

    p = sub.add_parser("run", parents=[common], help="Dauerbetrieb mit Taktgeber")
    p.add_argument("--max-cycles", type=int, default=None, help="nach so vielen Laeufen beenden (fuer Tests)")
    p.add_argument("--no-initial-run", action="store_true", help="nicht sofort beim Start loslegen")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("web", parents=[common], help="Weboberflaeche starten")
    p.add_argument("--host", default="127.0.0.1",
                   help="Adresse (Standard: 127.0.0.1, nur lokal erreichbar)")
    p.add_argument("--port", type=int, default=8080, help="Port (Standard: 8080)")
    p.add_argument("--password", default=None,
                   help="Passwort der Oberflaeche (sonst XBOT_WEB_PASSWORD, sonst zufaellig)")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("stats", parents=[common], help="Auswertung der bisherigen Aktionen")
    p.add_argument("--days", type=int, default=7, help="Zeitraum in Tagen (Standard: 7)")
    p.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:  # python-dotenv ist optional
        pass

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except CredentialsError as exc:
        print(f"Zugangsdaten: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except XBotError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

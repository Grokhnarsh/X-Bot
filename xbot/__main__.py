"""Erlaubt den Aufruf per ``python -m xbot``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

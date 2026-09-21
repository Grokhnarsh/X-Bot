"""Erlaubt den Start per ``python -m xbot.web``."""

from ..cli import main

if __name__ == "__main__":
    raise SystemExit(main(["web"]))

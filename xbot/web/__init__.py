"""Weboberflaeche zur Bedienung des Bots.

Optionaler Bestandteil: ``pip install -r requirements-web.txt``.
Der Bot selbst laeuft auch ohne diese Schicht.
"""

from .app import create_app

__all__ = ["create_app"]

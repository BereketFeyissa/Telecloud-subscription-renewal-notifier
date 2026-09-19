"""Telecloud component validity scraper and multi-channel notifier."""

from importlib.metadata import version

#: Read from the installed package metadata so there is exactly one source of truth.
#: Hard-coding it here is how pyproject and this file drifted apart through two releases.
__version__ = version("tele-scraper")

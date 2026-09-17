"""Exception hierarchy.

Every error raised by this package derives from :class:`TeleScraperError` so callers can
distinguish our failures from library failures without catching bare ``Exception``
(CLAUDE.md §3.4, §10.7).
"""

from __future__ import annotations


class TeleScraperError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(TeleScraperError):
    """Configuration is missing or invalid. Exit code 4."""


class ScrapeError(TeleScraperError):
    """The portal could not be reached or returned an unusable response. Exit code 1."""


class AuthError(ScrapeError):
    """Portal rejected our credentials.

    Never retried: a 401/403 is a credential incident, not a transient fault
    (CLAUDE.md §7.3).
    """


class ParseError(TeleScraperError):
    """Portal markup did not match what the parser expects. Yields UNKNOWN, never a default."""


class StateStoreError(TeleScraperError):
    """The dedup store is unreachable or corrupt.

    Fatal by design: without dedup we would re-notify everyone (CLAUDE.md §8.2).
    """


class NotifyError(TeleScraperError):
    """A notification could not be delivered. Exit code 3."""


class ChannelNotConfiguredError(NotifyError):
    """A route references a channel that has no working configuration."""


class ProviderNotSelectedError(NotifyError):
    """A channel needs a provider decision that has not been made yet (CLAUDE.md §0)."""

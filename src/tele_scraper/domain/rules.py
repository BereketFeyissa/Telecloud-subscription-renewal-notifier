"""Pure helpers for thresholds, quiet hours, and component matching.

Every function here is pure: no network, no clock reads, no environment access
(CLAUDE.md §5). The current time is always passed in.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from fnmatch import fnmatchcase
from zoneinfo import ZoneInfo

_DURATION_TOKEN = re.compile(r"^(?P<value>\d+)(?P<unit>[smhdw])$", re.IGNORECASE)
_UNIT_SECONDS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(token: str) -> timedelta:
    """Parse a single duration token such as ``14d``, ``12h``, ``30m``.

    Raises:
        ValueError: if the token is not a positive integer followed by s/m/h/d/w.
    """
    match = _DURATION_TOKEN.match(token.strip())
    if match is None:
        raise ValueError(
            f"invalid duration {token!r}: expected <int><unit> with unit in s, m, h, d, w"
        )
    value = int(match.group("value"))
    if value <= 0:
        raise ValueError(f"invalid duration {token!r}: must be greater than zero")
    return timedelta(seconds=value * _UNIT_SECONDS[match.group("unit").lower()])


def parse_thresholds(raw: str) -> tuple[timedelta, ...]:
    """Parse a comma-separated warning ladder into thresholds, widest first.

    ``"14d,7d,3d,1d,12h"`` becomes ``(14d, 7d, 3d, 1d, 12h)``. Duplicates are collapsed.

    Raises:
        ValueError: if the ladder is empty or any token is malformed.
    """
    tokens = [t for t in (part.strip() for part in raw.split(",")) if t]
    if not tokens:
        raise ValueError("warning threshold ladder must contain at least one duration")
    unique = {parse_duration(token) for token in tokens}
    return tuple(sorted(unique, reverse=True))


def select_rung(remaining: timedelta, thresholds: tuple[timedelta, ...]) -> timedelta | None:
    """Return the tightest threshold that ``remaining`` has crossed.

    With a ladder of 14d/7d/3d/1d/12h and 2 days left, the crossed rungs are 14d, 7d and 3d;
    the tightest is 3d. Including the rung in the dedup key is what makes each step of the
    ladder fire exactly once (CLAUDE.md §6, §8.2).

    Returns None when ``remaining`` is still wider than every threshold.
    """
    crossed = [t for t in thresholds if remaining <= t]
    return min(crossed) if crossed else None


def in_quiet_hours(now: datetime, start: time, end: time, tz: str) -> bool:
    """Whether ``now`` falls inside a local quiet-hours window.

    Handles windows that wrap past midnight (22:00 -> 06:00). A window whose start equals its
    end is treated as empty, never as "always quiet" - silencing alerts forever is the more
    dangerous reading.
    """
    if start == end:
        return False
    local = now.astimezone(ZoneInfo(tz)).time()
    if start < end:
        return start <= local < end
    return local >= start or local < end


def matches_component(patterns: tuple[str, ...], component_id: str, name: str) -> bool:
    """Whether a component matches any of a route's glob patterns.

    ``"*"`` matches everything. Patterns are tested against the id first, then the display
    name, so operators can route on either.
    """
    return any(
        fnmatchcase(component_id, pattern) or fnmatchcase(name, pattern) for pattern in patterns
    )

"""Tests for the pure threshold, quiet-hours and matching helpers."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from tele_scraper.domain.rules import (
    in_quiet_hours,
    matches_component,
    parse_duration,
    parse_thresholds,
    select_rung,
)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("30s", timedelta(seconds=30)),
        ("15m", timedelta(minutes=15)),
        ("12h", timedelta(hours=12)),
        ("14d", timedelta(days=14)),
        ("2w", timedelta(weeks=2)),
        (" 7D ", timedelta(days=7)),
    ],
)
def test_parse_duration(token: str, expected: timedelta) -> None:
    assert parse_duration(token) == expected


@pytest.mark.parametrize("token", ["", "14", "d", "14y", "-1d", "0d", "1.5d", "14 d"])
def test_parse_duration_rejects_garbage(token: str) -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration(token)


def test_parse_thresholds_sorts_widest_first_and_dedupes() -> None:
    assert parse_thresholds("1d,14d,7d,1d") == (
        timedelta(days=14),
        timedelta(days=7),
        timedelta(days=1),
    )


@pytest.mark.parametrize("raw", ["", "   ", ",,,"])
def test_parse_thresholds_rejects_empty(raw: str) -> None:
    with pytest.raises(ValueError, match="at least one duration"):
        parse_thresholds(raw)


def test_select_rung_returns_none_outside_the_ladder() -> None:
    ladder = parse_thresholds("14d,7d")
    assert select_rung(timedelta(days=30), ladder) is None


def test_select_rung_picks_the_tightest_crossed() -> None:
    ladder = parse_thresholds("14d,7d,3d")
    assert select_rung(timedelta(days=2), ladder) == timedelta(days=3)


# --- quiet hours -------------------------------------------------------------------

TZ = "Africa/Addis_Ababa"  # UTC+3


def utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 16, hour, minute, tzinfo=UTC)


def test_quiet_hours_same_day_window() -> None:
    # 09:00-17:00 local == 06:00-14:00 UTC
    assert in_quiet_hours(utc(7), time(9, 0), time(17, 0), TZ) is True
    assert in_quiet_hours(utc(15), time(9, 0), time(17, 0), TZ) is False


def test_quiet_hours_wrapping_midnight() -> None:
    # 22:00-06:00 local == 19:00-03:00 UTC
    assert in_quiet_hours(utc(20), time(22, 0), time(6, 0), TZ) is True
    assert in_quiet_hours(utc(2), time(22, 0), time(6, 0), TZ) is True
    assert in_quiet_hours(utc(12), time(22, 0), time(6, 0), TZ) is False


def test_quiet_hours_boundaries_are_half_open() -> None:
    assert in_quiet_hours(utc(6), time(9, 0), time(17, 0), TZ) is True  # exactly 09:00 local
    assert in_quiet_hours(utc(14), time(9, 0), time(17, 0), TZ) is False  # exactly 17:00 local


def test_equal_start_and_end_is_never_quiet() -> None:
    """An empty window must not read as 'always silent' - that would lose every alert."""
    assert in_quiet_hours(utc(3), time(9, 0), time(9, 0), TZ) is False


# --- component matching ------------------------------------------------------------


def test_wildcard_matches_everything() -> None:
    assert matches_component(("*",), "comp-1", "Anything") is True


def test_matches_on_id_or_name() -> None:
    assert matches_component(("comp-*",), "comp-1", "Storage") is True
    assert matches_component(("Storage*",), "comp-1", "Storage Bundle") is True
    assert matches_component(("other-*",), "comp-1", "Storage") is False


def test_matching_is_case_sensitive() -> None:
    assert matches_component(("COMP-*",), "comp-1", "Storage") is False

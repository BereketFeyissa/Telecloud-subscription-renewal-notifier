"""Portal JSON -> Component.

Driven by a redacted capture of the real endpoint (``tests/fixtures/renewlist.json``), whose
field names, date formats and status wording are reproduced exactly (CLAUDE.md §7.8).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tele_scraper.errors import ParseError
from tele_scraper.scraper.parser import (
    FIELDS,
    parse_component,
    parse_components,
    parse_duration,
    parse_envelope,
    parse_timestamp,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "renewlist.json"


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --- timestamps --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-22 05:19:55.864 +0000 UTC", datetime(2026, 9, 22, 5, 19, 55, 864000, tzinfo=UTC)),
        ("2026-09-19 05:19:55.79 +0000 UTC", datetime(2026, 9, 19, 5, 19, 55, 790000, tzinfo=UTC)),
        ("2026-01-01 00:00:00 +0000 UTC", datetime(2026, 1, 1, 0, 0, tzinfo=UTC)),
        ("2026-08-22T05:19:55.864Z", datetime(2026, 8, 22, 5, 19, 55, 864000, tzinfo=UTC)),
        ("2026-09-22 08:19:55.864 +0300 EAT", datetime(2026, 9, 22, 5, 19, 55, 864000, tzinfo=UTC)),
    ],
)
def test_parse_timestamp(raw: str, expected: datetime) -> None:
    assert parse_timestamp(raw) == expected


def test_parsed_timestamps_are_always_aware_utc() -> None:
    parsed = parse_timestamp("2026-09-22 08:19:55.864 +0300 EAT")
    assert parsed.tzinfo is UTC


def test_nanosecond_precision_is_truncated_not_rejected() -> None:
    assert parse_timestamp("2026-09-22 05:19:55.123456789 +0000 UTC").microsecond == 123456


@pytest.mark.parametrize("raw", ["", "not a date", "2026-13-45 99:99:99 +0000 UTC", "2026-09-22"])
def test_bad_timestamps_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_timestamp(raw)


# --- durations ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("19d11h50m5s", timedelta(days=19, hours=11, minutes=50, seconds=5)),
        ("4d20h58m45s", timedelta(days=4, hours=20, minutes=58, seconds=45)),
        ("45s", timedelta(seconds=45)),
        ("500ms", timedelta(milliseconds=500)),
        ("-1d-2h-29m-49s", -timedelta(days=1, hours=2, minutes=29, seconds=49)),
    ],
)
def test_parse_duration(raw: str, expected: timedelta) -> None:
    assert parse_duration(raw) == expected


def test_milliseconds_are_not_read_as_minutes() -> None:
    assert parse_duration("500ms") != parse_duration("500m")


@pytest.mark.parametrize("raw", ["", "soon", "abc"])
def test_bad_durations_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(raw)


# --- envelope ----------------------------------------------------------------------


def test_envelope_yields_items_and_total(payload: dict) -> None:
    page = parse_envelope(payload)
    assert page.total == 5
    assert len(page.items) == 5


def test_nonzero_status_is_an_error_even_on_http_200() -> None:
    with pytest.raises(ParseError, match="portal reported failure"):
        parse_envelope({"status": 500, "resMsg": "token expired", "data": {}})


def test_string_zero_status_is_accepted() -> None:
    assert parse_envelope({"status": "0", "data": {"total": 0, "items": []}}).items == []


@pytest.mark.parametrize(
    "bad",
    [[], "text", 42, {"status": 0}, {"status": 0, "data": []}, {"status": 0, "data": {"items": 3}}],
)
def test_malformed_envelopes_are_rejected(bad: object) -> None:
    with pytest.raises(ParseError):
        parse_envelope(bad)


def test_missing_items_is_an_empty_page_not_a_crash() -> None:
    assert parse_envelope({"status": 0, "data": {"total": 0}}).items == []


def test_bogus_total_falls_back_to_what_is_visible() -> None:
    page = parse_envelope({"status": 0, "data": {"total": -3, "items": [{"id": "a"}]}})
    assert page.total == 1


def test_non_object_entries_are_dropped() -> None:
    page = parse_envelope({"status": 0, "data": {"total": 2, "items": [{"id": "a"}, "junk"]}})
    assert len(page.items) == 1


# --- components --------------------------------------------------------------------


def test_fixture_parses_into_components(payload: dict) -> None:
    components = parse_components(parse_envelope(payload).items)
    assert len(components) == 5


def test_expiry_and_activation_are_mapped(payload: dict) -> None:
    first = parse_components(parse_envelope(payload).items)[0]
    assert first.component_id == "00000000-0000-4000-8000-00000000aaaa"
    assert first.expires_at == datetime(2027, 9, 22, 5, 19, 55, 864000, tzinfo=UTC)
    assert first.activated_at == datetime(2026, 4, 29, 15, 9, 55, 83000, tzinfo=UTC)
    assert first.parse_error is None


def test_validity_period_is_never_mapped_from_the_portal_field(payload: dict) -> None:
    """The portal's validityPeriod is time REMAINING, not the purchased duration.

    Mapping it would let §6 derive an expiry days away for a component bought a year ago.
    """
    for component in parse_components(parse_envelope(payload).items):
        assert component.validity_period is None


def test_portal_status_is_kept_verbatim_but_not_acted_on(payload: dict) -> None:
    """The live portal reports 'Active' for components that expired days ago."""
    components = parse_components(parse_envelope(payload).items)
    lapsed = next(c for c in components if c.component_id.endswith("cccc"))
    assert lapsed.portal_status == "Active"
    assert lapsed.expires_at is not None
    assert lapsed.expires_at < datetime(2026, 9, 17, tzinfo=UTC), "this entry is already expired"


def test_missing_expiry_becomes_a_parse_error_not_a_default(payload: dict) -> None:
    components = parse_components(parse_envelope(payload).items)
    broken = next(c for c in components if c.component_id.endswith("dddd"))
    assert broken.expires_at is None
    assert broken.parse_error is not None
    assert "expirationTime" in broken.parse_error


def test_entry_without_an_id_is_flagged_not_invented(payload: dict) -> None:
    """§1.1 forbids deriving the key from the display name, so it gets a positional sentinel."""
    components = parse_components(parse_envelope(payload).items)
    orphan = components[4]
    assert orphan.component_id == "unidentified-item-4"
    assert orphan.parse_error is not None
    assert "eeeeee" not in orphan.component_id


def test_name_carries_the_service_type() -> None:
    component = parse_component({"id": "x", "name": "web-1", "serviceType": "ECS"})
    assert component.name == "web-1 (ECS)"


def test_unreadable_expiry_is_flagged() -> None:
    component = parse_component({"id": "x", "expirationTime": "whenever"})
    assert component.expires_at is None
    assert "unreadable" in (component.parse_error or "")


def test_unreadable_activation_is_tolerated() -> None:
    """expirationTime is authoritative; a bad purchaseTime must not block the component."""
    component = parse_component(
        {"id": "x", "purchaseTime": "nonsense", "expirationTime": "2027-01-01 00:00:00 +0000 UTC"}
    )
    assert component.activated_at is None
    assert component.expires_at is not None
    assert component.parse_error is None


def test_absent_status_is_none_not_a_guess() -> None:
    assert parse_component({"id": "x"}).portal_status is None


def test_field_map_is_the_single_source_of_field_names() -> None:
    assert FIELDS["expires_at"] == "expirationTime"
    assert FIELDS["component_id"] == "id"


def test_empty_listing_parses_to_nothing() -> None:
    assert parse_components([]) == []

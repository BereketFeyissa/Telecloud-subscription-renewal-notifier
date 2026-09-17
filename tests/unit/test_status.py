"""Exhaustive tests for the §6 status ladder.

This module is required to reach 100% branch coverage of ``domain.status`` (CLAUDE.md §11.3).
Every case named in §11.3 has a test here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tele_scraper.domain.rules import parse_thresholds
from tele_scraper.domain.status import (
    derive_status,
    evaluate_all,
    normalize_token,
    resolve_expiry,
)
from tele_scraper.models import Status
from tests.conftest import NOW, make_component

LADDER = parse_thresholds("14d,7d,3d,1d,12h")


def evaluate(component, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("thresholds", LADDER)
    return derive_status(component, **kwargs)


# --- guard rails -------------------------------------------------------------------


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        derive_status(
            make_component(expires_in=timedelta(days=30)),
            now=datetime(2026, 9, 16, 12, 0),  # noqa: DTZ001
            thresholds=LADDER,
        )


def test_empty_ladder_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        derive_status(make_component(expires_in=timedelta(days=30)), now=NOW, thresholds=())


# --- the ladder, in order ----------------------------------------------------------


def test_suspended_wins_over_everything() -> None:
    component = make_component(expires_in=timedelta(days=365), portal_status="Suspended")
    result = evaluate(component, suspended_tokens=frozenset({"suspended"}))
    assert result.status is Status.SUSPENDED


def test_suspended_matching_is_case_and_space_insensitive() -> None:
    component = make_component(
        expires_in=timedelta(days=365), portal_status="  BLOCKED   BY  BILLING "
    )
    result = evaluate(component, suspended_tokens=frozenset({"blocked by billing"}))
    assert result.status is Status.SUSPENDED


def test_portal_status_not_in_vocabulary_is_ignored() -> None:
    component = make_component(expires_in=timedelta(days=365), portal_status="Running")
    result = evaluate(component, suspended_tokens=frozenset({"suspended"}))
    assert result.status is Status.ACTIVE


def test_empty_vocabulary_never_produces_suspended() -> None:
    """While PORTAL_SUSPENDED_TOKENS is unset nothing is SUSPENDED (CLAUDE.md §2 open 2)."""
    component = make_component(expires_in=timedelta(days=365), portal_status="Suspended")
    assert evaluate(component).status is Status.ACTIVE


def test_portal_status_absent_skips_the_suspended_check() -> None:
    component = make_component(expires_in=timedelta(days=365), portal_status=None)
    assert evaluate(component, suspended_tokens=frozenset({"suspended"})).status is Status.ACTIVE


def test_parse_error_is_unknown_not_active() -> None:
    result = evaluate(make_component(parse_error="expiry cell missing"))
    assert result.status is Status.UNKNOWN
    assert "expiry cell missing" in result.reason


def test_missing_expiry_is_unknown() -> None:
    result = evaluate(make_component(expires_at=None))
    assert result.status is Status.UNKNOWN
    assert result.status is not Status.ACTIVE


def test_naive_expiry_is_unknown() -> None:
    """A naive expiry cannot be compared safely, so it is never trusted (CLAUDE.md §3.10)."""
    component = make_component(expires_in=timedelta(days=30)).model_copy(
        update={"expires_at": datetime(2026, 10, 16, 12, 0)}  # noqa: DTZ001
    )
    assert evaluate(component).status is Status.UNKNOWN


def test_expired_by_one_second() -> None:
    result = evaluate(make_component(expires_in=timedelta(seconds=-1)))
    assert result.status is Status.EXPIRED
    assert result.remaining == timedelta(seconds=-1)


def test_expiring_exactly_now_is_expired() -> None:
    assert evaluate(make_component(expires_in=timedelta(0))).status is Status.EXPIRED


def test_one_second_before_expiry_is_expiring_soon() -> None:
    result = evaluate(make_component(expires_in=timedelta(seconds=1)))
    assert result.status is Status.EXPIRING_SOON
    assert result.rung == timedelta(hours=12)


@pytest.mark.parametrize(
    ("remaining", "expected_rung"),
    [
        (timedelta(days=13, hours=23), timedelta(days=14)),
        (timedelta(days=14), timedelta(days=14)),
        (timedelta(days=7), timedelta(days=7)),
        (timedelta(days=5), timedelta(days=7)),
        (timedelta(days=3), timedelta(days=3)),
        (timedelta(days=2), timedelta(days=3)),
        (timedelta(days=1), timedelta(days=1)),
        (timedelta(hours=13), timedelta(days=1)),
        (timedelta(hours=12), timedelta(hours=12)),
        (timedelta(hours=1), timedelta(hours=12)),
    ],
)
def test_rung_is_the_tightest_threshold_crossed(
    remaining: timedelta, expected_rung: timedelta
) -> None:
    result = evaluate(make_component(expires_in=remaining))
    assert result.status is Status.EXPIRING_SOON
    assert result.rung == expected_rung


def test_outside_the_ladder_is_active() -> None:
    result = evaluate(make_component(expires_in=timedelta(days=15)))
    assert result.status is Status.ACTIVE
    assert result.rung is None


# --- derived expiry ----------------------------------------------------------------


def test_expiry_is_derived_from_activation_plus_validity() -> None:
    component = make_component(
        expires_at=None,
        activated_at=NOW - timedelta(days=28),
        validity_period=timedelta(days=30),
    )
    result = evaluate(component)
    assert result.component.expires_at_derived is True
    assert result.component.expires_at == NOW + timedelta(days=2)
    assert result.status is Status.EXPIRING_SOON


def test_derivation_needs_both_inputs() -> None:
    only_activation = make_component(expires_at=None, activated_at=NOW - timedelta(days=1))
    only_validity = make_component(expires_at=None, validity_period=timedelta(days=30))
    assert evaluate(only_activation).status is Status.UNKNOWN
    assert evaluate(only_validity).status is Status.UNKNOWN


def test_resolve_expiry_leaves_a_scraped_expiry_alone() -> None:
    component = make_component(
        expires_in=timedelta(days=5),
        activated_at=NOW - timedelta(days=100),
        validity_period=timedelta(days=1),
    )
    resolved = resolve_expiry(component)
    assert resolved is component
    assert resolved.expires_at_derived is False


# --- helpers -----------------------------------------------------------------------


def test_normalize_token_folds_case_and_whitespace() -> None:
    assert normalize_token("  Not   ACTIVE ") == "not active"


def test_evaluate_all_preserves_order() -> None:
    components = [
        make_component("a", expires_in=timedelta(days=100)),
        make_component("b", expires_in=timedelta(days=-1)),
        make_component("c", expires_at=None),
    ]
    results = evaluate_all(components, now=NOW, thresholds=LADDER)
    assert [r.component.component_id for r in results] == ["a", "b", "c"]
    assert [r.status for r in results] == [Status.ACTIVE, Status.EXPIRED, Status.UNKNOWN]


def test_evaluate_all_on_empty_input_returns_empty() -> None:
    """The run-level UNKNOWN for an empty scrape is the runner's job, not this function's."""
    assert evaluate_all([], now=NOW, thresholds=LADDER) == []

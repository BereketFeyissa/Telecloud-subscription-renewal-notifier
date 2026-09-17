"""Model invariants: dedup keys, exit codes, run accounting."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from tele_scraper.models import (
    ChannelTarget,
    Component,
    DeliveryResult,
    Evaluation,
    NotificationEvent,
    RunReport,
    Status,
)
from tests.conftest import NOW, make_component


def make_event(
    status: Status = Status.EXPIRING_SOON, rung: timedelta | None = None
) -> NotificationEvent:
    evaluation = Evaluation(
        component=make_component(expires_in=timedelta(days=2)),
        status=status,
        evaluated_at=NOW,
        rung=rung,
    )
    return NotificationEvent(
        evaluation=evaluation,
        recipient="ops",
        target=ChannelTarget(channel="slack", address="https://hooks.test/x"),
    )


def test_naive_datetimes_are_rejected_at_the_boundary() -> None:
    with pytest.raises(ValidationError):
        Component(component_id="c", expires_at=datetime(2026, 1, 1, 0, 0))  # noqa: DTZ001


def test_empty_component_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Component(component_id="")


def test_label_falls_back_to_the_id() -> None:
    assert make_component("c-9", name="").label == "c-9"
    assert make_component("c-9", name="Storage").label == "Storage"


def test_dedup_key_changes_with_status() -> None:
    assert make_event(Status.EXPIRING_SOON).dedup_key != make_event(Status.EXPIRED).dedup_key


def test_dedup_key_changes_with_rung() -> None:
    a = make_event(rung=timedelta(days=7)).dedup_key
    b = make_event(rung=timedelta(days=3)).dedup_key
    assert a != b


def test_dedup_key_is_stable_for_the_same_state() -> None:
    assert (
        make_event(rung=timedelta(days=3)).dedup_key == make_event(rung=timedelta(days=3)).dedup_key
    )


def test_rung_key_for_no_rung() -> None:
    assert make_event().evaluation.rung_key == "-"


def test_critical_statuses() -> None:
    assert make_event(Status.EXPIRED).evaluation.is_critical is True
    assert make_event(Status.UNKNOWN).evaluation.is_critical is True
    assert make_event(Status.EXPIRING_SOON).evaluation.is_critical is False
    assert make_event(Status.SUSPENDED).evaluation.is_critical is False


# --- run reporting -----------------------------------------------------------------


def report(**kwargs) -> RunReport:  # type: ignore[no-untyped-def]
    kwargs.setdefault("run_id", "r1")
    kwargs.setdefault("started_at", NOW)
    kwargs.setdefault("finished_at", NOW)
    return RunReport(**kwargs)


def evaluation(status: Status) -> Evaluation:
    return Evaluation(component=make_component(), status=status, evaluated_at=NOW)


def test_exit_code_clean_run() -> None:
    assert report(evaluations=[evaluation(Status.ACTIVE)]).exit_code == 0


def test_exit_code_scrape_failure_takes_precedence() -> None:
    assert report(scrape_failed=True, evaluations=[evaluation(Status.UNKNOWN)]).exit_code == 1


def test_exit_code_unknown_is_two() -> None:
    assert report(evaluations=[evaluation(Status.UNKNOWN)]).exit_code == 2


def test_exit_code_delivery_failure_is_three() -> None:
    failed = DeliveryResult(channel="slack", recipient="ops", ok=False, error="boom")
    assert report(evaluations=[evaluation(Status.ACTIVE)], results=[failed]).exit_code == 3


def test_suppressed_results_are_not_delivery_failures() -> None:
    suppressed = DeliveryResult(
        channel="slack", recipient="ops", ok=True, suppressed=True, error="dedup"
    )
    assert report(results=[suppressed]).delivery_failed is False


def test_counts_cover_every_status() -> None:
    counts = report(evaluations=[evaluation(Status.ACTIVE), evaluation(Status.EXPIRED)]).counts
    assert set(counts) == set(Status)
    assert counts[Status.ACTIVE] == 1
    assert counts[Status.EXPIRED] == 1
    assert counts[Status.UNKNOWN] == 0

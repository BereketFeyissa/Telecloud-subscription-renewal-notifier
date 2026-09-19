"""End-to-end: fixture -> parse -> evaluate -> route -> deliver.

Exercises every layer together with no network, against the redacted capture of the real
endpoint. This is the test that would have caught mapping validityPeriod to validity_period.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time

from tele_scraper.domain.rules import parse_thresholds
from tele_scraper.models import Status
from tele_scraper.notify.base import MessageRenderer
from tele_scraper.notify.router import Router
from tele_scraper.runner import Runner
from tele_scraper.scraper.client import FixturePortalClient
from tele_scraper.state.store import MemoryStateStore
from tests.conftest import make_settings
from tests.unit.test_router import FakeNotifier

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "renewlist.json"

#: The fixture is written relative to this instant: one entry is long-lived, one is inside the
#: warning ladder, one lapsed two days ago, one has no expiry, one has no id.
NOW = datetime(2026, 9, 17, 8, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _frozen_clock():  # type: ignore[no-untyped-def]
    """Pin the clock to the instant the redacted fixture is written against (§11.5)."""
    with freeze_time(NOW):
        yield


ROUTES = json.dumps(
    {
        "routes": [
            {
                "recipient": "ops",
                "channels": [{"channel": "slack", "address": "https://hooks.test/ops"}],
                "statuses": ["EXPIRED", "EXPIRING_SOON", "UNKNOWN", "SUSPENDED"],
            }
        ]
    }
)


@pytest.fixture
def pipeline() -> tuple[Runner, FakeNotifier]:
    settings = make_settings(notify_routes_json=ROUTES, notify_enabled=True)
    slack = FakeNotifier("slack")
    renderer = MessageRenderer(settings.timezone, "en")
    store = MemoryStateStore()
    router = Router(settings, store, {"slack": slack}, renderer)
    return Runner(settings, FixturePortalClient(FIXTURE), store, router), slack


def key_of(component_id: str) -> str:
    """Short label for a fixture entry: its id suffix, or the sentinel for the entry with none."""
    return component_id if component_id.startswith("unidentified") else component_id[-4:]


def statuses_at(now: datetime) -> dict[str, Status]:
    from tele_scraper.domain.status import evaluate_all
    from tele_scraper.scraper.parser import parse_components, parse_envelope

    items = parse_envelope(json.loads(FIXTURE.read_text())).items
    return {
        key_of(e.component.component_id): e.status
        for e in evaluate_all(
            parse_components(items), now=now, thresholds=parse_thresholds("14d,7d,3d,1d,12h")
        )
    }


def test_each_fixture_entry_lands_on_the_right_status() -> None:
    result = statuses_at(NOW)
    assert result["aaaa"] is Status.ACTIVE, "expires 2027, far outside the ladder"
    assert result["bbbb"] is Status.EXPIRING_SOON, "expires in ~2 days"
    assert result["cccc"] is Status.EXPIRED, "lapsed, even though the portal says Active"
    assert result["dddd"] is Status.UNKNOWN, "no expirationTime"
    assert result["unidentified-item-4"] is Status.UNKNOWN, "no id"


def test_the_portal_calling_an_expired_component_active_does_not_win() -> None:
    """The failure this system exists to catch (CLAUDE.md §1, §6)."""
    from tele_scraper.scraper.parser import parse_components, parse_envelope

    items = parse_envelope(json.loads(FIXTURE.read_text())).items
    lapsed = next(c for c in parse_components(items) if c.component_id.endswith("cccc"))
    assert lapsed.portal_status == "Active"
    assert statuses_at(NOW)["cccc"] is Status.EXPIRED


async def test_full_run_notifies_only_the_unhealthy(pipeline: tuple[Runner, FakeNotifier]) -> None:
    runner, slack = pipeline
    report = await runner.run_once()

    notified = {e.evaluation.component.component_id[-4:] for e in slack.sent}
    assert "aaaa" not in notified, "a healthy component must stay quiet"
    assert {"bbbb", "cccc", "dddd"} <= notified
    assert report.exit_code == 2, "an UNKNOWN is present"


async def test_unconfirmed_alerts_repeat_on_the_next_run(
    pipeline: tuple[Runner, FakeNotifier],
) -> None:
    """Repetition is what makes confirmation meaningful (CLAUDE.md §8.2a)."""
    runner, slack = pipeline
    await runner.run_once()
    first = len(slack.sent)
    assert first > 0
    await runner.run_once()
    assert len(slack.sent) == first * 2


async def test_confirming_stops_the_repetition(pipeline: tuple[Runner, FakeNotifier]) -> None:
    import time as _time

    runner, slack = pipeline
    report = await runner.run_once()
    sent_before = len(slack.sent)

    # Confirm every outstanding situation, as a receiver pressing Confirm would.
    store = runner._store
    for evaluation in report.evaluations:
        if evaluation.status is not Status.ACTIVE:
            await store.acknowledge(
                evaluation.ack_key,
                component_id=evaluation.component.component_id,
                status=evaluation.status.value,
                rung=evaluation.rung_key,
                fingerprint=evaluation.component.fingerprint,
                acked_by="receiver",
                ttl_seconds=7 * 86400,
                now=_time.time(),
            )

    await runner.run_once()
    assert len(slack.sent) == sent_before, "confirmed alerts must go quiet"


async def test_messages_carry_what_section_8_5_requires(
    pipeline: tuple[Runner, FakeNotifier],
) -> None:
    runner, slack = pipeline
    settings = make_settings(notify_routes_json=ROUTES)
    renderer = MessageRenderer(settings.timezone, "en")
    await runner.run_once()

    body = renderer.render(
        next(e for e in slack.sent if e.evaluation.status is Status.EXPIRED)
    ).body
    assert "cccccc" in body, "component name"
    assert "cccc" in body, "component id"
    assert "EXPIRED" in body
    assert "UTC+0300" in body, "expiry must carry its offset"


def test_no_real_account_data_in_the_fixture() -> None:
    """§3.1: fixtures are redacted. Guard against a live capture being pasted over this."""
    raw = FIXTURE.read_text()
    payload: dict[str, Any] = json.loads(raw)
    for item in payload["data"]["items"]:
        assert not item["orderId"] or item["orderId"].startswith("CBP0000"), item["orderId"]
        assert not item["id"] or item["id"].startswith("00000000-0000-4000-8000-"), item["id"]

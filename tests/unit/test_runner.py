"""Run-level behaviour: empty scrapes, parse failures, and exit codes (CLAUDE.md §6, §12)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tele_scraper import runner as runner_module
from tele_scraper.errors import ParseError, ScrapeError
from tele_scraper.models import Component, Status
from tele_scraper.notify.router import Router
from tele_scraper.runner import EMPTY_RESULT_ID, Runner
from tele_scraper.scraper.client import FixturePortalClient
from tele_scraper.state.store import MemoryStateStore
from tests.conftest import make_component, make_settings
from tests.unit.test_router import FakeNotifier

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


class ExplodingClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def fetch_components(self) -> str:
        raise self._exc

    async def aclose(self) -> None:
        return None


def build(
    tmp_path: Path, client: Any, renderer: Any, **overrides: Any
) -> tuple[Runner, FakeNotifier]:
    settings = make_settings(notify_routes_json=ROUTES, **overrides)
    store = MemoryStateStore()
    slack = FakeNotifier("slack")
    router = Router(settings, store, {"slack": slack}, renderer)
    return Runner(settings, client, store, router), slack


@pytest.fixture
def page(tmp_path: Path) -> Path:
    """A minimal valid envelope; the parser is stubbed in most of these tests."""
    path = tmp_path / "renewlist.json"
    path.write_text('{"status": 0, "data": {"total": 1, "items": [{"id": "x"}]}}')
    return path


async def test_happy_path_notifies_on_expiring(
    tmp_path: Path, page: Path, renderer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    components = [
        make_component("c1", expires_in=timedelta(days=200)),
        make_component("c2", expires_in=timedelta(days=2)),
    ]
    monkeypatch.setattr(runner_module, "parse_components", lambda _items: components)
    run, slack = build(tmp_path, FixturePortalClient(page), renderer)

    report = await run.run_once()

    assert report.exit_code == 0
    assert report.counts[Status.ACTIVE] == 1
    assert report.counts[Status.EXPIRING_SOON] == 1
    assert [e.evaluation.component.component_id for e in slack.sent] == ["c2"]


async def test_empty_scrape_is_a_run_level_unknown(
    tmp_path: Path, page: Path, renderer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero components is never 'all fine' (CLAUDE.md §6)."""
    monkeypatch.setattr(runner_module, "parse_components", lambda _items: [])
    run, slack = build(tmp_path, FixturePortalClient(page), renderer)

    report = await run.run_once()

    assert report.has_unknown is True
    assert report.exit_code == 2
    assert [e.evaluation.component.component_id for e in slack.sent] == [EMPTY_RESULT_ID]


async def test_parse_failure_is_unknown_and_still_alerts(
    tmp_path: Path, page: Path, renderer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_items: list[dict[str, object]]) -> list[Component]:
        raise ParseError("portal reported failure: status=500")

    monkeypatch.setattr(runner_module, "parse_components", boom)
    run, slack = build(tmp_path, FixturePortalClient(page), renderer)

    report = await run.run_once()

    assert report.exit_code == 2
    assert report.evaluations[0].status is Status.UNKNOWN
    assert len(slack.sent) == 1


async def test_scrape_failure_exits_one_and_sends_nothing(tmp_path: Path, renderer: Any) -> None:
    run, slack = build(tmp_path, ExplodingClient(ScrapeError("portal down")), renderer)
    report = await run.run_once()

    assert report.scrape_failed is True
    assert report.exit_code == 1
    assert slack.sent == []


async def test_missing_fixture_file_is_a_scrape_error(tmp_path: Path, renderer: Any) -> None:
    run, _ = build(tmp_path, FixturePortalClient(tmp_path / "nope.json"), renderer)
    report = await run.run_once()
    assert report.exit_code == 1


async def test_derived_expiry_is_flagged_on_the_report(
    tmp_path: Path, page: Path, renderer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import NOW

    component = Component(
        component_id="c3",
        name="Derived",
        activated_at=NOW - timedelta(days=29),
        validity_period=timedelta(days=30),
    )
    monkeypatch.setattr(runner_module, "parse_components", lambda _items: [component])
    run, _ = build(tmp_path, FixturePortalClient(page), renderer)

    report = await run.run_once()
    assert report.evaluations[0].component.expires_at_derived is True


async def test_delivery_failure_exits_three(
    tmp_path: Path, page: Path, renderer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner_module,
        "parse_components",
        lambda _html: [make_component("c1", expires_in=timedelta(days=-1))],
    )
    settings = make_settings(notify_routes_json=ROUTES)
    router = Router(
        settings, MemoryStateStore(), {"slack": FakeNotifier("slack", ok=False)}, renderer
    )
    run = Runner(settings, FixturePortalClient(page), MemoryStateStore(), router)

    assert (await run.run_once()).exit_code == 3


async def test_prune_state_is_safe_to_call(tmp_path: Path, page: Path, renderer: Any) -> None:
    run, _ = build(tmp_path, FixturePortalClient(page), renderer)
    await run.prune_state()


# --- a changing component list -----------------------------------------------------


def listing(*items: dict[str, Any]) -> str:
    return json.dumps({"status": 0, "data": {"total": len(items), "items": list(items)}})


def entry(cid: str, name: str, *, expired: bool) -> dict[str, Any]:
    return {
        "id": cid,
        "name": name,
        "serviceType": "EIP",
        "status": "Active",
        "purchaseTime": "2026-01-01 00:00:00.000 +0000 UTC",
        "expirationTime": (
            "2020-01-01 00:00:00.000 +0000 UTC" if expired else "2099-01-01 00:00:00.000 +0000 UTC"
        ),
        "validityPeriod": "1d",
    }


@pytest.fixture
def evolving(tmp_path: Path, renderer: Any) -> tuple[Runner, FakeNotifier, Path]:
    """A runner reading a listing file that a test can rewrite between runs."""
    path = tmp_path / "renewlist.json"
    path.write_text(listing())
    settings = make_settings(notify_routes_json=ROUTES, notify_enabled=True)
    store = MemoryStateStore()
    slack = FakeNotifier("slack")
    router = Router(settings, store, {"slack": slack}, renderer)
    return Runner(settings, FixturePortalClient(path), store, router), slack, path


async def test_a_new_component_is_picked_up_without_restarting(
    evolving: tuple[Runner, FakeNotifier, Path],
) -> None:
    runner, slack, path = evolving
    path.write_text(listing(entry("A", "alpha", expired=True)))
    await runner.run_once()
    assert {e.evaluation.component.component_id for e in slack.sent} == {"A"}

    path.write_text(listing(entry("A", "alpha", expired=True), entry("C", "charlie", expired=True)))
    await runner.run_once()
    assert "C" in {e.evaluation.component.component_id for e in slack.sent}


async def test_a_new_healthy_component_stays_quiet(
    evolving: tuple[Runner, FakeNotifier, Path],
) -> None:
    runner, slack, path = evolving
    path.write_text(listing(entry("A", "alpha", expired=True)))
    await runner.run_once()
    sent_before = len(slack.sent)

    path.write_text(listing(entry("A", "alpha", expired=True), entry("D", "delta", expired=False)))
    report = await runner.run_once()

    assert report.counts[Status.ACTIVE] == 1
    notified = {e.evaluation.component.component_id for e in slack.sent[sent_before:]}
    assert "D" not in notified, "ACTIVE is the quiet state"


async def test_a_vanished_component_is_raised_not_silently_dropped(
    evolving: tuple[Runner, FakeNotifier, Path],
) -> None:
    """The gap this was built for: losing sight of a component must never look healthy."""
    runner, slack, path = evolving
    path.write_text(listing(entry("A", "alpha", expired=True), entry("B", "bravo", expired=False)))
    await runner.run_once()
    sent_before = len(slack.sent)

    path.write_text(listing(entry("A", "alpha", expired=True)))
    report = await runner.run_once()

    vanished = next(e for e in report.evaluations if e.component.component_id == "B")
    assert vanished.status is Status.UNKNOWN
    assert "no longer listed" in (vanished.component.parse_error or "")
    assert "B" in {e.evaluation.component.component_id for e in slack.sent[sent_before:]}
    assert report.exit_code == 2


async def test_a_vanished_component_keeps_its_name_for_the_message(
    evolving: tuple[Runner, FakeNotifier, Path],
) -> None:
    runner, _slack, path = evolving
    path.write_text(listing(entry("B", "bravo", expired=False)))
    await runner.run_once()
    path.write_text(listing())
    report = await runner.run_once()
    assert any("bravo" in e.component.name for e in report.evaluations)


async def test_confirming_a_disappearance_ends_it_permanently(
    evolving: tuple[Runner, FakeNotifier, Path],
) -> None:
    """Confirming must forget the component, not merely silence it every run."""
    import time as _time

    runner, _slack, path = evolving
    path.write_text(listing(entry("A", "alpha", expired=True), entry("B", "bravo", expired=False)))
    await runner.run_once()

    path.write_text(listing(entry("A", "alpha", expired=True)))
    report = await runner.run_once()
    vanished = next(e for e in report.evaluations if e.component.component_id == "B")

    await runner._store.acknowledge(
        vanished.ack_key,
        component_id="B",
        status=vanished.status.value,
        rung=vanished.rung_key,
        fingerprint=vanished.component.fingerprint,
        acked_by="ops",
        ttl_seconds=7 * 86400,
        now=_time.time(),
    )

    after = await runner.run_once()
    assert "B" not in {e.component.component_id for e in after.evaluations}
    # And it stays gone once forgotten, even after the acknowledgement would have lapsed.
    assert "B" not in {e.component.component_id for e in (await runner.run_once()).evaluations}


async def test_the_first_run_reports_nothing_as_missing(
    evolving: tuple[Runner, FakeNotifier, Path],
) -> None:
    runner, _slack, path = evolving
    path.write_text(listing(entry("A", "alpha", expired=False)))
    report = await runner.run_once()
    assert [e.component.component_id for e in report.evaluations] == ["A"]


async def test_positional_sentinels_are_never_tracked(tmp_path: Path, renderer: Any) -> None:
    """An entry with no id gets a positional label; position is not identity."""
    path = tmp_path / "renewlist.json"
    settings = make_settings(notify_routes_json=ROUTES, notify_enabled=True)
    store = MemoryStateStore()
    router = Router(settings, store, {"slack": FakeNotifier("slack")}, renderer)
    runner = Runner(settings, FixturePortalClient(path), store, router)

    path.write_text(listing({"id": "", "name": "nameless", "expirationTime": ""}))
    await runner.run_once()
    path.write_text(listing(entry("A", "alpha", expired=False)))
    report = await runner.run_once()

    assert not [
        e for e in report.evaluations if e.component.component_id.startswith("unidentified-item-")
    ], "a positional sentinel must not be remembered and then reported missing"


async def test_detection_can_be_switched_off(tmp_path: Path, renderer: Any) -> None:
    path = tmp_path / "renewlist.json"
    settings = make_settings(
        notify_routes_json=ROUTES, notify_enabled=True, detect_missing_components=False
    )
    store = MemoryStateStore()
    router = Router(settings, store, {"slack": FakeNotifier("slack")}, renderer)
    runner = Runner(settings, FixturePortalClient(path), store, router)

    path.write_text(listing(entry("A", "alpha", expired=False)))
    await runner.run_once()
    path.write_text(listing())
    report = await runner.run_once()
    assert report.evaluations[0].component.component_id == EMPTY_RESULT_ID

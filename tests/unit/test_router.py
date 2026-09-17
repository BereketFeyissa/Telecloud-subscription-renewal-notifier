"""Routing, dedup, quiet hours, and failure isolation (CLAUDE.md §8)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tele_scraper.domain.status import evaluate_all
from tele_scraper.errors import StateStoreError
from tele_scraper.models import DeliveryResult, NotificationEvent
from tele_scraper.notify.router import Router
from tele_scraper.state.store import MemoryStateStore
from tests.conftest import NOW, make_component, make_settings

LADDER = (timedelta(days=14), timedelta(days=7), timedelta(days=3), timedelta(days=1))


class FakeNotifier:
    """Records what it was asked to send."""

    def __init__(self, name: str, *, ok: bool = True, raises: Exception | None = None) -> None:
        self.name = name
        self.sent: list[NotificationEvent] = []
        self._ok = ok
        self._raises = raises

    async def send(self, event: NotificationEvent) -> DeliveryResult:
        if self._raises is not None:
            raise self._raises
        self.sent.append(event)
        return (
            DeliveryResult.success(event)
            if self._ok
            else DeliveryResult.failure(event, "rejected by fake")
        )

    async def aclose(self) -> None:
        return None


def routes_json(**overrides: Any) -> str:
    route: dict[str, Any] = {
        "recipient": "ops",
        "channels": [{"channel": "slack", "address": "https://hooks.test/ops"}],
        "statuses": ["EXPIRED", "EXPIRING_SOON", "UNKNOWN", "SUSPENDED"],
        "components": ["*"],
    }
    route.update(overrides)
    return json.dumps({"routes": [route]})


def build_router(settings, store, notifiers, renderer) -> Router:  # type: ignore[no-untyped-def]
    return Router(settings, store, notifiers, renderer)


def evaluate(components):  # type: ignore[no-untyped-def]
    return evaluate_all(components, now=NOW, thresholds=LADDER)


# --- planning ----------------------------------------------------------------------


def test_active_components_are_never_routed(renderer) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(notify_routes_json=routes_json())
    router = build_router(settings, MemoryStateStore(), {}, renderer)
    assert router.plan(evaluate([make_component(expires_in=timedelta(days=90))])) == []


def test_routes_filter_by_status(renderer) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(notify_routes_json=routes_json(statuses=["EXPIRED"]))
    router = build_router(settings, MemoryStateStore(), {}, renderer)
    assert router.plan(evaluate([make_component(expires_in=timedelta(days=2))])) == []
    assert len(router.plan(evaluate([make_component(expires_in=timedelta(days=-2))]))) == 1


def test_routes_filter_by_component_glob(renderer) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(notify_routes_json=routes_json(components=["db-*"]))
    router = build_router(settings, MemoryStateStore(), {}, renderer)
    assert router.plan(evaluate([make_component("web-1", expires_in=timedelta(days=-1))])) == []
    assert len(router.plan(evaluate([make_component("db-1", expires_in=timedelta(days=-1))]))) == 1


def test_one_recipient_fans_out_to_every_channel(renderer) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(
        notify_routes_json=routes_json(
            channels=[
                {"channel": "slack", "address": "https://hooks.test/ops"},
                {"channel": "telegram", "address": "12345"},
                {"channel": "email", "address": "ops@example.test"},
            ]
        )
    )
    router = build_router(settings, MemoryStateStore(), {}, renderer)
    events = router.plan(evaluate([make_component(expires_in=timedelta(days=-1))]))
    assert sorted(e.target.channel for e in events) == ["email", "slack", "telegram"]


# --- dispatch ----------------------------------------------------------------------


async def test_unacknowledged_alerts_repeat_every_run(renderer) -> None:  # type: ignore[no-untyped-def]
    """Repetition is the pressure that makes confirmation mean something (CLAUDE.md §8.2a)."""
    settings = make_settings(notify_routes_json=routes_json())
    slack = FakeNotifier("slack")
    router = build_router(settings, MemoryStateStore(), {"slack": slack}, renderer)
    evaluations = evaluate([make_component(expires_in=timedelta(days=-1))])

    for _ in range(3):
        await router.dispatch(evaluations, now=NOW)
    assert len(slack.sent) == 3


async def test_acknowledgement_silences_that_exact_situation(renderer) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(notify_routes_json=routes_json())
    store = MemoryStateStore()
    slack = FakeNotifier("slack")
    router = build_router(settings, store, {"slack": slack}, renderer)
    evaluations = evaluate([make_component(expires_in=timedelta(days=-1))])

    await router.dispatch(evaluations, now=NOW)
    assert len(slack.sent) == 1

    evaluation = evaluations[0]
    await store.acknowledge(
        evaluation.ack_key,
        component_id=evaluation.component.component_id,
        status=evaluation.status.value,
        rung=evaluation.rung_key,
        fingerprint=evaluation.component.fingerprint,
        acked_by="tester",
        ttl_seconds=7 * 86400,
        now=time.time(),
    )

    results = await router.dispatch(evaluations, now=NOW)
    assert results[0].error == "acknowledged"
    assert len(slack.sent) == 1


async def test_one_acknowledgement_clears_every_recipient(renderer) -> None:  # type: ignore[no-untyped-def]
    """Slack/discord recipients cannot confirm, so an ack must not be per-channel."""
    settings = make_settings(
        notify_routes_json=routes_json(
            channels=[
                {"channel": "slack", "address": "https://hooks.test/ops"},
                {"channel": "telegram", "address": "123"},
            ]
        )
    )
    store = MemoryStateStore()
    slack, telegram = FakeNotifier("slack"), FakeNotifier("telegram")
    router = build_router(settings, store, {"slack": slack, "telegram": telegram}, renderer)
    evaluations = evaluate([make_component(expires_in=timedelta(days=-1))])

    await router.dispatch(evaluations, now=NOW)
    evaluation = evaluations[0]
    await store.acknowledge(
        evaluation.ack_key,
        component_id=evaluation.component.component_id,
        status=evaluation.status.value,
        rung=evaluation.rung_key,
        fingerprint=evaluation.component.fingerprint,
        acked_by="someone-on-telegram",
        ttl_seconds=7 * 86400,
        now=time.time(),
    )
    results = await router.dispatch(evaluations, now=NOW)

    assert {r.error for r in results} == {"acknowledged"}
    assert len(slack.sent) == 1 and len(telegram.sent) == 1


async def test_a_lapsed_acknowledgement_lets_the_alert_return(renderer) -> None:  # type: ignore[no-untyped-def]
    """An ack is a snooze, never permanent silence."""
    settings = make_settings(notify_routes_json=routes_json())
    store = MemoryStateStore()
    slack = FakeNotifier("slack")
    router = build_router(settings, store, {"slack": slack}, renderer)
    evaluations = evaluate([make_component(expires_in=timedelta(days=-1))])
    evaluation = evaluations[0]

    await store.acknowledge(
        evaluation.ack_key,
        component_id=evaluation.component.component_id,
        status=evaluation.status.value,
        rung=evaluation.rung_key,
        fingerprint=evaluation.component.fingerprint,
        acked_by="tester",
        ttl_seconds=-1,  # already lapsed
        now=time.time(),
    )
    await router.dispatch(evaluations, now=NOW)
    assert len(slack.sent) == 1


async def test_changed_data_invalidates_an_acknowledgement(renderer) -> None:  # type: ignore[no-untyped-def]
    """An ack covers a situation, not a component: a renewal landing re-arms the alert."""
    settings = make_settings(notify_routes_json=routes_json())
    store = MemoryStateStore()
    slack = FakeNotifier("slack")
    router = build_router(settings, store, {"slack": slack}, renderer)

    before = evaluate([make_component(expires_in=timedelta(days=-1))])
    await router.dispatch(before, now=NOW)
    evaluation = before[0]
    await store.acknowledge(
        evaluation.ack_key,
        component_id=evaluation.component.component_id,
        status=evaluation.status.value,
        rung=evaluation.rung_key,
        fingerprint=evaluation.component.fingerprint,
        acked_by="tester",
        ttl_seconds=7 * 86400,
        now=time.time(),
    )
    assert (await router.dispatch(before, now=NOW))[0].error == "acknowledged"

    # Same component and status, different expiry: the facts moved, so it speaks up again.
    after = evaluate([make_component(expires_in=timedelta(days=-2))])
    await router.dispatch(after, now=NOW)
    assert len(slack.sent) == 2


async def test_a_tighter_rung_re_arms_without_any_data_change(renderer) -> None:  # type: ignore[no-untyped-def]
    """Acking at 14d must not swallow the 3d warning."""
    settings = make_settings(notify_routes_json=routes_json())
    store = MemoryStateStore()
    slack = FakeNotifier("slack")
    router = build_router(settings, store, {"slack": slack}, renderer)

    wide = evaluate([make_component(expires_in=timedelta(days=10))])
    await router.dispatch(wide, now=NOW)
    evaluation = wide[0]
    await store.acknowledge(
        evaluation.ack_key,
        component_id=evaluation.component.component_id,
        status=evaluation.status.value,
        rung=evaluation.rung_key,
        fingerprint=evaluation.component.fingerprint,
        acked_by="tester",
        ttl_seconds=7 * 86400,
        now=time.time(),
    )
    tight = evaluate([make_component(expires_in=timedelta(days=2))])
    await router.dispatch(tight, now=NOW)
    assert len(slack.sent) == 2


async def test_a_new_rung_breaks_the_dedup(renderer) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(notify_routes_json=routes_json())
    store = MemoryStateStore()
    slack = FakeNotifier("slack")
    router = build_router(settings, store, {"slack": slack}, renderer)

    await router.dispatch(evaluate([make_component(expires_in=timedelta(days=5))]), now=NOW)
    await router.dispatch(evaluate([make_component(expires_in=timedelta(days=2))]), now=NOW)
    assert [e.evaluation.rung for e in slack.sent] == [timedelta(days=7), timedelta(days=3)]


async def test_quiet_hours_hold_non_critical_but_not_critical(renderer) -> None:  # type: ignore[no-untyped-def]
    quiet = {"start": "22:00", "end": "06:00", "tz": "Africa/Addis_Ababa"}
    settings = make_settings(notify_routes_json=routes_json(quiet_hours=quiet))
    slack = FakeNotifier("slack")
    router = build_router(settings, MemoryStateStore(), {"slack": slack}, renderer)
    # 23:00 local == 20:00 UTC
    night = datetime(2026, 9, 16, 20, 0, tzinfo=UTC)

    held = await router.dispatch(
        evaluate([make_component(expires_in=timedelta(days=2))]), now=night
    )
    assert held[0].suppressed is True
    assert held[0].error == "quiet_hours"
    assert slack.sent == []

    paged = await router.dispatch(
        evaluate([make_component("c2", expires_in=timedelta(days=-1))]), now=night
    )
    assert paged[0].suppressed is False
    assert len(slack.sent) == 1


async def test_quiet_hours_do_not_record_dedup(renderer) -> None:  # type: ignore[no-untyped-def]
    """A held alert must still go out once the window closes, not be lost."""
    quiet = {"start": "22:00", "end": "06:00", "tz": "Africa/Addis_Ababa"}
    settings = make_settings(notify_routes_json=routes_json(quiet_hours=quiet))
    slack = FakeNotifier("slack")
    router = build_router(settings, MemoryStateStore(), {"slack": slack}, renderer)
    evaluations = evaluate([make_component(expires_in=timedelta(days=2))])

    await router.dispatch(evaluations, now=datetime(2026, 9, 16, 20, 0, tzinfo=UTC))
    await router.dispatch(evaluations, now=datetime(2026, 9, 16, 9, 0, tzinfo=UTC))
    assert len(slack.sent) == 1


async def test_notify_disabled_sends_nothing(renderer) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(notify_routes_json=routes_json(), notify_enabled=False)
    slack = FakeNotifier("slack")
    router = build_router(settings, MemoryStateStore(), {"slack": slack}, renderer)
    results = await router.dispatch(
        evaluate([make_component(expires_in=timedelta(days=-1))]), now=NOW
    )
    assert results[0].error == "notify_disabled"
    assert slack.sent == []


async def test_dry_run_sends_nothing(renderer) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(notify_routes_json=routes_json(), dry_run=True)
    slack = FakeNotifier("slack")
    router = build_router(settings, MemoryStateStore(), {"slack": slack}, renderer)
    results = await router.dispatch(
        evaluate([make_component(expires_in=timedelta(days=-1))]), now=NOW
    )
    assert results[0].error == "dry_run"
    assert slack.sent == []


async def test_unconfigured_channel_fails_without_aborting_the_rest(renderer) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(
        notify_routes_json=routes_json(
            channels=[
                {"channel": "sms", "address": "+251900000000"},
                {"channel": "slack", "address": "https://hooks.test/ops"},
            ]
        )
    )
    slack = FakeNotifier("slack")
    router = build_router(settings, MemoryStateStore(), {"slack": slack}, renderer)
    results = await router.dispatch(
        evaluate([make_component(expires_in=timedelta(days=-1))]), now=NOW
    )

    by_channel = {r.channel: r for r in results}
    assert by_channel["sms"].ok is False
    assert "not configured" in (by_channel["sms"].error or "")
    assert by_channel["slack"].ok is True
    assert len(slack.sent) == 1


async def test_a_raising_notifier_does_not_sink_the_run(renderer) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(
        notify_routes_json=routes_json(
            channels=[
                {"channel": "telegram", "address": "123"},
                {"channel": "slack", "address": "https://hooks.test/ops"},
            ]
        )
    )
    slack = FakeNotifier("slack")
    broken = FakeNotifier("telegram", raises=RuntimeError("boom"))
    router = build_router(
        settings, MemoryStateStore(), {"slack": slack, "telegram": broken}, renderer
    )
    results = await router.dispatch(
        evaluate([make_component(expires_in=timedelta(days=-1))]), now=NOW
    )

    by_channel = {r.channel: r for r in results}
    assert by_channel["telegram"].ok is False
    assert by_channel["slack"].ok is True


async def test_failed_delivery_is_not_recorded_as_sent(renderer) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(notify_routes_json=routes_json())
    store = MemoryStateStore()
    slack = FakeNotifier("slack", ok=False)
    router = build_router(settings, store, {"slack": slack}, renderer)
    evaluations = evaluate([make_component(expires_in=timedelta(days=-1))])

    await router.dispatch(evaluations, now=NOW)
    await router.dispatch(evaluations, now=NOW)
    assert len(slack.sent) == 2, "a failed send must be retried on the next cycle, not deduped"


async def test_state_store_failure_aborts_the_run(renderer) -> None:  # type: ignore[no-untyped-def]
    """Without dedup we cannot tell a repeat from a new alert, so we stop (§8.2)."""

    class BrokenStore(MemoryStateStore):
        async def is_acknowledged(self, ack_key: str, fingerprint: str, *, now: float) -> bool:
            raise StateStoreError("disk gone")

    settings = make_settings(notify_routes_json=routes_json())
    router = build_router(settings, BrokenStore(), {"slack": FakeNotifier("slack")}, renderer)
    with pytest.raises(StateStoreError):
        await router.dispatch(evaluate([make_component(expires_in=timedelta(days=-1))]), now=NOW)


async def test_nothing_to_send_is_not_an_error(renderer) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(notify_routes_json=routes_json())
    router = build_router(settings, MemoryStateStore(), {}, renderer)
    assert (
        await router.dispatch(evaluate([make_component(expires_in=timedelta(days=90))]), now=NOW)
        == []
    )


async def test_dry_run_previews_even_when_notifications_are_disabled(renderer) -> None:  # type: ignore[no-untyped-def]
    """--dry-run must show intended sends without requiring NOTIFY_ENABLED=true first."""
    settings = make_settings(notify_routes_json=routes_json(), notify_enabled=False, dry_run=True)
    slack = FakeNotifier("slack")
    router = build_router(settings, MemoryStateStore(), {"slack": slack}, renderer)
    results = await router.dispatch(
        evaluate([make_component(expires_in=timedelta(days=-1))]), now=NOW
    )
    assert results[0].error == "dry_run", "a dry run must not be masked by notify_disabled"
    assert slack.sent == []

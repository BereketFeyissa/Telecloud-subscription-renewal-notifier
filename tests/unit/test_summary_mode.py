"""Summary mode: one message per status group instead of one per component."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any

from tele_scraper.domain.status import evaluate_all
from tele_scraper.models import Status
from tele_scraper.notify.base import MessageRenderer
from tele_scraper.notify.router import Router
from tele_scraper.state.store import MemoryStateStore
from tests.conftest import NOW, make_component, make_settings
from tests.unit.test_router import FakeNotifier

LADDER = (timedelta(days=14), timedelta(days=7), timedelta(days=3), timedelta(days=1))


def routes(mode: str = "summary", ack: str = "components", **extra: Any) -> str:
    route: dict[str, Any] = {
        "recipient": "ops",
        "channels": [{"channel": "slack", "address": "https://hooks.test/ops"}],
        "statuses": ["EXPIRED", "EXPIRING_SOON", "UNKNOWN", "SUSPENDED"],
        "mode": mode,
        "summary_ack": ack,
    }
    route.update(extra)
    return json.dumps({"routes": [route]})


def build(renderer: MessageRenderer, mode: str = "summary", ack: str = "components"):  # type: ignore[no-untyped-def]
    settings = make_settings(notify_routes_json=routes(mode, ack), notify_enabled=True)
    store = MemoryStateStore()
    slack = FakeNotifier("slack")
    return Router(settings, store, {"slack": slack}, renderer), slack, store


def mixed() -> list:  # type: ignore[type-arg]
    return evaluate_all(
        [
            make_component("exp-1", "alpha", expires_in=timedelta(days=-1)),
            make_component("exp-2", "bravo", expires_in=timedelta(days=-2)),
            make_component("soon-1", "charlie", expires_in=timedelta(days=2)),
            make_component("ok-1", "delta", expires_in=timedelta(days=200)),
        ],
        now=NOW,
        thresholds=LADDER,
    )


# --- grouping ----------------------------------------------------------------------


def test_summary_sends_one_message_per_status(renderer: MessageRenderer) -> None:
    router, _slack, _store = build(renderer)
    events = router.plan(mixed())
    assert len(events) == 2, "one EXPIRED digest, one EXPIRING_SOON digest"
    by_status = {e.status: e for e in events}
    assert {s.value for s in by_status} == {"EXPIRED", "EXPIRING_SOON"}
    assert len(by_status[Status.EXPIRED].evaluations) == 2
    assert len(by_status[Status.EXPIRING_SOON].evaluations) == 1


def test_detailed_still_sends_one_message_per_component(renderer: MessageRenderer) -> None:
    router, _slack, _store = build(renderer, mode="detailed")
    assert len(router.plan(mixed())) == 3, "three notifiable components"


def test_detailed_is_the_default(renderer: MessageRenderer) -> None:
    """Existing routes must not change behaviour (CLAUDE.md §8)."""
    settings = make_settings(
        notify_routes_json=json.dumps(
            {
                "routes": [
                    {
                        "recipient": "ops",
                        "channels": [{"channel": "slack", "address": "https://x.test"}],
                    }
                ]
            }
        )
    )
    route = settings.routing.routes[0]
    assert route.mode == "detailed"
    assert route.summary_ack == "components"


def test_an_empty_status_group_produces_no_message(renderer: MessageRenderer) -> None:
    """Silence when nothing is wrong."""
    router, _slack, _store = build(renderer)
    healthy = evaluate_all(
        [make_component("ok", expires_in=timedelta(days=300))], now=NOW, thresholds=LADDER
    )
    assert router.plan(healthy) == []


def test_statuses_are_never_mixed_in_one_message(renderer: MessageRenderer) -> None:
    """EXPIRED ignores quiet hours and EXPIRING_SOON does not; one message cannot do both."""
    router, _slack, _store = build(renderer)
    for event in router.plan(mixed()):
        assert len({e.status for e in event.evaluations}) == 1


def test_a_digest_is_critical_if_anything_in_it_is(renderer: MessageRenderer) -> None:
    router, _slack, _store = build(renderer)
    expired = next(e for e in router.plan(mixed()) if e.status is Status.EXPIRED)
    assert expired.is_critical is True


# --- ack mode: components ----------------------------------------------------------


async def test_components_mode_shrinks_the_digest_as_items_are_confirmed(
    renderer: MessageRenderer,
) -> None:
    router, slack, store = build(renderer, ack="components")
    evaluations = mixed()
    await router.dispatch(evaluations, now=NOW)
    first = next(e for e in slack.sent if e.status is Status.EXPIRED)
    assert len(first.evaluations) == 2

    confirmed = first.evaluations[0]
    await store.acknowledge(
        confirmed.ack_key,
        component_id=confirmed.component.component_id,
        status=confirmed.status.value,
        rung=confirmed.rung_key,
        fingerprint=confirmed.component.fingerprint,
        acked_by="ops",
        ttl_seconds=7 * 86400,
        now=time.time(),
    )

    slack.sent.clear()
    await router.dispatch(evaluations, now=NOW)
    second = next(e for e in slack.sent if e.status is Status.EXPIRED)
    assert len(second.evaluations) == 1, "the confirmed item drops out"
    assert second.evaluations[0].component.component_id != confirmed.component.component_id


async def test_components_mode_goes_quiet_once_everything_is_confirmed(
    renderer: MessageRenderer,
) -> None:
    router, slack, store = build(renderer, ack="components")
    evaluations = mixed()
    await router.dispatch(evaluations, now=NOW)

    for ev in evaluations:
        if ev.status is not Status.ACTIVE:
            await store.acknowledge(
                ev.ack_key,
                component_id=ev.component.component_id,
                status=ev.status.value,
                rung=ev.rung_key,
                fingerprint=ev.component.fingerprint,
                acked_by="ops",
                ttl_seconds=7 * 86400,
                now=time.time(),
            )
    slack.sent.clear()
    results = await router.dispatch(evaluations, now=NOW)
    assert slack.sent == []
    assert all(r.error == "acknowledged" for r in results)


async def test_components_mode_records_membership_for_the_confirm_button(
    renderer: MessageRenderer,
) -> None:
    """Telegram's callback_data cannot carry a component list, so it is stored."""
    router, _slack, store = build(renderer, ack="components")
    await router.dispatch(mixed(), now=NOW)
    members = await store.find_digest_members("__digest__|EXPIRED")
    assert len(members) == 2


# --- ack mode: digest --------------------------------------------------------------


async def test_digest_mode_acknowledges_the_set_as_a_unit(renderer: MessageRenderer) -> None:
    router, slack, store = build(renderer, ack="digest")
    evaluations = mixed()
    await router.dispatch(evaluations, now=NOW)
    sent = next(e for e in slack.sent if e.status is Status.EXPIRED)

    await store.acknowledge(
        sent.digest_key,
        component_id=sent.digest_key,
        status="EXPIRED",
        rung="-",
        fingerprint=sent.digest_fingerprint,
        acked_by="ops",
        ttl_seconds=7 * 86400,
        now=time.time(),
    )
    slack.sent.clear()
    await router.dispatch(evaluations, now=NOW)
    assert not [e for e in slack.sent if e.status is Status.EXPIRED]


async def test_digest_mode_re_sends_in_full_when_the_set_changes(
    renderer: MessageRenderer,
) -> None:
    router, slack, store = build(renderer, ack="digest")
    await router.dispatch(mixed(), now=NOW)
    sent = next(e for e in slack.sent if e.status is Status.EXPIRED)
    await store.acknowledge(
        sent.digest_key,
        component_id=sent.digest_key,
        status="EXPIRED",
        rung="-",
        fingerprint=sent.digest_fingerprint,
        acked_by="ops",
        ttl_seconds=7 * 86400,
        now=time.time(),
    )

    # A third component expires: the set has changed, so the ack no longer applies.
    grown = evaluate_all(
        [
            make_component("exp-1", "alpha", expires_in=timedelta(days=-1)),
            make_component("exp-2", "bravo", expires_in=timedelta(days=-2)),
            make_component("exp-3", "echo", expires_in=timedelta(days=-3)),
        ],
        now=NOW,
        thresholds=LADDER,
    )
    slack.sent.clear()
    await router.dispatch(grown, now=NOW)
    resent = next(e for e in slack.sent if e.status is Status.EXPIRED)
    assert len(resent.evaluations) == 3, "the whole group is re-sent, not just the new item"


# --- ack mode: none ----------------------------------------------------------------


async def test_none_mode_repeats_every_run_and_is_never_suppressed(
    renderer: MessageRenderer,
) -> None:
    router, slack, _store = build(renderer, ack="none")
    evaluations = mixed()
    for _ in range(3):
        await router.dispatch(evaluations, now=NOW)
    assert len([e for e in slack.sent if e.status is Status.EXPIRED]) == 3


async def test_none_mode_offers_no_confirm_button(renderer: MessageRenderer) -> None:
    import httpx
    import respx

    from tele_scraper.notify.telegram import TelegramNotifier

    router, _slack, _store = build(renderer, ack="none")
    event = next(e for e in router.plan(mixed()) if e.status is Status.EXPIRED)
    token = "123456:AAH-fake"
    with respx.mock:
        route = respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with httpx.AsyncClient() as client:
            telegram_event = event.model_copy(
                update={
                    "target": event.target.model_copy(
                        update={"channel": "telegram", "address": "1"}
                    )
                }
            )
            await TelegramNotifier(client, renderer, bot_token=token).send(telegram_event)
    assert "reply_markup" not in json.loads(route.calls[0].request.content)


# --- rendering ---------------------------------------------------------------------


def test_a_digest_lists_every_component(renderer: MessageRenderer) -> None:
    router, _slack, _store = build(renderer)
    event = next(e for e in router.plan(mixed()) if e.status is Status.EXPIRED)
    body = renderer.render(event).body
    assert "alpha" in body and "bravo" in body
    assert "exp-1" in body and "exp-2" in body
    assert "UTC+0300" in body, "expiries still carry their offset (§8.5)"


def test_a_digest_subject_says_how_many(renderer: MessageRenderer) -> None:
    router, _slack, _store = build(renderer)
    event = next(e for e in router.plan(mixed()) if e.status is Status.EXPIRED)
    assert "2" in renderer.render(event).subject
    assert "EXPIRED" in renderer.render(event).subject

"""Acknowledgement listener: Telegram Confirm buttons via outbound-only long polling."""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
import respx

from tele_scraper.notify.acks import TelegramAckListener, parse_ack_key
from tele_scraper.notify.telegram import ACK_PREFIX, MAX_CALLBACK_DATA, ack_callback_data
from tele_scraper.state.store import MemoryStateStore
from tests.conftest import make_settings

TOKEN = "123456:AAH-fake-bot-token"
API = f"https://api.telegram.org/bot{TOKEN}"
ACK_KEY = "comp-1|EXPIRED|-"


def updates(*items: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "result": list(items)}


def press(ack_key: str, update_id: int = 1, username: str = "amanuel") -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": 42, "username": username},
            "data": f"{ACK_PREFIX}{ack_key}",
        },
    }


# --- callback payloads -------------------------------------------------------------


def test_callback_data_round_trips() -> None:
    payload = ack_callback_data(ACK_KEY)
    assert payload is not None
    assert parse_ack_key(payload) == ACK_KEY


def test_a_uuid_sized_key_fits_telegrams_limit() -> None:
    key = "91528149-5c12-4fa9-98c1-bc1d6ac4daf6|EXPIRED|-"
    payload = ack_callback_data(key)
    assert payload is not None
    assert len(payload.encode()) <= MAX_CALLBACK_DATA


def test_an_oversized_key_yields_no_button_rather_than_a_truncated_one() -> None:
    """A clipped key would confirm the wrong situation, which is worse than no button."""
    assert ack_callback_data("x" * 200) is None


@pytest.mark.parametrize(
    "data", ["", "hello", "ack:", "ack:only-one-part", "ack:a|b", "other:a|b|c"]
)
def test_unrecognised_payloads_are_refused(data: str) -> None:
    assert parse_ack_key(data) is None


# --- listener ----------------------------------------------------------------------


async def build(store: MemoryStateStore, client: httpx.AsyncClient) -> TelegramAckListener:
    settings = make_settings(telegram_bot_token=TOKEN)
    return TelegramAckListener(settings, store, client)


@respx.mock
async def test_a_confirm_press_is_recorded() -> None:
    store = MemoryStateStore()
    await store.record_sent(
        "scope",
        "key",
        recipient="ops",
        channel="telegram",
        component_id="comp-1",
        status="EXPIRED",
        rung="-",
        ack_key=ACK_KEY,
        fingerprint="fp123",
    )
    respx.get(f"{API}/getUpdates").mock(
        return_value=httpx.Response(200, json=updates(press(ACK_KEY)))
    )
    answered = respx.post(f"{API}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    async with httpx.AsyncClient() as client:
        listener = await build(store, client)
        assert await listener.poll_once() == 1

    assert await store.is_acknowledged(ACK_KEY, "fp123", now=time.time()) is True
    assert answered.called, "the button must stop spinning"


@respx.mock
async def test_the_ack_uses_the_fingerprint_that_was_notified() -> None:
    """Confirming means 'I saw THAT'. If the data has since moved, the alert must return."""
    store = MemoryStateStore()
    await store.record_sent(
        "scope",
        "key",
        recipient="ops",
        channel="telegram",
        component_id="comp-1",
        status="EXPIRED",
        rung="-",
        ack_key=ACK_KEY,
        fingerprint="as-notified",
    )
    respx.get(f"{API}/getUpdates").mock(
        return_value=httpx.Response(200, json=updates(press(ACK_KEY)))
    )
    respx.post(f"{API}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    async with httpx.AsyncClient() as client:
        await (await build(store, client)).poll_once()

    assert await store.is_acknowledged(ACK_KEY, "as-notified", now=time.time()) is True
    assert await store.is_acknowledged(ACK_KEY, "data-has-moved", now=time.time()) is False


@respx.mock
async def test_a_press_for_a_situation_we_never_sent_is_refused() -> None:
    store = MemoryStateStore()
    respx.get(f"{API}/getUpdates").mock(
        return_value=httpx.Response(200, json=updates(press("ghost|EXPIRED|-")))
    )
    respx.post(f"{API}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    async with httpx.AsyncClient() as client:
        await (await build(store, client)).poll_once()

    assert await store.is_acknowledged("ghost|EXPIRED|-", "any", now=time.time()) is False


@respx.mock
async def test_the_offset_advances_so_presses_are_not_replayed() -> None:
    store = MemoryStateStore()
    route = respx.get(f"{API}/getUpdates").mock(
        side_effect=[
            httpx.Response(200, json=updates(press(ACK_KEY, update_id=7))),
            httpx.Response(200, json=updates()),
        ]
    )
    respx.post(f"{API}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    async with httpx.AsyncClient() as client:
        listener = await build(store, client)
        await listener.poll_once()
        await listener.poll_once()

    assert dict(route.calls[0].request.url.params)["offset"] == "0"
    assert dict(route.calls[1].request.url.params)["offset"] == "8"


@respx.mock
async def test_only_callback_updates_are_requested() -> None:
    store = MemoryStateStore()
    route = respx.get(f"{API}/getUpdates").mock(return_value=httpx.Response(200, json=updates()))
    async with httpx.AsyncClient() as client:
        await (await build(store, client)).poll_once()
    assert dict(route.calls[0].request.url.params)["allowed_updates"] == '["callback_query"]'


@respx.mock
async def test_non_callback_updates_are_ignored() -> None:
    store = MemoryStateStore()
    respx.get(f"{API}/getUpdates").mock(
        return_value=httpx.Response(200, json=updates({"update_id": 3, "message": {"text": "hi"}}))
    )
    async with httpx.AsyncClient() as client:
        assert await (await build(store, client)).poll_once() == 1


@respx.mock
async def test_a_failed_poll_raises_for_the_loop_to_handle() -> None:
    store = MemoryStateStore()
    respx.get(f"{API}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": False, "description": "unauthorized"})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="unauthorized"):
            await (await build(store, client)).poll_once()


@respx.mock
async def test_http_errors_raise_for_the_loop_to_handle() -> None:
    store = MemoryStateStore()
    respx.get(f"{API}/getUpdates").mock(return_value=httpx.Response(401))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await (await build(store, client)).poll_once()


@respx.mock
async def test_the_loop_survives_a_bad_poll_and_stops_when_asked() -> None:
    store = MemoryStateStore()
    respx.get(f"{API}/getUpdates").mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as client:
        listener = await build(store, client)
        listener.request_stop()
        await listener.run_forever()  # returns rather than looping forever

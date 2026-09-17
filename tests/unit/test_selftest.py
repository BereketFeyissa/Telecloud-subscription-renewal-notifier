"""Channel self-test: proving credentials work before a real alert depends on them."""

from __future__ import annotations

import json
import smtplib

import httpx
import pytest
import respx

from tele_scraper.models import ChannelTarget, Status
from tele_scraper.notify.base import MessageRenderer
from tele_scraper.notify.registry import build_notifiers, build_test_event, send_test_messages
from tests.conftest import make_settings
from tests.unit.test_notifiers import FakeSmtp

TOKEN = "123456:AAH-fake-bot-token"


def routes(*channels: dict[str, str], recipient: str = "ops") -> str:
    return json.dumps({"routes": [{"recipient": recipient, "channels": list(channels)}]})


def test_the_test_event_is_unmistakably_a_test() -> None:
    event = build_test_event("ops", ChannelTarget(channel="slack", address="https://x.test"), "en")
    assert "TEST MESSAGE" in event.evaluation.component.name
    assert event.evaluation.status is Status.EXPIRING_SOON


def test_the_test_message_renders_through_the_real_templates(renderer: MessageRenderer) -> None:
    """A self-test that used a different code path would prove nothing about real alerts."""
    event = build_test_event("ops", ChannelTarget(channel="slack", address="https://x.test"), "en")
    body = renderer.render(event).body
    assert "TEST MESSAGE" in body
    assert "configuration check" in body
    assert "UTC+0300" in body, "renders through the same template a real alert uses"


@respx.mock
async def test_every_configured_channel_is_exercised(
    renderer: MessageRenderer, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeSmtp.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post("https://hooks.test/slack").mock(return_value=httpx.Response(200))
    respx.post("https://discord.test/hook").mock(return_value=httpx.Response(204))

    settings = make_settings(
        notify_routes_json=routes(
            {"channel": "email", "address": "ops@example.test"},
            {"channel": "telegram", "address": "-100123"},
            {"channel": "slack", "address": "https://hooks.test/slack"},
            {"channel": "discord", "address": "https://discord.test/hook"},
        ),
        telegram_bot_token=TOKEN,
    )
    async with httpx.AsyncClient() as client:
        notifiers = build_notifiers(settings, renderer, client)
        results = await send_test_messages(settings, notifiers, renderer)

    assert {r.channel for r in results} == {"email", "telegram", "slack", "discord"}
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]


@respx.mock
async def test_one_broken_channel_does_not_hide_the_working_ones(
    renderer: MessageRenderer,
) -> None:
    respx.post("https://hooks.test/slack").mock(return_value=httpx.Response(200))
    respx.post("https://discord.test/hook").mock(return_value=httpx.Response(403))

    settings = make_settings(
        notify_routes_json=routes(
            {"channel": "slack", "address": "https://hooks.test/slack"},
            {"channel": "discord", "address": "https://discord.test/hook"},
        )
    )
    async with httpx.AsyncClient() as client:
        notifiers = build_notifiers(settings, renderer, client)
        results = await send_test_messages(settings, notifiers, renderer)

    by_channel = {r.channel: r for r in results}
    assert by_channel["slack"].ok is True
    assert by_channel["discord"].ok is False


async def test_an_unimplemented_channel_reports_rather_than_crashing(
    renderer: MessageRenderer,
) -> None:
    """sms raises by design; the self-test must surface that as a result, not an exception."""
    settings = make_settings(
        notify_routes_json=routes({"channel": "sms", "address": "+251900000000"})
    )
    async with httpx.AsyncClient() as client:
        notifiers = build_notifiers(settings, renderer, client)
        results = await send_test_messages(settings, notifiers, renderer)
    assert results[0].ok is False
    assert "provider has not been chosen" in (results[0].error or "")


async def test_every_recipient_is_covered(renderer: MessageRenderer) -> None:
    table = {
        "routes": [
            {"recipient": "a", "channels": [{"channel": "slack", "address": "https://x.test/a"}]},
            {"recipient": "b", "channels": [{"channel": "slack", "address": "https://x.test/b"}]},
        ]
    }
    settings = make_settings(notify_routes_json=json.dumps(table))
    async with httpx.AsyncClient() as client:
        notifiers = build_notifiers(settings, renderer, client)
        with respx.mock:
            respx.post("https://x.test/a").mock(return_value=httpx.Response(200))
            respx.post("https://x.test/b").mock(return_value=httpx.Response(200))
            results = await send_test_messages(settings, notifiers, renderer)
    assert {r.recipient for r in results} == {"a", "b"}


# --- chat discovery ----------------------------------------------------------------


@respx.mock
async def test_chat_discovery_lists_ids() -> None:
    from tele_scraper.notify.acks import discover_chats

    respx.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {"chat": {"id": -100999, "type": "group", "title": "Ops"}},
                    },
                    {
                        "update_id": 2,
                        "message": {"chat": {"id": 555, "type": "private", "username": "bereket"}},
                    },
                    {
                        "update_id": 3,
                        "message": {"chat": {"id": -100999, "type": "group", "title": "Ops"}},
                    },
                ],
            },
        )
    )
    settings = make_settings(telegram_bot_token=TOKEN)
    async with httpx.AsyncClient() as client:
        chats = await discover_chats(settings, client)

    assert {c["chat_id"] for c in chats} == {"-100999", "555"}, "duplicates collapse"
    assert {c["title"] for c in chats} == {"Ops", "bereket"}


@respx.mock
async def test_chat_discovery_reports_a_bad_token() -> None:
    from tele_scraper.notify.acks import discover_chats

    respx.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": False, "description": "Unauthorized"})
    )
    settings = make_settings(telegram_bot_token=TOKEN)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="Unauthorized"):
            await discover_chats(settings, client)


@respx.mock
async def test_no_chats_is_not_an_error() -> None:
    from tele_scraper.notify.acks import discover_chats

    respx.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )
    settings = make_settings(telegram_bot_token=TOKEN)
    async with httpx.AsyncClient() as client:
        assert await discover_chats(settings, client) == []

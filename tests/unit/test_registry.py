"""Channel registry: only what is routed gets built, and misconfiguration fails at startup."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tele_scraper.errors import ChannelNotConfiguredError
from tele_scraper.notify.registry import build_notifiers, close_notifiers
from tele_scraper.observability import logging as logging_module
from tests.conftest import make_settings


def routes(*channels: dict[str, str]) -> str:
    return json.dumps({"routes": [{"recipient": "ops", "channels": list(channels)}]})


@pytest.fixture
async def client():  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient() as c:
        yield c


async def test_only_routed_channels_are_built(client: Any, renderer: Any) -> None:
    settings = make_settings(
        notify_routes_json=routes({"channel": "slack", "address": "https://hooks.test/x"})
    )
    notifiers = build_notifiers(settings, renderer, client)
    assert set(notifiers) == {"slack"}
    await close_notifiers(notifiers)


async def test_every_channel_can_be_built(client: Any, renderer: Any) -> None:
    settings = make_settings(
        notify_routes_json=routes(
            {"channel": "slack", "address": "https://hooks.test/s"},
            {"channel": "discord", "address": "https://discord.test/d"},
            {"channel": "email", "address": "ops@example.test"},
            {"channel": "telegram", "address": "123"},
            {"channel": "sms", "address": "+251900000000"},
        ),
        telegram_bot_token="123456:AA-token-value",
    )
    notifiers = build_notifiers(settings, renderer, client)
    assert set(notifiers) == {"slack", "discord", "email", "telegram", "sms"}
    await close_notifiers(notifiers)


async def test_email_without_smtp_host_fails_at_startup(client: Any, renderer: Any) -> None:
    settings = make_settings(
        notify_routes_json=routes({"channel": "email", "address": "ops@example.test"}),
        smtp_host="",
    )
    with pytest.raises(ChannelNotConfiguredError, match="SMTP_HOST"):
        build_notifiers(settings, renderer, client)


async def test_telegram_without_token_fails_at_startup(client: Any, renderer: Any) -> None:
    settings = make_settings(notify_routes_json=routes({"channel": "telegram", "address": "1"}))
    with pytest.raises(ChannelNotConfiguredError, match="TELEGRAM_BOT_TOKEN"):
        build_notifiers(settings, renderer, client)


async def test_webhook_urls_are_registered_for_redaction(client: Any, renderer: Any) -> None:
    """A webhook URL is a credential; it must never appear in a log line (§8.6)."""
    url = "https://hooks.example.test/services/super/secret/path"
    settings = make_settings(notify_routes_json=routes({"channel": "slack", "address": url}))
    notifiers = build_notifiers(settings, renderer, client)

    scrubbed = logging_module.redact_processor(None, "info", {"event": f"posting to {url}"})
    assert url not in str(scrubbed["event"])
    await close_notifiers(notifiers)


async def test_telegram_token_is_registered_for_redaction(client: Any, renderer: Any) -> None:
    token = "987654:ZZ-another-fake-token"
    settings = make_settings(
        notify_routes_json=routes({"channel": "telegram", "address": "1"}),
        telegram_bot_token=token,
    )
    notifiers = build_notifiers(settings, renderer, client)
    scrubbed = logging_module.redact_processor(None, "info", {"event": f"bot{token}/sendMessage"})
    assert token not in str(scrubbed["event"])
    await close_notifiers(notifiers)


async def test_close_notifiers_survives_a_failing_close(renderer: Any) -> None:
    class Broken:
        name = "broken"

        async def send(self, event: Any) -> Any:  # pragma: no cover - not exercised
            raise AssertionError

        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    await close_notifiers({"broken": Broken()})  # type: ignore[dict-item]

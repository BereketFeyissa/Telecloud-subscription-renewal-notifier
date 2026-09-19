"""Builds the channel registry from settings.

Adding a channel means adding a builder here and a template - never editing the router
(CLAUDE.md §8.1).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from tele_scraper.config import Settings
from tele_scraper.errors import ChannelNotConfiguredError
from tele_scraper.models import (
    ChannelTarget,
    Component,
    DeliveryResult,
    Evaluation,
    NotificationEvent,
    Status,
)
from tele_scraper.notify.base import MessageRenderer, Notifier
from tele_scraper.notify.discord import DiscordNotifier
from tele_scraper.notify.email import EmailNotifier
from tele_scraper.notify.slack import SlackNotifier
from tele_scraper.notify.sms import SmsNotifier
from tele_scraper.notify.telegram import TelegramNotifier
from tele_scraper.observability.logging import get_logger, register_secret

log = get_logger(__name__)


def build_notifiers(
    settings: Settings, renderer: MessageRenderer, client: httpx.AsyncClient
) -> dict[str, Notifier]:
    """Build a notifier for every channel the routing table actually references.

    Channels that are not routed anywhere are not built, so an unused channel never needs
    credentials.

    Raises:
        ChannelNotConfiguredError: if a routed channel is missing the settings it needs. This
            is a startup failure: discovering it mid-run would mean a dropped alert.
    """
    required = settings.required_channels()
    notifiers: dict[str, Notifier] = {}

    if "email" in required:
        required_smtp = (
            ("SMTP_HOST", settings.smtp_host),
            ("SMTP_FROM", settings.smtp_from),
        )
        missing = [name for name, value in required_smtp if not value]
        if missing:
            raise ChannelNotConfiguredError(
                f"routing table uses 'email' but {', '.join(missing)} is unset"
            )
        register_secret(settings.smtp_password.get_secret_value())
        notifiers["email"] = EmailNotifier(
            renderer,
            host=settings.smtp_host,
            port=settings.smtp_port,
            sender=settings.smtp_from,
            username=settings.smtp_username,
            password=settings.smtp_password.get_secret_value(),
            tls=settings.smtp_tls,
            timeout=settings.notify_timeout_seconds,
        )

    if "telegram" in required:
        token = settings.telegram_bot_token.get_secret_value()
        if not token:
            raise ChannelNotConfiguredError(
                "routing table uses 'telegram' but TELEGRAM_BOT_TOKEN is unset"
            )
        register_secret(token)
        notifiers["telegram"] = TelegramNotifier(
            client,
            renderer,
            bot_token=token,
            api_base=settings.telegram_api_base,
            timeout=settings.notify_timeout_seconds,
        )

    if "slack" in required:
        notifiers["slack"] = SlackNotifier(
            client, renderer, timeout=settings.notify_timeout_seconds
        )

    if "discord" in required:
        notifiers["discord"] = DiscordNotifier(
            client, renderer, timeout=settings.notify_timeout_seconds
        )

    if "sms" in required:
        # Fails closed on use rather than at startup, so the other channels still work while
        # the provider decision is outstanding (CLAUDE.md §2 open 3).
        log.warning(
            "sms.provider_not_selected",
            detail="routes reference 'sms'; deliveries on that channel will fail until a "
            "provider is chosen",
        )
        notifiers["sms"] = SmsNotifier()

    # Webhook URLs are per-recipient addresses, so they are registered for redaction as the
    # routing table is resolved rather than here.
    for route in settings.routing.routes:
        for target in route.targets():
            if target.channel in {"slack", "discord"}:
                register_secret(target.address)

    log.info("notifiers.built", channels=sorted(notifiers))
    return notifiers


def build_test_event(recipient: str, target: ChannelTarget, locale: str) -> NotificationEvent:
    """A synthetic alert used to verify channel credentials.

    Rendered through the real templates so a self-test exercises the same path a genuine alert
    takes. The component is named unmistakably so nobody mistakes it for a live incident.
    """
    now = datetime.now(UTC)
    component = Component(
        component_id="configuration-test",
        name="TEST MESSAGE - tele-scraper configuration check",
        portal_status="TEST",
        expires_at=now + timedelta(days=2),
    )
    evaluation = Evaluation(
        component=component,
        status=Status.EXPIRING_SOON,
        evaluated_at=now,
        rung=timedelta(days=3),
        remaining=timedelta(days=2),
        reason="this is a configuration test, not a real alert",
    )
    return NotificationEvent(
        evaluations=(evaluation,), recipient=recipient, target=target, locale=locale
    )


async def send_test_messages(
    settings: Settings, notifiers: dict[str, Notifier], renderer: MessageRenderer
) -> list[DeliveryResult]:
    """Send one test message per recipient per channel, bypassing dedup and quiet hours.

    Deliberately does not go through the router: the point is to prove credentials work, not to
    exercise suppression. Every delivery is attempted even if an earlier one fails, so one bad
    webhook does not hide a working mailbox (CLAUDE.md §8.3).
    """
    results: list[DeliveryResult] = []
    for route in settings.routing.routes:
        for target in route.targets():
            event = build_test_event(
                route.recipient, target, route.locale or settings.default_locale
            )
            notifier = notifiers.get(target.channel)
            if notifier is None:
                results.append(
                    DeliveryResult.failure(event, f"channel {target.channel!r} is not configured")
                )
                continue
            try:
                results.append(await notifier.send(event))
            except Exception as exc:  # noqa: BLE001 - one channel must not hide the others
                results.append(DeliveryResult.failure(event, f"{type(exc).__name__}: {exc}"))
    return results


async def close_notifiers(notifiers: dict[str, Notifier]) -> None:
    """Release every notifier's resources, reporting rather than hiding failures."""
    for name, notifier in notifiers.items():
        try:
            await notifier.aclose()
        except Exception as exc:  # noqa: BLE001 - shutdown must not mask the original error
            log.warning("notifier.close_failed", channel=name, error=str(exc))

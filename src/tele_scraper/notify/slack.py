"""Slack incoming-webhook channel.

Address is the webhook URL, which is itself a secret and therefore expected to arrive through
``address_env`` backed by a Kubernetes Secret rather than sitting in a ConfigMap
(CLAUDE.md §8.6, §14.6).
"""

from __future__ import annotations

import httpx

from tele_scraper.models import DeliveryResult, NotificationEvent
from tele_scraper.notify.base import MessageRenderer, post_json, truncate
from tele_scraper.observability.logging import get_logger

log = get_logger(__name__)

MAX_BODY = 3000


class SlackNotifier:
    """Posts a message to a Slack incoming webhook."""

    name = "slack"

    def __init__(
        self, client: httpx.AsyncClient, renderer: MessageRenderer, *, timeout: float = 15.0
    ) -> None:
        self._client = client
        self._renderer = renderer
        self._timeout = timeout

    async def send(self, event: NotificationEvent) -> DeliveryResult:
        message = self._renderer.render(event)
        payload: dict[str, object] = {
            "text": f"*{message.subject}*\n{truncate(message.body, MAX_BODY)}"
        }
        try:
            response = await post_json(
                self._client, event.target.address, payload, timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001 - normalized to a result
            log.warning("slack.send_failed", recipient=event.recipient, error=str(exc))
            return DeliveryResult.failure(event, f"slack transport error: {exc}")

        if response.status_code == 429:
            retry_after = float(response.headers.get("retry-after", 60))
            return DeliveryResult.failure(event, "slack rate limited", retry_after=retry_after)
        if response.status_code >= 400:
            return DeliveryResult.failure(
                event, f"slack rejected the message: HTTP {response.status_code}"
            )
        return DeliveryResult.success(event)

    async def aclose(self) -> None:
        return None

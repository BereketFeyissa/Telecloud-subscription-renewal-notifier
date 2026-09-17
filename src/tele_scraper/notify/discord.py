"""Discord incoming-webhook channel.

Address is the webhook URL, a secret; see :mod:`tele_scraper.notify.slack` for why it should
arrive via ``address_env``.
"""

from __future__ import annotations

import httpx

from tele_scraper.models import DeliveryResult, NotificationEvent
from tele_scraper.notify.base import MessageRenderer, post_json, truncate
from tele_scraper.observability.logging import get_logger

log = get_logger(__name__)

#: Discord hard-rejects a ``content`` longer than 2000 characters.
MAX_BODY = 2000


class DiscordNotifier:
    """Posts a message to a Discord webhook."""

    name = "discord"

    def __init__(
        self, client: httpx.AsyncClient, renderer: MessageRenderer, *, timeout: float = 15.0
    ) -> None:
        self._client = client
        self._renderer = renderer
        self._timeout = timeout

    async def send(self, event: NotificationEvent) -> DeliveryResult:
        message = self._renderer.render(event)
        content = truncate(f"**{message.subject}**\n{message.body}", MAX_BODY)
        payload: dict[str, object] = {"content": content, "allowed_mentions": {"parse": []}}
        try:
            response = await post_json(
                self._client, event.target.address, payload, timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001 - normalized to a result
            log.warning("discord.send_failed", recipient=event.recipient, error=str(exc))
            return DeliveryResult.failure(event, f"discord transport error: {exc}")

        if response.status_code == 429:
            retry_after = float(response.headers.get("retry-after", 60))
            return DeliveryResult.failure(event, "discord rate limited", retry_after=retry_after)
        if response.status_code >= 400:
            return DeliveryResult.failure(
                event, f"discord rejected the message: HTTP {response.status_code}"
            )
        return DeliveryResult.success(event)

    async def aclose(self) -> None:
        return None

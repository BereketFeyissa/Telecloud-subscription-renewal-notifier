"""Telegram bot channel.

Address is the recipient's ``chat_id``. The bot token is a secret and lives in the URL, so it
is registered for redaction and never logged (CLAUDE.md §8.6).
"""

from __future__ import annotations

import httpx

from tele_scraper.models import DeliveryResult, NotificationEvent
from tele_scraper.notify.base import MessageRenderer, post_json, truncate
from tele_scraper.observability.logging import get_logger

log = get_logger(__name__)

#: Telegram rejects messages longer than this.
MAX_BODY = 4096

#: Prefix for the Confirm button's callback payload. Telegram caps callback_data at 64 bytes;
#: "ack:" plus a UUID is 40, so a component id fits comfortably.
ACK_PREFIX = "ack:"
MAX_CALLBACK_DATA = 64


def ack_callback_data(ack_key: str) -> str | None:
    """Callback payload for an acknowledgement, or None if it would exceed Telegram's cap.

    Returning None rather than truncating matters: a clipped key would acknowledge the wrong
    situation, which is worse than offering no button.
    """
    payload = f"{ACK_PREFIX}{ack_key}"
    return payload if len(payload.encode()) <= MAX_CALLBACK_DATA else None


class TelegramNotifier:
    """Sends via the Telegram Bot API ``sendMessage`` method."""

    name = "telegram"

    def __init__(
        self,
        client: httpx.AsyncClient,
        renderer: MessageRenderer,
        *,
        bot_token: str,
        api_base: str = "https://api.telegram.org",
        timeout: float = 15.0,
        offer_ack: bool = True,
    ) -> None:
        self._client = client
        self._renderer = renderer
        self._token = bot_token
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        self._offer_ack = offer_ack

    @property
    def _url(self) -> str:
        return f"{self._api_base}/bot{self._token}/sendMessage"

    async def send(self, event: NotificationEvent) -> DeliveryResult:
        message = self._renderer.render(event)
        payload: dict[str, object] = {
            "chat_id": event.target.address,
            "text": truncate(message.body, MAX_BODY),
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if self._offer_ack:
            callback = ack_callback_data(event.evaluation.ack_key)
            if callback is not None:
                payload["reply_markup"] = {
                    "inline_keyboard": [
                        [
                            {
                                "text": "\u2705 Confirm \u2014 stop repeating",
                                "callback_data": callback,
                            }
                        ]
                    ]
                }
            else:
                log.warning(
                    "telegram.ack_button_omitted",
                    component_id=event.evaluation.component.component_id,
                    detail="ack key exceeds Telegram's 64-byte callback_data limit",
                )
        try:
            response = await post_json(self._client, self._url, payload, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - normalized to a result
            log.warning("telegram.send_failed", recipient=event.recipient, error=str(exc))
            return DeliveryResult.failure(event, f"telegram transport error: {exc}")

        if response.status_code == 429:
            retry_after = float(response.headers.get("retry-after", 60))
            log.warning("telegram.rate_limited", recipient=event.recipient, retry_after=retry_after)
            return DeliveryResult.failure(event, "telegram rate limited", retry_after=retry_after)
        if response.status_code >= 400:
            return DeliveryResult.failure(
                event, f"telegram rejected the message: HTTP {response.status_code}"
            )
        return DeliveryResult.success(event)

    async def aclose(self) -> None:
        return None

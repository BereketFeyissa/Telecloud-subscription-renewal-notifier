"""Acknowledgement listener.

Receivers confirm an alert by pressing a button under the Telegram message. Telegram is polled
with ``getUpdates`` long polling, so this is **outbound only**: no webhook, no Ingress, no public
endpoint on a pod that has none (CLAUDE.md §14).

An acknowledgement is recorded against ``(component_id, status, rung)`` and clears the alert for
every recipient, including those on slack and discord who have no way to confirm themselves.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import httpx

from tele_scraper.config import Settings
from tele_scraper.notify.telegram import ACK_PREFIX
from tele_scraper.observability import metrics
from tele_scraper.observability.logging import get_logger
from tele_scraper.state.store import StateStore

log = get_logger(__name__)

#: Where the poll offset is kept, so a restart does not replay old button presses.
OFFSET_KEY = "__telegram_offset__"


#: Digest ack keys look like ``__digest__|EXPIRED`` and have two parts, not three.
DIGEST_PREFIX = "__digest__"


def parse_ack_key(callback_data: str) -> str | None:
    """Extract the acknowledged situation from a callback payload.

    Returns None for anything that is not one of our Confirm buttons. Callback data is attacker
    -influenced in the sense that anyone in the chat can send it, so it is validated, never
    trusted (CLAUDE.md §3.8).
    """
    if not callback_data.startswith(ACK_PREFIX):
        return None
    ack_key = callback_data[len(ACK_PREFIX) :]
    parts = ack_key.split("|")
    if ack_key.startswith(DIGEST_PREFIX):
        return ack_key if len(parts) == 2 and parts[1] else None
    if len(parts) != 3 or not all(parts[:2]):
        return None
    return ack_key


async def discover_chats(settings: Settings, client: httpx.AsyncClient) -> list[dict[str, str]]:
    """List chats that have recently messaged the bot, so their ids can go in the routing table.

    Telegram will not tell a bot which chats exist; a chat id only becomes visible once someone
    messages the bot (or adds it to a group). This reads those pending updates.
    """
    token = settings.telegram_bot_token.get_secret_value()
    api = f"{settings.telegram_api_base.rstrip('/')}/bot{token}"
    response = await client.get(
        f"{api}/getUpdates", params={"timeout": 0}, timeout=settings.notify_timeout_seconds
    )
    payload = response.json()
    if not payload.get("ok"):
        raise ValueError(f"getUpdates failed: {payload.get('description')!r}")

    seen: dict[str, dict[str, str]] = {}
    for update in payload.get("result") or []:
        for key in ("message", "channel_post", "my_chat_member", "callback_query"):
            node = update.get(key)
            if not isinstance(node, dict):
                continue
            chat = node.get("chat") or (node.get("message") or {}).get("chat")
            if not isinstance(chat, dict):
                continue
            chat_id = str(chat.get("id", ""))
            if chat_id:
                seen[chat_id] = {
                    "chat_id": chat_id,
                    "type": str(chat.get("type", "")),
                    "title": str(
                        chat.get("title") or chat.get("username") or chat.get("first_name") or ""
                    ),
                }
    return list(seen.values())


class TelegramAckListener:
    """Polls Telegram for Confirm presses and records them."""

    def __init__(self, settings: Settings, store: StateStore, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._store = store
        self._client = client
        self._token = settings.telegram_bot_token.get_secret_value()
        self._api = f"{settings.telegram_api_base.rstrip('/')}/bot{self._token}"
        self._offset = 0
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def _answer(self, callback_id: str, text: str) -> None:
        """Acknowledge the button press so Telegram stops showing a spinner."""
        with contextlib.suppress(httpx.HTTPError):
            await self._client.post(
                f"{self._api}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
                timeout=self._settings.notify_timeout_seconds,
            )

    async def _record_digest(self, ack_key: str, acked_by: str, fingerprint: str) -> bool:
        """Confirm a digest, expanding to its members when the route asked for that.

        Membership was recorded when the digest was sent, because Telegram's 64-byte
        callback_data cannot carry a list of components.
        """
        members = await self._store.find_digest_members(ack_key)
        ttl = self._settings.ack_ttl_days * 86400
        now = time.time()

        if not members:
            # No membership recorded means the route acknowledges the set as a unit.
            await self._store.acknowledge(
                ack_key,
                component_id=ack_key,
                status=ack_key.split("|")[-1],
                rung="-",
                fingerprint=fingerprint,
                acked_by=acked_by,
                ttl_seconds=ttl,
                now=now,
            )
            metrics.ACKNOWLEDGEMENTS.labels(channel="telegram").inc()
            log.info("ack.recorded", ack_key=ack_key, acked_by=acked_by, members=0)
            return True

        for member_key, member_fp in members:
            component_id, status, rung = member_key.split("|")
            await self._store.acknowledge(
                member_key,
                component_id=component_id,
                status=status,
                rung=rung,
                fingerprint=member_fp,
                acked_by=acked_by,
                ttl_seconds=ttl,
                now=now,
            )
        metrics.ACKNOWLEDGEMENTS.labels(channel="telegram").inc()
        log.info(
            "ack.recorded",
            ack_key=ack_key,
            acked_by=acked_by,
            members=len(members),
            channel="telegram",
        )
        return True

    async def _record(self, ack_key: str, acked_by: str) -> bool:
        fingerprint = await self._store.find_fingerprint(ack_key)
        if fingerprint is None:
            log.warning("ack.unknown_situation", ack_key=ack_key)
            return False
        if ack_key.startswith(DIGEST_PREFIX):
            return await self._record_digest(ack_key, acked_by, fingerprint)
        component_id, status, rung = ack_key.split("|")
        # The fingerprint is unknown here - the button carries only the situation - so the ack
        # is stored against the current one by re-deriving it on the next run. Storing the key
        # with a wildcard fingerprint would let an ack survive a data change, which §8.2a
        # forbids, so instead we record the fingerprint the alert was sent with.
        await self._store.acknowledge(
            ack_key,
            component_id=component_id,
            status=status,
            rung=rung,
            fingerprint=fingerprint,
            acked_by=acked_by,
            ttl_seconds=self._settings.ack_ttl_days * 86400,
            now=time.time(),
        )
        metrics.ACKNOWLEDGEMENTS.labels(channel="telegram").inc()
        log.info("ack.recorded", ack_key=ack_key, acked_by=acked_by, channel="telegram")
        return True

    async def _handle(self, update: dict[str, object]) -> None:
        query = update.get("callback_query")
        if not isinstance(query, dict):
            return
        data = query.get("data")
        callback_id = query.get("id")
        if not isinstance(data, str) or not isinstance(callback_id, str):
            return

        ack_key = parse_ack_key(data)
        if ack_key is None:
            await self._answer(callback_id, "Unrecognised button.")
            return

        sender = query.get("from")
        acked_by = "unknown"
        if isinstance(sender, dict):
            acked_by = str(sender.get("username") or sender.get("id") or "unknown")

        if await self._record(ack_key, acked_by):
            await self._answer(callback_id, "Confirmed — this alert will stop repeating.")
        else:
            await self._answer(callback_id, "Could not confirm; it may have already changed.")

    async def poll_once(self) -> int:
        """Fetch and handle one batch of updates. Returns how many were handled."""
        response = await self._client.get(
            f"{self._api}/getUpdates",
            params={
                "offset": self._offset,
                "timeout": self._settings.telegram_poll_timeout_seconds,
                "allowed_updates": '["callback_query"]',
            },
            timeout=self._settings.telegram_poll_timeout_seconds + 10,
        )
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"getUpdates returned {response.status_code}",
                request=response.request,
                response=response,
            )
        payload = response.json()
        if not payload.get("ok"):
            raise ValueError(f"getUpdates reported failure: {payload.get('description')!r}")

        updates = payload.get("result") or []
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                # Confirming the offset tells Telegram not to resend this update.
                self._offset = max(self._offset, update_id + 1)
            await self._handle(update)
        return len(updates)

    async def run_forever(self) -> None:
        """Poll until asked to stop. One bad poll never kills the listener."""
        log.info("ack_listener.started", channel="telegram")
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self.poll_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the listener must outlive a bad poll
                log.warning("ack_listener.poll_failed", error=str(exc), retry_in=backoff)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 60.0)
        log.info("ack_listener.stopped", channel="telegram")

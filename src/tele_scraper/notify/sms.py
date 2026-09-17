"""SMS channel - BLOCKED, awaiting a provider decision.

SMS is a confirmed requirement (CLAUDE.md §2 decision 3), but the provider is an open question
(§2 open 3): Twilio, Africa's Talking, and a local telecom gateway have different credentials,
endpoints, sender-id rules, and delivery-receipt semantics. §0 forbids guessing any of them, so
this module raises loudly rather than shipping an invented integration.

To implement, we need from the operator:
  1. Provider name and API base URL.
  2. Credential shape (account SID + auth token, API key, username + password, ...).
  3. Sender ID / short code, and whether it must be pre-registered.
  4. Whether delivery receipts must be polled or arrive via callback.
  5. Per-message cost and any rate cap, so §8.7 backoff can be set sensibly.
"""

from __future__ import annotations

from tele_scraper.errors import ProviderNotSelectedError
from tele_scraper.models import DeliveryResult, NotificationEvent

PROVIDER_QUESTIONS = (
    "provider name and API base URL",
    "credential shape",
    "sender ID / short code and registration requirement",
    "delivery receipts: polled or callback",
    "per-message cost and rate cap",
)


class SmsNotifier:
    """Placeholder that refuses to run.

    Deliberately fails closed. A route may name ``sms`` today, but a run that tries to use it
    stops with a clear message instead of silently dropping an alert someone is relying on.
    """

    name = "sms"

    async def send(self, event: NotificationEvent) -> DeliveryResult:
        raise ProviderNotSelectedError(
            "SMS provider has not been chosen; cannot deliver to "
            f"{event.recipient}. Outstanding decisions: " + "; ".join(PROVIDER_QUESTIONS)
        )

    async def aclose(self) -> None:
        return None

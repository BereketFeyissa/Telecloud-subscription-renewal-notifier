"""Notifier protocol and message rendering.

Every channel implements the same protocol, so adding one never means editing the router
(CLAUDE.md §8.1). Bodies come from Jinja2 templates under ``templates/<locale>/`` - message
wording is content, not code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import httpx
from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateNotFound,
    select_autoescape,
)
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from tele_scraper.models import DeliveryResult, NotificationEvent

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "templates"


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """A notification rendered for one channel."""

    subject: str
    body: str


@runtime_checkable
class Notifier(Protocol):
    """One delivery medium."""

    #: Channel name as it appears in the routing table.
    name: str

    async def send(self, event: NotificationEvent) -> DeliveryResult:
        """Deliver one notification. Must not raise; failures come back as a result."""
        ...

    async def aclose(self) -> None:
        """Release any held resources."""
        ...


def humanize(delta: timedelta) -> str:
    """Render a duration the way an operator reads it: ``3 days, 4 hours``.

    Past durations are rendered as ``4 hours ago`` so an expired component is unambiguous.
    """
    seconds = int(abs(delta).total_seconds())
    past = delta.total_seconds() < 0
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    parts: list[str] = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes and not days:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if not parts:
        parts.append("less than a minute")

    text = ", ".join(parts)
    return f"{text} ago" if past else text


class MessageRenderer:
    """Renders notification subjects and bodies from templates.

    Template lookup is ``<locale>/<channel>.<part>.j2`` falling back to
    ``<locale>/default.<part>.j2`` and then to the default locale. A missing template for a
    configured locale is a configuration error, surfaced at render time rather than silently
    producing an empty message.
    """

    def __init__(self, timezone: ZoneInfo, default_locale: str = "en", root: Path | None = None):
        self._tz = timezone
        self._default_locale = default_locale
        self._env = Environment(
            loader=FileSystemLoader(root or TEMPLATE_ROOT),
            autoescape=select_autoescape(default=False, default_for_string=False),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._env.filters["humanize"] = humanize

    def _pick(self, locale: str, channel: str, part: str) -> str:
        candidates = (
            f"{locale}/{channel}.{part}.j2",
            f"{locale}/default.{part}.j2",
            f"{self._default_locale}/{channel}.{part}.j2",
            f"{self._default_locale}/default.{part}.j2",
        )
        for name in candidates:
            try:
                self._env.get_template(name)
            except TemplateNotFound:
                continue
            return name
        raise TemplateNotFound(
            f"no template for channel={channel!r} part={part!r} locale={locale!r}"
        )

    def context(self, event: NotificationEvent) -> dict[str, object]:
        """Template context.

        ``expires_local`` carries the offset because §8.5 forbids showing a human a bare
        timestamp with no timezone attached.
        """
        evaluation = event.evaluation
        component = evaluation.component
        expires_local: datetime | None = (
            component.expires_at.astimezone(self._tz) if component.expires_at else None
        )
        return {
            "recipient": event.recipient,
            "channel": event.target.channel,
            "component": component,
            "component_id": component.component_id,
            "component_name": component.label,
            "status": evaluation.status.value,
            "reason": evaluation.reason,
            "is_critical": evaluation.is_critical,
            "expires_at_utc": component.expires_at,
            "expires_local": expires_local,
            "expires_display": (
                expires_local.strftime("%Y-%m-%d %H:%M %Z (UTC%z)") if expires_local else "unknown"
            ),
            "expires_derived": component.expires_at_derived,
            "remaining": evaluation.remaining,
            "remaining_display": (
                humanize(evaluation.remaining) if evaluation.remaining is not None else "unknown"
            ),
            "rung_display": humanize(evaluation.rung) if evaluation.rung else "",
            "portal_status": component.portal_status or "unknown",
            "is_credential": component.kind == "credential",
            "evaluated_at": evaluation.evaluated_at,
        }

    def render(self, event: NotificationEvent) -> RenderedMessage:
        """Render subject and body for one event."""
        ctx = self.context(event)
        locale = event.locale or self._default_locale
        channel = event.target.channel
        subject = self._env.get_template(self._pick(locale, channel, "subject")).render(**ctx)
        body = self._env.get_template(self._pick(locale, channel, "body")).render(**ctx)
        return RenderedMessage(subject=subject.strip(), body=body.strip())


class _TransientError(Exception):
    """Internal marker for a retryable upstream response."""


def truncate(text: str, limit: int, suffix: str = "\n[truncated]") -> str:
    """Trim a body to a channel's hard limit without losing the truncation signal."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, object],
    *,
    timeout: float,  # noqa: ASYNC109 - forwarded to httpx, not an asyncio cancel scope
    max_attempts: int = 3,
) -> httpx.Response:
    """POST JSON with bounded backoff on transient faults.

    Retries timeouts, connection errors and 5xx. Does **not** retry 429 - a rate limit is
    respected by backing off until the next cycle, not by hammering (CLAUDE.md §8.7) - and does
    not retry other 4xx, which are our bug, not the server's.
    """
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=1, max=10),
        retry=retry_if_exception_type(
            (httpx.TimeoutException, httpx.TransportError, _TransientError)
        ),
        reraise=True,
    ):
        with attempt:
            response = await client.post(url, json=payload, timeout=timeout)
            if response.status_code >= 500:
                raise _TransientError(f"upstream {response.status_code}")
            return response
    raise AssertionError("unreachable: AsyncRetrying always returns or raises")

"""Routing, dedup, and dispatch.

Recipients and channels are data, not code (CLAUDE.md §8): this module reads the routing table
and never contains a name, address, or channel choice of its own.

Suppression order matters and is deliberate:

1. **Acknowledged** - a human has confirmed they have seen this exact situation. Holds until
   the component's data changes or the ack ages out (``ACK_TTL_DAYS``), never indefinitely.
   An unacknowledged alert repeats every run, which is the pressure that makes confirmation
   mean something.
2. **Quiet hours** - non-critical only, and deliberately *not* recorded, so the alert goes out
   once the window closes rather than being lost.
3. **Notify disabled / dry run** - the intended send is logged and metered, never delivered.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from tele_scraper.config import Route, Settings
from tele_scraper.domain.rules import in_quiet_hours, matches_component
from tele_scraper.errors import StateStoreError
from tele_scraper.models import (
    NOTIFIABLE_STATUSES,
    DeliveryResult,
    Evaluation,
    NotificationEvent,
    Status,
)
from tele_scraper.notify.base import MessageRenderer, Notifier
from tele_scraper.observability import metrics
from tele_scraper.observability.logging import get_logger
from tele_scraper.state.store import StateStore

log = get_logger(__name__)


def _suppressed(event: NotificationEvent, reason: str) -> DeliveryResult:
    metrics.NOTIFICATIONS_SUPPRESSED.labels(channel=event.target.channel, reason=reason).inc()
    return DeliveryResult(
        channel=event.target.channel,
        recipient=event.recipient,
        ok=True,
        suppressed=True,
        error=reason,
    )


class Router:
    """Turns evaluations into deliveries."""

    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        notifiers: dict[str, Notifier],
        renderer: MessageRenderer,
        *,
        concurrency: int = 4,
    ) -> None:
        self._settings = settings
        self._store = store
        self._notifiers = notifiers
        self._renderer = renderer
        self._semaphore = asyncio.Semaphore(concurrency)

    def _matching(self, route: Route, evaluations: list[Evaluation]) -> list[Evaluation]:
        """Evaluations this route cares about, in input order."""
        return [
            e
            for e in evaluations
            if e.status in NOTIFIABLE_STATUSES
            and e.status in route.statuses
            and matches_component(route.components, e.component.component_id, e.component.name)
        ]

    def plan(self, evaluations: list[Evaluation]) -> list[NotificationEvent]:
        """Expand evaluations into the full set of intended deliveries.

        A ``detailed`` route produces one message per component; a ``summary`` route produces
        one per status group. Grouping by status rather than lumping everything into a single
        message keeps criticality intact: EXPIRED ignores quiet hours and EXPIRING_SOON does
        not, and one mixed message could not honour both.

        A status with nothing in it produces no message at all - silence when nothing is wrong.

        Pure with respect to I/O, so it can be asserted on directly in tests.
        """
        events: list[NotificationEvent] = []
        for route in self._settings.routing.routes:
            matched = self._matching(route, evaluations)
            if not matched:
                continue
            locale = route.locale or self._settings.default_locale

            groups: list[tuple[Evaluation, ...]]
            if route.mode == "detailed":
                groups = [(e,) for e in matched]
            else:
                by_status: dict[Status, list[Evaluation]] = {}
                for e in matched:
                    by_status.setdefault(e.status, []).append(e)
                groups = [tuple(v) for v in by_status.values()]

            for group in groups:
                for target in route.targets():
                    events.append(
                        NotificationEvent(
                            evaluations=group,
                            recipient=route.recipient,
                            target=target,
                            locale=locale,
                            digest=route.mode == "summary",
                            ack_mode=route.summary_ack,
                        )
                    )
        return events

    def _route_for(self, recipient: str) -> Route | None:
        for route in self._settings.routing.routes:
            if route.recipient == recipient:
                return route
        return None

    @staticmethod
    def _scope(event: NotificationEvent) -> str:
        """Dedup scope: one row per recipient, channel, address and component (or digest)."""
        subject = event.digest_key if event.digest else event.evaluation.component.component_id
        return "|".join((event.recipient, event.target.channel, event.target.address, subject))

    async def dispatch(
        self, evaluations: list[Evaluation], *, now: datetime
    ) -> list[DeliveryResult]:
        """Deliver every intended notification.

        A failure on one delivery never aborts the others (CLAUDE.md §8.3); results are
        collected and returned so the caller can set the run's exit status.
        """
        events = self.plan(evaluations)
        if not events:
            log.info("router.nothing_to_send", evaluations=len(evaluations))
            return []

        results = await asyncio.gather(
            *(self._deliver(event, now=now) for event in events), return_exceptions=True
        )

        collected: list[DeliveryResult] = []
        for event, outcome in zip(events, results, strict=True):
            if isinstance(outcome, StateStoreError):
                # Dedup is load-bearing; losing it means we cannot tell a repeat from a new
                # alert. Surface it rather than guessing (CLAUDE.md §8.2).
                raise outcome
            if isinstance(outcome, BaseException):
                log.error(
                    "router.delivery_crashed",
                    recipient=event.recipient,
                    channel=event.target.channel,
                    error=str(outcome),
                )
                metrics.NOTIFICATIONS_FAILED.labels(
                    channel=event.target.channel, reason=type(outcome).__name__
                ).inc()
                collected.append(DeliveryResult.failure(event, str(outcome)))
            else:
                collected.append(outcome)
        return collected

    async def _apply_acknowledgements(self, event: NotificationEvent) -> NotificationEvent | None:
        """Drop what has already been confirmed; None when nothing is left to say.

        For a detailed message this is all-or-nothing. For a digest it depends on the route's
        ``summary_ack``:

        * ``components`` - confirmed items are filtered out, so the digest **shrinks** as
          people work through it rather than re-listing what they have already handled.
        * ``digest`` - the set is acknowledged as a unit; changing the set lapses the ack and
          the whole group is sent again.
        * ``none`` - nothing is ever suppressed; the digest repeats every run by design.
        """
        now = time.time()
        if event.digest and event.ack_mode == "none":
            return event

        if event.digest and event.ack_mode == "digest":
            if await self._store.is_acknowledged(
                event.digest_key, event.digest_fingerprint, now=now
            ):
                return None
            return event

        surviving = [
            e
            for e in event.evaluations
            if not await self._store.is_acknowledged(e.ack_key, e.component.fingerprint, now=now)
        ]
        if not surviving:
            return None
        if len(surviving) == len(event.evaluations):
            return event
        return event.model_copy(update={"evaluations": tuple(surviving)})

    async def _deliver(self, event: NotificationEvent, *, now: datetime) -> DeliveryResult:
        original = event
        component_id = event.evaluation.component.component_id
        scope = self._scope(event)

        if self._settings.require_acknowledgement:
            # Repeat every run until a human confirms. Acknowledgement, not elapsed time, is
            # what stops an alert (CLAUDE.md §8.2a).
            remaining = await self._apply_acknowledgements(event)
            if remaining is None:
                return _suppressed(original, "acknowledged")
            event = remaining
        elif await self._store.already_sent(scope, event.dedup_key):
            return _suppressed(event, "dedup")

        route = self._route_for(event.recipient)
        quiet = route.quiet_hours if route is not None else None
        if (
            quiet is not None
            and not event.is_critical
            and in_quiet_hours(now, quiet.start, quiet.end, quiet.tz)
        ):
            log.info(
                "router.quiet_hours",
                recipient=event.recipient,
                channel=event.target.channel,
                component_id=component_id,
            )
            return _suppressed(event, "quiet_hours")

        # Dry run is checked BEFORE notify_enabled: both are non-sending paths, and the point
        # of a dry run is to preview intended notifications without first having to switch
        # sending on. The reverse order made --dry-run silent by default.
        if self._settings.dry_run:
            message = self._renderer.render(event)
            log.info(
                "router.dry_run",
                recipient=event.recipient,
                channel=event.target.channel,
                component_id=component_id,
                status=event.evaluation.status.value,
                subject=message.subject,
            )
            return _suppressed(event, "dry_run")

        if not self._settings.notify_enabled:
            return _suppressed(event, "notify_disabled")

        notifier = self._notifiers.get(event.target.channel)
        if notifier is None:
            metrics.NOTIFICATIONS_FAILED.labels(
                channel=event.target.channel, reason="not_configured"
            ).inc()
            return DeliveryResult.failure(
                event, f"channel {event.target.channel!r} is not configured"
            )

        async with self._semaphore:
            try:
                result = await notifier.send(event)
            except Exception as exc:  # noqa: BLE001 - one channel must not sink the run
                log.error(
                    "router.notifier_raised",
                    recipient=event.recipient,
                    channel=event.target.channel,
                    component_id=component_id,
                    error=str(exc),
                )
                metrics.NOTIFICATIONS_FAILED.labels(
                    channel=event.target.channel, reason=type(exc).__name__
                ).inc()
                return DeliveryResult.failure(event, str(exc))

        if result.ok:
            metrics.NOTIFICATIONS_SENT.labels(
                channel=event.target.channel, status=event.evaluation.status.value
            ).inc()
            await self._store.record_sent(
                scope,
                event.dedup_key,
                recipient=event.recipient,
                channel=event.target.channel,
                component_id=component_id,
                status=event.status.value,
                rung=event.evaluation.rung_key,
                ack_key=event.ack_key,
                fingerprint=event.ack_fingerprint,
            )
            if event.digest and event.ack_mode == "components":
                # The Confirm button can only carry the digest's identity, so remember what it
                # listed in order to expand a press into per-component acknowledgements.
                await self._store.record_digest_members(
                    event.digest_key, event.members(), now=time.time()
                )
            log.info(
                "router.sent",
                recipient=event.recipient,
                channel=event.target.channel,
                component_id=component_id,
                status=event.evaluation.status.value,
            )
        else:
            metrics.NOTIFICATIONS_FAILED.labels(
                channel=event.target.channel, reason="rejected"
            ).inc()
            log.warning(
                "router.send_failed",
                recipient=event.recipient,
                channel=event.target.channel,
                component_id=component_id,
                error=result.error,
            )
        return result

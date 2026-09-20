"""Status derivation - the core rule of this system (CLAUDE.md §6).

Pure module: no network, no clock reads, no environment access. ``now`` is always injected.
Requires 100% branch coverage.

The governing principle is that absence of evidence is never evidence of health. A component
whose expiry cannot be established is ``UNKNOWN``, which is an operator alert - never ``ACTIVE``
and never silently skipped.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tele_scraper.domain.rules import select_rung
from tele_scraper.models import Component, Evaluation, Status


def normalize_token(value: str) -> str:
    """Fold a portal status string for comparison against the configured vocabulary."""
    return " ".join(value.strip().lower().split())


def resolve_expiry(component: Component) -> Component:
    """Fill in ``expires_at`` from ``activated_at + validity_period`` when it is absent.

    The result is flagged ``expires_at_derived`` so callers can log and meter it
    (CLAUDE.md §6). Components that already carry an expiry are returned unchanged.
    """
    if component.expires_at is not None:
        return component
    if component.activated_at is None or component.validity_period is None:
        return component
    return component.model_copy(
        update={
            "expires_at": component.activated_at + component.validity_period,
            "expires_at_derived": True,
        }
    )


def derive_status(
    component: Component,
    *,
    now: datetime,
    thresholds: tuple[timedelta, ...],
    suspended_tokens: frozenset[str] = frozenset(),
) -> Evaluation:
    """Apply the §6 ladder to one component. First match wins; the order is not negotiable.

    1. Portal explicitly reports a terminating state -> ``SUSPENDED``.
    2. Expiry missing, unparseable, or not timezone-aware -> ``UNKNOWN``.
    3. ``expires_at <= now`` -> ``EXPIRED``.
    4. Inside the warning ladder -> ``EXPIRING_SOON`` with the tightest rung crossed.
    5. Otherwise -> ``ACTIVE``.

    Args:
        component: The scraped component.
        now: Timezone-aware current instant, injected by the caller.
        thresholds: Warning ladder, widest first. Must be non-empty.
        suspended_tokens: Portal strings that mean suspended/blocked/inactive. Empty by
            default because the portal's vocabulary is an open question (CLAUDE.md §2 open 2);
            while empty, no component is ever classified ``SUSPENDED``.

    Note on derived expiry: when ``expires_at`` was computed rather than scraped, the result is
    used normally but flagged, logged, and metered. It may raise severity; it must never
    quietly stand in for data we do not have. Callers are expected to surface the flag.

    Raises:
        ValueError: if ``now`` is naive. That is a programming error, not portal data.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("derive_status requires a timezone-aware 'now'")
    if not thresholds:
        raise ValueError("derive_status requires a non-empty warning ladder")

    resolved = resolve_expiry(component)

    def result(
        status: Status,
        reason: str,
        remaining: timedelta | None = None,
        rung: timedelta | None = None,
    ) -> Evaluation:
        return Evaluation(
            component=resolved,
            status=status,
            evaluated_at=now,
            rung=rung,
            remaining=remaining,
            reason=reason,
        )

    # 1. The portal says it is off.
    if (
        resolved.portal_status is not None
        and suspended_tokens
        and normalize_token(resolved.portal_status) in suspended_tokens
    ):
        return result(Status.SUSPENDED, f"portal reported {resolved.portal_status!r}")

    # 2. We could not establish an expiry. Loud, never assumed healthy.
    if resolved.parse_error is not None:
        # No prefix: parse_error already says what went wrong, and calling an unreachable
        # portal a "parse error" misdescribes it.
        return result(Status.UNKNOWN, resolved.parse_error)
    if resolved.expires_at is None:
        return result(Status.UNKNOWN, "expires_at missing and not derivable")
    if resolved.expires_at.tzinfo is None or resolved.expires_at.utcoffset() is None:
        return result(Status.UNKNOWN, "expires_at is not timezone-aware")

    remaining = resolved.expires_at - now

    # 3. Already over.
    if remaining <= timedelta(0):
        return result(Status.EXPIRED, "expiry has passed", remaining=remaining)

    # 4. Inside the ladder.
    rung = select_rung(remaining, thresholds)
    if rung is not None:
        return result(Status.EXPIRING_SOON, "inside warning window", remaining=remaining, rung=rung)

    # 5. Healthy.
    return result(Status.ACTIVE, "valid", remaining=remaining)


def evaluate_all(
    components: list[Component],
    *,
    now: datetime,
    thresholds: tuple[timedelta, ...],
    suspended_tokens: frozenset[str] = frozenset(),
) -> list[Evaluation]:
    """Evaluate every component. Order is preserved.

    An empty input is *not* handled here: a scrape returning zero components is a run-level
    ``UNKNOWN`` and is the runner's responsibility (CLAUDE.md §6).
    """
    return [
        derive_status(c, now=now, thresholds=thresholds, suspended_tokens=suspended_tokens)
        for c in components
    ]

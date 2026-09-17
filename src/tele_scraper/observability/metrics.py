"""Prometheus metrics.

The pod is long-lived, so metrics are scraped from ``/metrics`` rather than pushed
(CLAUDE.md §12, §14.1).

``scrape_last_success_timestamp_seconds`` is the most important series here: it is what a
stale-run alert is written against, and a silently wedged scheduler is this system's top
failure mode.
"""

from __future__ import annotations

import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

RUN_DURATION = Histogram(
    "scrape_run_duration_seconds",
    "Wall-clock duration of one full scrape/evaluate/notify cycle.",
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 900),
    registry=REGISTRY,
)

RUNS_TOTAL = Counter(
    "scrape_runs_total",
    "Completed cycles by outcome.",
    labelnames=("result",),
    registry=REGISTRY,
)

COMPONENTS = Gauge(
    "scrape_components",
    "Components observed in the most recent successful cycle, by derived status.",
    labelnames=("status",),
    registry=REGISTRY,
)

PARSE_FAILURES = Counter(
    "scrape_parse_failures_total",
    "Components whose markup could not be parsed into a usable record.",
    registry=REGISTRY,
)

COMPONENTS_MISSING = Gauge(
    "scrape_components_missing",
    "Components the portal listed previously but no longer returns. Non-zero means either a "
    "deletion nobody confirmed, or an incomplete listing.",
    registry=REGISTRY,
)

DERIVED_EXPIRY = Gauge(
    "scrape_components_derived_expiry",
    "Components whose expiry was computed from activation + validity rather than scraped.",
    registry=REGISTRY,
)

NOTIFICATIONS_SENT = Counter(
    "notifications_sent_total",
    "Notifications delivered successfully.",
    labelnames=("channel", "status"),
    registry=REGISTRY,
)

NOTIFICATIONS_FAILED = Counter(
    "notifications_failed_total",
    "Notifications that could not be delivered.",
    labelnames=("channel", "reason"),
    registry=REGISTRY,
)

NOTIFICATIONS_SUPPRESSED = Counter(
    "notifications_suppressed_total",
    "Notifications intentionally not sent (dedup, quiet hours, dry run, notify disabled).",
    labelnames=("channel", "reason"),
    registry=REGISTRY,
)

ACKNOWLEDGEMENTS = Counter(
    "acknowledgements_total",
    "Alerts confirmed by a recipient, by the channel they confirmed on.",
    labelnames=("channel",),
    registry=REGISTRY,
)

LAST_SUCCESS = Gauge(
    "scrape_last_success_timestamp_seconds",
    "Unix timestamp of the last cycle that completed without a scrape failure. "
    "Alert on staleness of this series.",
    registry=REGISTRY,
)

SCHEDULER_UP = Gauge(
    "scheduler_up",
    "1 while the scheduler loop is running and not wedged.",
    registry=REGISTRY,
)


def mark_success() -> None:
    """Record that a cycle completed without a scrape failure."""
    LAST_SUCCESS.set(time.time())

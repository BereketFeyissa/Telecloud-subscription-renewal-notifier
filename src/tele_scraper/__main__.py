"""CLI entrypoint. Thin by contract (CLAUDE.md §5).

Wires the object graph together and hands control to the scheduler. All behaviour lives in the
modules it composes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from pathlib import Path

import httpx

from tele_scraper.config import Settings, load_settings
from tele_scraper.errors import ConfigError, StateStoreError, TeleScraperError
from tele_scraper.health import HealthServer, HealthState
from tele_scraper.notify.acks import TelegramAckListener, discover_chats
from tele_scraper.notify.base import MessageRenderer
from tele_scraper.notify.registry import (
    build_notifiers,
    close_notifiers,
    send_test_messages,
)
from tele_scraper.notify.router import Router
from tele_scraper.observability import metrics
from tele_scraper.observability.logging import configure_logging, get_logger, register_secret
from tele_scraper.runner import Runner
from tele_scraper.scheduler import Scheduler
from tele_scraper.scraper.client import HttpPortalClient
from tele_scraper.state.store import SqliteStateStore, build_store

EXIT_CONFIG_ERROR = 4


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tele-scraper",
        description="Scrape telecloud component validity and notify recipients.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single cycle and exit with that cycle's exit code",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scrape and evaluate, log intended notifications, send nothing",
    )
    parser.add_argument(
        "--capture",
        nargs="?",
        const="portal-capture",
        metavar="PATH",
        help="fetch one page using PORTAL_SESSION_COOKIE / PORTAL_AUTH_HEADER, save the raw "
        "response, and report what the portal serves (no parsing, no notifications)",
    )
    parser.add_argument(
        "--url",
        metavar="PATH",
        help="path to fetch with --capture (default: PORTAL_COMPONENTS_PATH)",
    )
    parser.add_argument(
        "--ack",
        metavar="COMPONENT_ID",
        help="confirm every outstanding alert for a component, so it stops repeating. The "
        "universal fallback for recipients on channels that cannot confirm (slack, discord)",
    )
    parser.add_argument(
        "--by",
        metavar="NAME",
        default="cli",
        help="who is confirming, recorded for audit (default: cli)",
    )
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="send one test message to every configured recipient and channel, to verify "
        "credentials. Requires NOTIFY_ENABLED=true",
    )
    parser.add_argument(
        "--telegram-chats",
        action="store_true",
        help="list chat ids that have messaged the bot, for filling in the routing table",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration and the routing table, then exit",
    )
    return parser.parse_args(argv)


def _register_secrets(settings: Settings) -> None:
    """Register credentials for log redaction before anything can log them."""
    register_secret(settings.portal_password.get_secret_value())
    register_secret(settings.portal_session_cookie.get_secret_value())
    register_secret(settings.portal_auth_header.get_secret_value())
    register_secret(settings.smtp_password.get_secret_value())
    register_secret(settings.telegram_bot_token.get_secret_value())


async def _acknowledge(settings: Settings, component_id: str, acked_by: str) -> int:
    """Confirm every outstanding alert for one component.

    Acks are recorded against the exact situations that were notified, so confirming does not
    swallow a state the recipient never saw.
    """
    log = get_logger(__name__)
    store = build_store(settings.state_backend, settings.state_dsn)
    if isinstance(store, SqliteStateStore):
        await store.open()
    try:
        outstanding = await store.find_acknowledgeable(component_id)
        if not outstanding:
            log.warning(
                "ack.nothing_outstanding",
                component_id=component_id,
                detail="no notification has been sent for this component; nothing to confirm",
            )
            return 1
        for row in outstanding:
            await store.acknowledge(
                row["ack_key"],
                component_id=component_id,
                status=row["status"],
                rung=row["rung"],
                fingerprint=row["fingerprint"],
                acked_by=acked_by,
                ttl_seconds=settings.ack_ttl_days * 86400,
                now=time.time(),
            )
            metrics.ACKNOWLEDGEMENTS.labels(channel="cli").inc()
            log.info("ack.recorded", ack_key=row["ack_key"], acked_by=acked_by, channel="cli")
        log.info(
            "ack.complete",
            component_id=component_id,
            confirmed=len(outstanding),
            holds_for_days=settings.ack_ttl_days,
        )
        return 0
    finally:
        await store.close()


async def _test_notify(settings: Settings) -> int:
    """Verify channel credentials by sending one clearly-marked test message to each."""
    log = get_logger(__name__)
    if not settings.notify_enabled:
        log.error(
            "test_notify.refused",
            detail="this really sends messages, so it requires NOTIFY_ENABLED=true "
            "(CLAUDE.md §3.5)",
        )
        return EXIT_CONFIG_ERROR

    client = httpx.AsyncClient(timeout=settings.notify_timeout_seconds)
    renderer = MessageRenderer(settings.timezone, settings.default_locale)
    notifiers = build_notifiers(settings, renderer, client)
    try:
        results = await send_test_messages(settings, notifiers, renderer)
    finally:
        await close_notifiers(notifiers)
        await client.aclose()

    for result in results:
        if result.ok:
            log.info("test_notify.ok", channel=result.channel, recipient=result.recipient)
        else:
            log.error(
                "test_notify.failed",
                channel=result.channel,
                recipient=result.recipient,
                error=result.error,
            )
    failed = [r for r in results if not r.ok]
    log.info("test_notify.summary", attempted=len(results), failed=len(failed))
    return 3 if failed else 0


async def _telegram_chats(settings: Settings) -> int:
    """Print chat ids that have messaged the bot, for filling in the routing table."""
    log = get_logger(__name__)
    if not settings.telegram_bot_token.get_secret_value():
        log.error("telegram_chats.no_token", detail="set TELEGRAM_BOT_TOKEN first")
        return EXIT_CONFIG_ERROR

    async with httpx.AsyncClient(timeout=settings.notify_timeout_seconds) as client:
        try:
            chats = await discover_chats(settings, client)
        except (httpx.HTTPError, ValueError) as exc:
            log.error("telegram_chats.failed", error=str(exc))
            return 1

    if not chats:
        log.warning(
            "telegram_chats.none",
            detail="no chats found. Message the bot (or add it to your group) and run this "
            "again - Telegram only reveals a chat id after someone contacts the bot",
        )
        return 1
    for chat in chats:
        log.info("telegram_chats.found", **chat)
    return 0


async def _capture(settings: Settings, destination: str, path: str | None) -> int:
    """Fetch one page and write it out, so the parser can be written against real markup.

    Deliberately does not touch the notification stack or the state store: this is a
    read-only diagnostic.
    """
    log = get_logger(__name__)
    portal = HttpPortalClient(settings)
    try:
        body, content_type, verdict = await portal.capture(path)
    except TeleScraperError as exc:
        log.error("capture.failed", error=str(exc))
        return 1
    finally:
        await portal.aclose()

    suffix = {"json": ".json", "html": ".html", "login_page": ".html"}.get(verdict, ".txt")
    target = Path(destination)
    if not target.suffix:
        target = target.with_suffix(suffix)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    except OSError as exc:
        log.error("capture.write_failed", path=str(target), error=str(exc))
        return 1

    log.info(
        "capture.saved",
        path=str(target),
        bytes=len(body),
        content_type=content_type,
        verdict=verdict,
    )
    if verdict != "login_page":
        # The captured file holds live account data. §3.2 forbids committing it unredacted, and
        # the obvious destination (tests/fixtures/) is a tracked directory.
        log.warning(
            "capture.contains_account_data",
            path=str(target),
            detail="this file contains REAL account data. Redact identifiers before "
            "committing it or sharing it (CLAUDE.md §3.1, §3.2).",
        )
    if verdict == "login_page":
        log.warning(
            "capture.looks_like_login",
            detail="the response looks like the login page, so the session was not accepted. "
            "Copy a fresh Cookie header from the browser and try again.",
        )
    elif verdict == "json":
        log.info(
            "capture.next_step",
            detail="the portal serves JSON - no HTML parsing needed. Redact account "
            "identifiers in the saved file and share it.",
        )
    elif verdict == "html":
        log.info(
            "capture.next_step",
            detail="the portal serves HTML. Redact account identifiers in the saved file, "
            "save it as tests/fixtures/components.html, and share it. If the component table "
            "is missing from the file, it is rendered client-side and needs a browser engine.",
        )
    else:
        log.warning(
            "capture.unrecognized",
            detail="could not tell what this is; share the saved file and the content_type.",
        )
    return 0


async def _run(settings: Settings, *, once: bool) -> int:
    log = get_logger(__name__)
    health = HealthState(
        stale_after_seconds=settings.run_interval_seconds + settings.run_timeout_seconds + 120
    )
    server = HealthServer(settings.metrics_host, settings.metrics_port, health)
    server.start()

    store = build_store(settings.state_backend, settings.state_dsn)
    if isinstance(store, SqliteStateStore):
        await store.open()

    client = httpx.AsyncClient(timeout=settings.notify_timeout_seconds)
    renderer = MessageRenderer(settings.timezone, settings.default_locale)
    notifiers = build_notifiers(settings, renderer, client)
    portal = HttpPortalClient(settings)
    router = Router(settings, store, notifiers, renderer)
    runner = Runner(settings, portal, store, router)

    health.set_ready(True)
    log.info(
        "startup.complete",
        once=once,
        dry_run=settings.dry_run,
        notify_enabled=settings.notify_enabled,
        interval_seconds=settings.run_interval_seconds,
        channels=sorted(notifiers),
        recipients=[r.recipient for r in settings.routing.routes],
    )

    listener: TelegramAckListener | None = None
    listener_task: asyncio.Task[None] | None = None
    if (
        not once
        and settings.telegram_ack_enabled
        and settings.telegram_bot_token.get_secret_value()
        and "telegram" in settings.required_channels()
    ):
        # Long polling: the bot calls out to Telegram, so no ingress and no public endpoint
        # (CLAUDE.md §14). Runs alongside the scheduler, not inside it, so a slow scrape never
        # delays a Confirm press.
        listener = TelegramAckListener(settings, store, client)
        listener_task = asyncio.create_task(listener.run_forever(), name="ack-listener")

    try:
        if once:
            report = await runner.run_once()
            return report.exit_code
        scheduler = Scheduler(settings, runner, health)
        return await scheduler.run_forever()
    finally:
        if listener is not None and listener_task is not None:
            listener.request_stop()
            listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener_task
        health.set_ready(False)
        await close_notifiers(notifiers)
        await portal.aclose()
        await client.aclose()
        await store.close()
        server.stop()


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code; never raises for expected failures."""
    args = _parse_args(argv)

    try:
        # --capture is read-only: it never builds the notification stack, so it must not
        # require a routing table to run.
        settings = load_settings(require_notifications=not args.capture)
    except ConfigError as exc:
        configure_logging("INFO", "json")
        get_logger(__name__).error("config.invalid", error=str(exc))
        return EXIT_CONFIG_ERROR

    if args.dry_run:
        settings = settings.model_copy(update={"dry_run": True})

    configure_logging(settings.log_level, settings.log_format)
    _register_secrets(settings)
    log = get_logger(__name__)

    if args.check_config:
        log.info(
            "config.ok",
            recipients=[r.recipient for r in settings.routing.routes],
            channels=sorted(settings.required_channels()),
            thresholds=[str(t) for t in settings.thresholds],
            suspended_vocabulary=sorted(settings.suspended_tokens) or "NONE CONFIGURED",
        )
        if not settings.suspended_tokens:
            log.warning(
                "config.no_suspended_tokens",
                detail="PORTAL_SUSPENDED_TOKENS is empty, so no component will ever be "
                "classified SUSPENDED (CLAUDE.md §2 open 2)",
            )
        return 0

    if args.telegram_chats:
        return asyncio.run(_telegram_chats(settings))

    if args.test_notify:
        return asyncio.run(_test_notify(settings))

    if args.ack:
        return asyncio.run(_acknowledge(settings, args.ack, args.by))

    if args.capture:
        return asyncio.run(_capture(settings, args.capture, args.url))

    try:
        return asyncio.run(_run(settings, once=args.once))
    except StateStoreError as exc:
        log.error("state.unavailable", error=str(exc))
        return EXIT_CONFIG_ERROR
    except TeleScraperError as exc:
        log.error("fatal", error=str(exc))
        return 1
    except KeyboardInterrupt:
        log.info("interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())

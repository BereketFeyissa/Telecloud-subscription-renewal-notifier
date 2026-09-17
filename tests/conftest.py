"""Shared test fixtures.

Unit tests never touch the network (CLAUDE.md §11.2): HTTP is intercepted with respx and the
portal is replaced by a fixture client.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import structlog

from tele_scraper.config import Settings
from tele_scraper.models import Component
from tele_scraper.notify.base import MessageRenderer
from tele_scraper.observability.logging import reset_secrets
from tele_scraper.state.store import MemoryStateStore

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)

DEFAULT_ROUTES: dict[str, Any] = {
    "routes": [
        {
            "recipient": "ops-team",
            "channels": [
                {"channel": "slack", "address": "https://hooks.example.test/slack/ops"},
                {"channel": "email", "address": "ops@example.test"},
            ],
            "statuses": ["EXPIRED", "EXPIRING_SOON", "UNKNOWN", "SUSPENDED"],
            "components": ["*"],
        }
    ]
}


#: Env prefixes that would otherwise leak a developer's own .env into test results.
LEAKY_PREFIXES = (
    "PORTAL_",
    "NOTIFY_",
    "SMTP_",
    "TELEGRAM_",
    "STATE_",
    "SCRAPE_",
    "APP_",
    "WARN_",
    "RUN_",
    "LOG_",
    "METRICS_",
    "DRY_RUN",
    "DEFAULT_LOCALE",
)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip portal/notify settings from the environment for every test.

    Without this the suite reads whatever is in the developer's shell, so a local .env could
    decide whether a test passes - which is exactly how a real isolation bug got through here.
    """
    for key in list(os.environ):
        if key.startswith(LEAKY_PREFIXES):
            monkeypatch.delenv(key, raising=False)


def make_settings(**overrides: Any) -> Settings:
    """Build Settings from explicit values so tests never depend on the ambient environment.

    ``_env_file=None`` disables the .env lookup: a developer's local .env must never reach a
    test, or results stop being reproducible between machines.
    """
    base: dict[str, Any] = {
        "portal_base_url": "https://portal.example.test",
        "portal_username": "svc-monitor",
        "portal_password": "hunter2-hunter2",
        "notify_routes_json": json.dumps(DEFAULT_ROUTES),
        "notify_enabled": True,
        "smtp_host": "smtp.example.test",
        "smtp_from": "alerts@example.test",
        "scrape_delay_seconds": 0.0,
        "state_dsn": "/tmp/tele-scraper-test.sqlite3",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def renderer() -> MessageRenderer:
    return MessageRenderer(ZoneInfo("Africa/Addis_Ababa"), "en")


@pytest.fixture
def store() -> MemoryStateStore:
    return MemoryStateStore()


@pytest.fixture(autouse=True)
def _reset_logging() -> Any:
    """Undo any logging configuration a test triggered.

    configure_logging() binds structlog to the current sys.stdout and caches the bound logger.
    Under capsys that stream is replaced per test, so without a reset the first test to call
    main() leaves every later test writing to a closed buffer.
    """
    yield
    structlog.reset_defaults()


@pytest.fixture(autouse=True)
def _clear_secrets() -> Any:
    reset_secrets()
    yield
    reset_secrets()


def make_component(
    component_id: str = "comp-1",
    name: str = "Compute Bundle",
    *,
    expires_in: timedelta | None = None,
    expires_at: datetime | None = None,
    activated_at: datetime | None = None,
    validity_period: timedelta | None = None,
    portal_status: str | None = None,
    parse_error: str | None = None,
) -> Component:
    """Build a component relative to the frozen NOW used across tests."""
    if expires_at is None and expires_in is not None:
        expires_at = NOW + expires_in
    return Component(
        component_id=component_id,
        name=name,
        portal_status=portal_status,
        activated_at=activated_at,
        validity_period=validity_period,
        expires_at=expires_at,
        parse_error=parse_error,
    )

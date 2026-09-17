"""Liveness, readiness and metrics endpoints (CLAUDE.md §12, §14.2)."""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
import pytest

from tele_scraper.health import HealthServer, HealthState


def test_state_starts_not_ready_but_live() -> None:
    state = HealthState(stale_after_seconds=60)
    assert state.is_ready is False
    assert state.is_live is True


def test_readiness_toggles() -> None:
    state = HealthState(stale_after_seconds=60)
    state.set_ready(True)
    assert state.is_ready is True
    state.set_ready(False)
    assert state.is_ready is False


def test_liveness_fails_once_the_heartbeat_goes_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    state = HealthState(stale_after_seconds=10)
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 11)
    assert state.is_live is False
    assert state.seconds_since_beat >= 10


def test_heartbeat_restores_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    state = HealthState(stale_after_seconds=10)
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 11)
    assert state.is_live is False
    state.heartbeat()
    assert state.is_live is True


@pytest.fixture
def server() -> Iterator[tuple[HealthServer, HealthState]]:
    state = HealthState(stale_after_seconds=60)
    srv = HealthServer("127.0.0.1", 0, state)
    srv.start()
    yield srv, state
    srv.stop()


def test_endpoints(server: tuple[HealthServer, HealthState]) -> None:
    srv, state = server
    base = f"http://127.0.0.1:{srv.port}"

    assert httpx.get(f"{base}/healthz", timeout=5).status_code == 200
    assert httpx.get(f"{base}/readyz", timeout=5).status_code == 503

    state.set_ready(True)
    ready = httpx.get(f"{base}/readyz", timeout=5)
    assert ready.status_code == 200
    assert "ready" in ready.text

    metrics = httpx.get(f"{base}/metrics", timeout=5)
    assert metrics.status_code == 200
    assert "scrape_last_success_timestamp_seconds" in metrics.text

    assert httpx.get(f"{base}/nope", timeout=5).status_code == 404


def test_healthz_reports_503_when_wedged(
    server: tuple[HealthServer, HealthState], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged scheduler loop must be visible to Kubernetes, not silently idle."""
    srv, _state = server
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 1000)
    response = httpx.get(f"http://127.0.0.1:{srv.port}/healthz", timeout=5)
    assert response.status_code == 503
    assert "wedged" in response.text

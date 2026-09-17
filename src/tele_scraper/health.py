"""Liveness, readiness, and metrics endpoints.

The pod is long-lived, so Kubernetes needs a way to tell "running" from "wedged"
(CLAUDE.md §12, §14.2). Served from a small stdlib HTTP server on a background thread rather
than a web framework, to avoid a dependency for three endpoints.

A run that produces ``UNKNOWN`` must not flip liveness: that is an alert about the portal, not
a reason to restart the process.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from tele_scraper.observability import metrics
from tele_scraper.observability.logging import get_logger

log = get_logger(__name__)


class HealthState:
    """Shared liveness/readiness state, written by the scheduler and read by the server."""

    def __init__(self, stale_after_seconds: float) -> None:
        self._stale_after = stale_after_seconds
        self._lock = threading.Lock()
        self._last_beat = time.monotonic()
        self._ready = False

    def heartbeat(self) -> None:
        """Called by the scheduler each time it completes a cycle or a sleep tick."""
        with self._lock:
            self._last_beat = time.monotonic()

    def set_ready(self, ready: bool) -> None:
        with self._lock:
            self._ready = ready

    @property
    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    @property
    def is_live(self) -> bool:
        """Live while the scheduler has checked in recently enough."""
        with self._lock:
            return (time.monotonic() - self._last_beat) < self._stale_after

    @property
    def seconds_since_beat(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_beat


def _make_handler(state: HealthState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # BaseHTTPRequestHandler mandates this name
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/metrics":
                self._respond(200, generate_latest(metrics.REGISTRY), CONTENT_TYPE_LATEST)
            elif path == "/healthz":
                live = state.is_live
                body = (
                    f"ok\nseconds_since_heartbeat={state.seconds_since_beat:.1f}\n"
                    if live
                    else f"wedged\nseconds_since_heartbeat={state.seconds_since_beat:.1f}\n"
                )
                self._respond(200 if live else 503, body.encode(), "text/plain; charset=utf-8")
            elif path == "/readyz":
                ready = state.is_ready
                self._respond(
                    200 if ready else 503,
                    b"ready\n" if ready else b"not ready\n",
                    "text/plain; charset=utf-8",
                )
            else:
                self._respond(404, b"not found\n", "text/plain; charset=utf-8")

        def log_message(self, fmt: str, *args: Any) -> None:
            """Silence the default stderr access log; we emit structured logs instead."""
            return

    return Handler


class HealthServer:
    """Runs the health/metrics endpoints on a daemon thread."""

    def __init__(self, host: str, port: int, state: HealthState) -> None:
        self._server = ThreadingHTTPServer((host, port), _make_handler(state))
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="health", daemon=True
        )
        self._host = host
        self._port = port

    @property
    def port(self) -> int:
        """The bound port. Differs from the requested one when port 0 was asked for."""
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread.start()
        log.info("health.listening", host=self._host, port=self._port)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        log.info("health.stopped")

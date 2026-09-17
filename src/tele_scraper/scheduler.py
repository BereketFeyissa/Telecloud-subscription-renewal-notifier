"""Long-running scheduler loop.

The workload is a Deployment, not a CronJob (CLAUDE.md §2 decision 2, §14.1), so the cadence
lives here. Three properties matter:

* **No overlap.** One cycle at a time; a slow run delays the next rather than racing it.
* **A wedged loop is visible.** The loop heartbeats while it works and while it sleeps, so
  ``/healthz`` can tell "running" from "hung" (§12).
* **SIGTERM is graceful.** New cycles stop, the in-flight cycle finishes, state is flushed
  (§14.3).
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import signal

from tele_scraper.config import Settings
from tele_scraper.health import HealthState
from tele_scraper.observability import metrics
from tele_scraper.observability.logging import get_logger
from tele_scraper.runner import Runner

log = get_logger(__name__)

#: Heartbeat cadence while sleeping between cycles.
_TICK_SECONDS = 15.0


class Scheduler:
    """Runs cycles on an interval until asked to stop."""

    def __init__(self, settings: Settings, runner: Runner, health: HealthState) -> None:
        self._settings = settings
        self._runner = runner
        self._health = health
        self._stop = asyncio.Event()

    def request_stop(self, reason: str = "signal") -> None:
        """Ask the loop to finish the current cycle and exit."""
        if not self._stop.is_set():
            log.info("scheduler.stop_requested", reason=reason)
            self._stop.set()

    def install_signal_handlers(self) -> None:
        """Route SIGTERM/SIGINT into a graceful stop.

        Falls back silently on platforms where the loop cannot install handlers; the process
        then relies on the default behaviour rather than failing to start.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.request_stop, sig.name)

    async def _sleep_with_heartbeat(self, seconds: float) -> None:
        """Sleep in ticks, heartbeating as we go, waking early if asked to stop."""
        remaining = seconds
        while remaining > 0 and not self._stop.is_set():
            chunk = min(_TICK_SECONDS, remaining)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=chunk)
            self._health.heartbeat()
            remaining -= chunk

    async def run_forever(self) -> int:
        """Run cycles until stopped. Returns the exit code of the last cycle."""
        self.install_signal_handlers()
        metrics.SCHEDULER_UP.set(1)
        last_exit = 0

        try:
            while not self._stop.is_set():
                self._health.heartbeat()
                try:
                    report = await asyncio.wait_for(
                        self._runner.run_once(), timeout=self._settings.run_timeout_seconds
                    )
                    last_exit = report.exit_code
                except TimeoutError:
                    # Bound the blast radius of a hung portal: abandon this cycle, keep the
                    # loop alive, and let the next one try again.
                    last_exit = 1
                    metrics.RUNS_TOTAL.labels(result="timeout").inc()
                    log.error("scheduler.run_timeout", timeout=self._settings.run_timeout_seconds)
                except Exception as exc:  # the loop must outlive one bad cycle
                    last_exit = 1
                    metrics.RUNS_TOTAL.labels(result="crashed").inc()
                    log.exception("scheduler.run_crashed", error=str(exc))

                self._health.heartbeat()

                with contextlib.suppress(Exception):
                    await self._runner.prune_state()

                if self._stop.is_set():
                    break

                jitter = random.uniform(0, self._settings.run_jitter_seconds)  # noqa: S311
                delay = self._settings.run_interval_seconds + jitter
                log.info("scheduler.sleeping", seconds=round(delay, 1))
                await self._sleep_with_heartbeat(delay)
        finally:
            metrics.SCHEDULER_UP.set(0)
            log.info("scheduler.stopped", last_exit_code=last_exit)

        return last_exit

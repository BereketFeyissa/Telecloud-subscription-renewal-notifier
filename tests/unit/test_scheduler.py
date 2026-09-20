"""Scheduler loop: no overlap, graceful stop, and surviving a bad cycle (CLAUDE.md §14.1-3)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from tele_scraper.health import HealthState
from tele_scraper.models import RunReport
from tele_scraper.scheduler import Scheduler
from tests.conftest import NOW, make_settings


def report(exit_code_source: list[Any] | None = None) -> RunReport:
    return RunReport(run_id="r", started_at=NOW, finished_at=NOW, evaluations=[], results=[])


class FakeRunner:
    """Counts cycles and stops the scheduler after a fixed number."""

    def __init__(self, stop_after: int = 1, behaviour: str = "ok") -> None:
        self.calls = 0
        self.pruned = 0
        self.stop_after = stop_after
        self.behaviour = behaviour
        self.scheduler: Scheduler | None = None

    async def run_once(self, retry_after: timedelta | None = None) -> RunReport:
        self.calls += 1
        if self.calls >= self.stop_after and self.scheduler is not None:
            self.scheduler.request_stop("test")
        if self.behaviour == "crash":
            raise RuntimeError("cycle blew up")
        if self.behaviour == "hang":
            await asyncio.sleep(10)
        return report()

    async def prune_state(self) -> None:
        self.pruned += 1


def build(runner: FakeRunner, **overrides: Any) -> Scheduler:
    settings = make_settings().model_copy(update=overrides)
    scheduler = Scheduler(settings, runner, HealthState(stale_after_seconds=60))  # type: ignore[arg-type]
    runner.scheduler = scheduler
    return scheduler


async def test_runs_a_cycle_then_stops_when_asked() -> None:
    runner = FakeRunner(stop_after=1)
    exit_code = await build(runner).run_forever()
    assert runner.calls == 1
    assert runner.pruned == 1
    assert exit_code == 0


async def test_loop_survives_a_crashing_cycle() -> None:
    runner = FakeRunner(stop_after=1, behaviour="crash")
    exit_code = await build(runner).run_forever()
    assert exit_code == 1, "a crashed cycle is reported, but the loop still shuts down cleanly"


async def test_a_hung_cycle_is_abandoned_not_left_running() -> None:
    runner = FakeRunner(stop_after=1, behaviour="hang")
    exit_code = await build(runner, run_timeout_seconds=0.05).run_forever()
    assert exit_code == 1
    assert runner.calls == 1


async def test_stop_is_idempotent() -> None:
    scheduler = build(FakeRunner())
    scheduler.request_stop("first")
    scheduler.request_stop("second")
    assert scheduler._stop.is_set() is True


async def test_sleep_wakes_early_on_stop() -> None:
    scheduler = build(FakeRunner())
    scheduler.request_stop("test")
    await asyncio.wait_for(scheduler._sleep_with_heartbeat(3600), timeout=2)


async def test_signal_handlers_install_without_error() -> None:
    scheduler = build(FakeRunner())
    scheduler.install_signal_handlers()


# --- retry backoff -----------------------------------------------------------------


def test_backoff_doubles_per_consecutive_failure() -> None:
    scheduler = build(FakeRunner(), retry_backoff_seconds=10, retry_backoff_max_seconds=1000)
    seen = []
    for _ in range(5):
        seen.append(scheduler.next_backoff().total_seconds())
        scheduler._failures += 1
    assert seen == [10, 20, 40, 80, 160]


def test_backoff_is_capped() -> None:
    """A long outage must not turn into an ever-growing blind spot, nor a retry storm."""
    scheduler = build(FakeRunner(), retry_backoff_seconds=60, retry_backoff_max_seconds=900)
    scheduler._failures = 20
    assert scheduler.next_backoff().total_seconds() == 900


async def test_a_failed_run_retries_soon_instead_of_sleeping_the_whole_interval() -> None:
    """The bug this fixes: one lost cycle meant silence until the next scheduled run."""
    runner = FakeRunner(stop_after=1, behaviour="crash")
    scheduler = build(runner, run_interval_seconds=28800, retry_backoff_seconds=30)
    assert scheduler.next_backoff().total_seconds() == 30, (
        "a failed cycle retries in 30s, not after the 8-hour interval"
    )
    await scheduler.run_forever()
    assert scheduler._failures == 1, "the failure is recorded even though the loop then stopped"


async def test_a_successful_run_resets_the_backoff() -> None:
    scheduler = build(FakeRunner(stop_after=1), retry_backoff_seconds=30)
    scheduler._failures = 4
    await scheduler.run_forever()
    assert scheduler._failures == 0


async def test_findings_are_not_failures() -> None:
    """A run that reached the portal succeeded, even if what it found is alarming."""
    from tele_scraper.models import Evaluation, Status
    from tests.conftest import make_component

    class FindingRunner(FakeRunner):
        async def run_once(self, retry_after: timedelta | None = None) -> RunReport:
            self.calls += 1
            if self.scheduler is not None:
                self.scheduler.request_stop("test")
            return RunReport(
                run_id="r",
                started_at=NOW,
                finished_at=NOW,
                evaluations=[
                    Evaluation(component=make_component(), status=Status.UNKNOWN, evaluated_at=NOW)
                ],
                scrape_failed=False,
            )

    runner = FindingRunner(stop_after=1)
    scheduler = build(runner)
    exit_code = await scheduler.run_forever()
    assert exit_code == 2, "UNKNOWN present"
    assert scheduler._failures == 0, "an UNKNOWN finding must not trigger retry backoff"

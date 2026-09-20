"""One full cycle: scrape -> parse -> evaluate -> route -> send.

Owns the run-level rules that §6 places above any individual component, in particular that a
scrape returning zero components is a run-level ``UNKNOWN`` rather than a clean bill of health.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

from tele_scraper.config import Settings
from tele_scraper.domain.status import evaluate_all
from tele_scraper.errors import ParseError, ScrapeError
from tele_scraper.models import Component, Evaluation, RunReport, Status
from tele_scraper.notify.router import Router
from tele_scraper.observability import metrics
from tele_scraper.observability.logging import bind_run, get_logger
from tele_scraper.scraper.client import PortalClient
from tele_scraper.scraper.parser import (
    ACCOUNT_COMPONENT_ID,
    PASSWORD_COMPONENT_ID,
    account_components,
    parse_components,
)
from tele_scraper.state.store import StateStore

log = get_logger(__name__)

#: Sentinel id for the run-level alert raised when a scrape yields nothing.
EMPTY_RESULT_ID = "__run__"

#: Sentinel id for the run-level alert raised when the portal cannot be reached.
UNREACHABLE_ID = "__portal_unreachable__"

#: Prefix given to listing entries that arrive without a usable id. Positional, so it must not
#: be remembered across runs - position is not identity.
UNIDENTIFIED_PREFIX = "unidentified-item-"


#: Ids that are ours, not the portal's. They must never be tracked for disappearance: they
#: exist only when we synthesise them, so their absence says nothing about the portal.
SYNTHETIC_IDS = frozenset(
    {EMPTY_RESULT_ID, UNREACHABLE_ID, PASSWORD_COMPONENT_ID, ACCOUNT_COMPONENT_ID}
)


def _is_trackable(component: Component) -> bool:
    """Whether a component has an identity stable enough to notice its absence."""
    return not (
        component.component_id in SYNTHETIC_IDS
        or component.component_id.startswith(UNIDENTIFIED_PREFIX)
    )


def _unreachable_component(error: str, retry_after: timedelta | None) -> Component:
    """Synthetic record for a run that could not reach the portal at all.

    Without this a total scrape failure produced an exit code, a log line and a metric, and
    told nobody - the system was blind and silent, which is indistinguishable from everything
    being fine. That is the exact failure this project exists to prevent, so it alerts like any
    other UNKNOWN (CLAUDE.md §6).

    The retry is stated because the recipient's first question is whether anyone is still
    trying. ``None`` means no retry is scheduled - a ``--once`` run - and the text says so
    rather than promising one that will not happen.
    """
    if retry_after is None:
        follow_up = "This was a single run, so nothing will retry automatically."
    else:
        minutes = max(1, round(retry_after.total_seconds() / 60))
        follow_up = (
            f"The next attempt is in about {minutes} minute{'s' if minutes != 1 else ''}; "
            "this repeats until the portal answers or someone confirms it."
        )
    return Component(
        component_id=UNREACHABLE_ID,
        name="Portal unreachable",
        kind="run",
        parse_error=f"Could not reach the portal: {error}. {follow_up}",
    )


def _missing_component(component_id: str, name: str) -> Component:
    """Synthetic record for a component the portal has stopped listing.

    Carried as ``UNKNOWN`` rather than quietly dropped: we cannot tell a deliberate deletion
    from a portal glitch, so a human decides. Acknowledging it is what makes it go away.
    """
    return Component(
        component_id=component_id,
        name=name,
        parse_error="no longer listed by the portal; it may have been deleted, or the "
        "listing may be incomplete",
    )


def _empty_result_component(detail: str) -> Component:
    """Synthetic component used to alert on an empty scrape.

    Routing it as an ordinary component means operators hear about it on the channels they
    already configured, instead of it being visible only in metrics nobody is watching.
    """
    return Component(
        component_id=EMPTY_RESULT_ID,
        name="Portal returned no components",
        kind="run",
        parse_error=detail,
    )


class Runner:
    """Executes one cycle and reports what happened."""

    def __init__(
        self,
        settings: Settings,
        client: PortalClient,
        store: StateStore,
        router: Router,
    ) -> None:
        self._settings = settings
        self._client = client
        self._store = store
        self._router = router

    async def run_once(self, retry_after: timedelta | None = None) -> RunReport:
        """Run one cycle. Never raises for ordinary failures; they land in the report.

        Args:
            retry_after: How long until the next attempt if this run fails, so the alert can
                say. None when nothing will retry.
        """
        run_id = uuid.uuid4().hex[:12]
        bind_run(run_id)
        started = datetime.now(UTC)
        clock_start = time.perf_counter()
        log.info(
            "run.started",
            dry_run=self._settings.dry_run,
            notify_enabled=self._settings.notify_enabled,
        )

        components: list[Component] = []
        scrape_failed = False

        try:
            items = await self._client.fetch_components()
            components = parse_components(items)
        except ScrapeError as exc:
            scrape_failed = True
            # Reaching nobody is the most severe outcome there is, so it alerts rather than
            # only setting an exit code.
            components = [_unreachable_component(str(exc), retry_after)]
            log.error(
                "run.scrape_failed",
                error=str(exc),
                retry_after_seconds=(retry_after.total_seconds() if retry_after else None),
            )
        except ParseError as exc:
            # Parse failures are not scrape failures: we reached the portal, we just could not
            # read it. That is UNKNOWN, and UNKNOWN alerts (CLAUDE.md §6, §7.6).
            metrics.PARSE_FAILURES.inc()
            components = [_empty_result_component(f"parser failed: {exc}")]
            log.error("run.parse_failed", error=str(exc))

        if not scrape_failed and not components:
            metrics.PARSE_FAILURES.inc()
            components = [
                _empty_result_component("portal returned zero components; cannot confirm anything")
            ]
            log.error("run.empty_result")

        now = datetime.now(UTC)

        if not scrape_failed and self._settings.detect_missing_components:
            components = await self._add_missing(components)

        evaluations = evaluate_all(
            components,
            now=now,
            thresholds=self._settings.thresholds,
            suspended_tokens=self._settings.suspended_tokens,
        )
        evaluations += self._evaluate_credentials(now)

        derived = sum(1 for e in evaluations if e.component.expires_at_derived)
        if derived:
            log.warning("run.derived_expiry", count=derived)
        metrics.DERIVED_EXPIRY.set(derived)

        counts: dict[Status, int] = dict.fromkeys(Status, 0)
        for evaluation in evaluations:
            counts[evaluation.status] += 1
        for status, count in counts.items():
            metrics.COMPONENTS.labels(status=status.value).set(count)

        results = await self._router.dispatch(evaluations, now=now)

        report = RunReport(
            run_id=run_id,
            started_at=started,
            finished_at=datetime.now(UTC),
            evaluations=evaluations,
            results=results,
            scrape_failed=scrape_failed,
        )

        duration = time.perf_counter() - clock_start
        metrics.RUN_DURATION.observe(duration)
        outcome = "ok" if report.exit_code == 0 else f"exit_{report.exit_code}"
        metrics.RUNS_TOTAL.labels(result=outcome).inc()
        if not scrape_failed:
            metrics.mark_success()

        log.info(
            "run.finished",
            duration_seconds=round(duration, 3),
            exit_code=report.exit_code,
            components=len(evaluations),
            **{f"count_{k.value.lower()}": v for k, v in counts.items()},
            sent=sum(1 for r in results if r.ok and not r.suppressed),
            suppressed=sum(1 for r in results if r.suppressed),
            failed=sum(1 for r in results if not r.ok),
        )
        return report

    def _evaluate_credentials(self, now: datetime) -> list[Evaluation]:
        """Watch our own credentials on their own, wider ladder.

        An expired portal password does not degrade this service, it silences it: every run
        fails to authenticate while the absence of alerts looks like good news. Changing one
        also needs a Secret update and a rollout, so it is warned about earlier than a
        component is (CLAUDE.md §6).
        """
        if not self._settings.monitor_credentials:
            return []
        account = getattr(self._client, "account", None)
        if account is None:
            return []

        components = account_components(
            account, include_account=self._settings.monitor_account_expiry
        )
        if not components:
            log.warning(
                "run.no_credential_dates",
                detail="the login response carried no expiry dates; credential expiry is not "
                "being watched",
            )
            return []
        return evaluate_all(components, now=now, thresholds=self._settings.credential_thresholds)

    async def _add_missing(self, components: list[Component]) -> list[Component]:
        """Append synthetic records for components the portal has stopped listing.

        A component whose disappearance has already been acknowledged is forgotten outright,
        so confirming once ends it rather than merely silencing it each run.
        """
        trackable = [c for c in components if _is_trackable(c)]
        present = {c.component_id for c in trackable}

        known = await self._store.list_known()
        missing: list[Component] = []
        for row in known:
            component_id = row["component_id"]
            if component_id in present:
                continue
            synthetic = _missing_component(component_id, row["name"])
            ack_key = f"{component_id}|{Status.UNKNOWN.value}|-"
            if await self._store.is_acknowledged(ack_key, synthetic.fingerprint, now=time.time()):
                await self._store.forget(component_id)
                log.info("run.missing_forgotten", component_id=component_id)
                continue
            missing.append(synthetic)

        if missing:
            log.error(
                "run.components_missing",
                count=len(missing),
                component_ids=[c.component_id for c in missing],
            )
        metrics.COMPONENTS_MISSING.set(len(missing))

        await self._store.record_seen(
            [(c.component_id, c.label) for c in trackable], now=time.time()
        )
        return components + missing

    async def prune_state(self) -> None:
        """Drop dedup records past the retention window."""
        removed = await self._store.prune(self._settings.state_retention_days * 86400)
        if removed:
            log.info("state.pruned", removed=removed)

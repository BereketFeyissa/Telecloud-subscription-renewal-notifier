"""Domain models.

All datetimes here are timezone-aware UTC. Naive datetimes are rejected at the boundary
(CLAUDE.md §3.10).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class Status(StrEnum):
    """Derived component status. The only value notifications may act on (CLAUDE.md §6)."""

    ACTIVE = "ACTIVE"
    EXPIRING_SOON = "EXPIRING_SOON"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"
    UNKNOWN = "UNKNOWN"


#: Statuses that ignore quiet hours and always page (CLAUDE.md §8.4).
CRITICAL_STATUSES: frozenset[Status] = frozenset({Status.EXPIRED, Status.UNKNOWN})

#: Statuses that are worth notifying about at all. ACTIVE is the quiet, healthy state.
NOTIFIABLE_STATUSES: frozenset[Status] = frozenset(
    {Status.EXPIRED, Status.EXPIRING_SOON, Status.SUSPENDED, Status.UNKNOWN}
)


class Component(BaseModel):
    """One purchased telecloud component as scraped from the portal."""

    model_config = ConfigDict(frozen=True)

    component_id: str = Field(min_length=1)
    name: str = ""
    #: What this record represents. ``credential`` marks our own portal login rather than a
    #: purchased item, so messages can say what its expiry actually costs.
    kind: Literal["component", "credential"] = "component"
    portal_status: str | None = None
    activated_at: AwareDatetime | None = None
    validity_period: timedelta | None = None
    expires_at: AwareDatetime | None = None
    expires_at_derived: bool = False
    parse_error: str | None = None

    @field_validator("component_id", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def label(self) -> str:
        """Human-facing name, falling back to the id so a message is never blank."""
        return self.name or self.component_id

    @property
    def fingerprint(self) -> str:
        """Digest of the facts an acknowledgement was made against.

        An ack means "I have seen this situation". If the situation changes - the expiry moves
        because a renewal landed, or the portal status changes - the ack no longer applies and
        the component notifies again (CLAUDE.md §8.2a).
        """
        material = (
            f"{self.expires_at.isoformat() if self.expires_at else '-'}|{self.portal_status or '-'}"
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]


class AccountInfo(BaseModel):
    """Credential lifetimes, read from the login response.

    The portal returns these on every login, so watching them costs no extra request. They
    matter because an expired credential does not merely degrade this service - it stops the
    service reporting at all, while looking outwardly fine.
    """

    model_config = ConfigDict(frozen=True)

    username: str = ""
    password_expires_at: AwareDatetime | None = None
    account_expires_at: AwareDatetime | None = None


class Evaluation(BaseModel):
    """The result of applying §6 to a single component at a point in time."""

    model_config = ConfigDict(frozen=True)

    component: Component
    status: Status
    evaluated_at: AwareDatetime
    #: Tightest warning rung crossed, e.g. ``timedelta(days=3)``. None outside EXPIRING_SOON.
    rung: timedelta | None = None
    #: Time left until expiry. Negative once expired. None when unknown.
    remaining: timedelta | None = None
    reason: str = ""

    @property
    def is_critical(self) -> bool:
        return self.status in CRITICAL_STATUSES

    @property
    def rung_key(self) -> str:
        """Stable token for the rung, used in the dedup key (CLAUDE.md §8.2)."""
        if self.rung is None:
            return "-"
        return f"{int(self.rung.total_seconds())}s"

    @property
    def ack_key(self) -> str:
        """What an acknowledgement silences: this component in this exact state.

        Deliberately independent of recipient and channel, so one person confirming clears the
        alert for everyone - recipients on channels that cannot acknowledge (slack, discord)
        would otherwise be notified every run forever.

        A new rung or a changed status produces a different key, so escalation re-arms by
        itself and an ack can never swallow a worsening situation.
        """
        return f"{self.component.component_id}|{self.status.value}|{self.rung_key}"


class ChannelTarget(BaseModel):
    """One delivery address on one channel for one recipient."""

    model_config = ConfigDict(frozen=True)

    channel: str
    address: str

    @field_validator("channel")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class NotificationEvent(BaseModel):
    """A single intended delivery: one evaluation, one recipient, one channel."""

    model_config = ConfigDict(frozen=True)

    evaluation: Evaluation
    recipient: str
    target: ChannelTarget
    locale: str = "en"

    @property
    def dedup_key(self) -> str:
        """``(component_id, status, rung)`` scoped per recipient and channel (CLAUDE.md §8.2)."""
        e = self.evaluation
        return "|".join(
            (
                self.recipient,
                self.target.channel,
                self.target.address,
                e.component.component_id,
                e.status.value,
                e.rung_key,
            )
        )


class DeliveryResult(BaseModel):
    """Outcome of one send attempt."""

    model_config = ConfigDict(frozen=True)

    channel: str
    recipient: str
    ok: bool
    error: str | None = None
    retry_after: float | None = None
    suppressed: bool = False

    @classmethod
    def success(cls, event: NotificationEvent) -> DeliveryResult:
        return cls(channel=event.target.channel, recipient=event.recipient, ok=True)

    @classmethod
    def failure(
        cls, event: NotificationEvent, error: str, retry_after: float | None = None
    ) -> DeliveryResult:
        return cls(
            channel=event.target.channel,
            recipient=event.recipient,
            ok=False,
            error=error,
            retry_after=retry_after,
        )


class RunReport(BaseModel):
    """Summary of one full cycle, used for logging, metrics, and exit codes."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    started_at: datetime
    finished_at: datetime
    evaluations: list[Evaluation] = Field(default_factory=list)
    results: list[DeliveryResult] = Field(default_factory=list)
    scrape_failed: bool = False

    @property
    def counts(self) -> dict[Status, int]:
        out: dict[Status, int] = dict.fromkeys(Status, 0)
        for e in self.evaluations:
            out[e.status] += 1
        return out

    @property
    def has_unknown(self) -> bool:
        return any(e.status is Status.UNKNOWN for e in self.evaluations)

    @property
    def delivery_failed(self) -> bool:
        return any(not r.ok and not r.suppressed for r in self.results)

    @property
    def exit_code(self) -> int:
        """Exit codes per CLAUDE.md §12."""
        if self.scrape_failed:
            return 1
        if self.has_unknown:
            return 2
        if self.delivery_failed:
            return 3
        return 0

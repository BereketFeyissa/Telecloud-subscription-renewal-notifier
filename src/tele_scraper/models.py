"""Domain models.

All datetimes here are timezone-aware UTC. Naive datetimes are rejected at the boundary
(CLAUDE.md §3.10).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


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
    """One intended delivery to one recipient on one channel.

    Carries a tuple of evaluations rather than a single one: a detailed message holds exactly
    one, a summary message holds every component in a status group. Constructing with
    ``evaluation=`` still works and wraps it, so the two modes share one delivery path.
    """

    model_config = ConfigDict(frozen=True)

    evaluations: tuple[Evaluation, ...] = Field(min_length=1)
    recipient: str
    target: ChannelTarget
    locale: str = "en"
    #: True when this message summarises a status group rather than one component.
    digest: bool = False
    #: Only meaningful for a digest. See Route.summary_ack.
    ack_mode: Literal["components", "digest", "none"] = "components"

    @model_validator(mode="before")
    @classmethod
    def _accept_single_evaluation(cls, data: Any) -> Any:
        """Allow ``evaluation=`` as shorthand for a one-item message."""
        if isinstance(data, dict) and "evaluation" in data and "evaluations" not in data:
            data = {**data, "evaluations": (data.pop("evaluation"),)}
        return data

    @property
    def evaluation(self) -> Evaluation:
        """The first evaluation. For a detailed message this is the only one."""
        return self.evaluations[0]

    @property
    def status(self) -> Status:
        """The status this message is about. A digest groups a single status."""
        return self.evaluations[0].status

    @property
    def is_critical(self) -> bool:
        """Critical if anything in the message is, so a digest never downgrades an EXPIRED."""
        return any(e.is_critical for e in self.evaluations)

    @property
    def digest_key(self) -> str:
        """Identifies this digest: the recipient-independent status group it represents.

        Deliberately excludes the member set, so the same group keeps one identity as items
        come and go. The *set* is what the fingerprint captures.
        """
        return f"__digest__|{self.status.value}"

    @property
    def digest_fingerprint(self) -> str:
        """Digest of exactly which situations this message listed.

        In ``digest`` ack mode this is what a confirmation is recorded against, so adding or
        removing a component lapses the ack and the group is sent again in full.
        """
        material = "|".join(
            sorted(f"{e.ack_key}@{e.component.fingerprint}" for e in self.evaluations)
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    @property
    def ack_key(self) -> str:
        """What a confirmation on this message is recorded against.

        One key whether the message is detailed or a digest, so the router and the ack
        listener do not each need to know which kind they are holding.
        """
        return self.digest_key if self.digest else self.evaluations[0].ack_key

    @property
    def ack_fingerprint(self) -> str:
        """The facts this message showed, against which a confirmation is recorded."""
        return self.digest_fingerprint if self.digest else self.evaluations[0].component.fingerprint

    def members(self) -> list[tuple[str, str]]:
        """``(ack_key, fingerprint)`` for every situation listed in this message."""
        return [(e.ack_key, e.component.fingerprint) for e in self.evaluations]

    @property
    def dedup_key(self) -> str:
        """``(component_id, status, rung)`` scoped per recipient and channel (CLAUDE.md §8.2)."""
        if self.digest:
            return "|".join(
                (
                    self.recipient,
                    self.target.channel,
                    self.target.address,
                    self.digest_key,
                    self.digest_fingerprint,
                )
            )
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

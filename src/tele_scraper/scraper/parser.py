"""Portal JSON -> :class:`Component`.

Pure module: no network, no clock reads, no environment access (CLAUDE.md §5).

The portal is a Vue SPA backed by ``/api/cbp/thirdapi/v1/renewal_service/renewlist``, which
returns JSON, so there is no HTML to parse.

Two properties of that payload drive everything here, both established by arithmetic against a
real response rather than assumed:

* **``validityPeriod`` is time REMAINING, not the purchased duration.** It tracks
  ``expirationTime - now`` to within minutes and goes negative once a component lapses, while
  the actual purchased span is months. It is therefore *never* mapped to
  :attr:`Component.validity_period`, which §6 uses to *derive* an expiry - doing so would
  compute a nonsense expiry a few days out from a component bought a year ago.
* **``status`` is not trustworthy for expiry.** The live portal reports ``"Active"`` for
  components whose ``expirationTime`` passed days earlier. It is kept verbatim as
  ``portal_status`` and never allowed to decide the outcome (§1.1, §6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from tele_scraper.errors import ParseError
from tele_scraper.models import AccountInfo, Component

#: Response field names, in one place rather than scattered inline (CLAUDE.md §7.7).
FIELDS: Final[dict[str, str]] = {
    "component_id": "id",
    "name": "name",
    "portal_status": "status",
    "activated_at": "purchaseTime",
    "expires_at": "expirationTime",
    "service_type": "serviceType",
    #: Remaining time, NOT purchased duration - see the module docstring.
    "remaining_reported": "validityPeriod",
}

#: Go's default time rendering, e.g. ``2026-09-22 05:19:55.864 +0000 UTC``. The trailing zone
#: abbreviation is decorative; the numeric offset is authoritative.
_GO_TIME = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"
    r"(?P<frac>\.\d+)?\s*"
    r"(?P<offset>[+-]\d{2}:?\d{2}|Z)"
    r"(?:\s+\S+)?$"
)

#: Go duration components, e.g. ``19d11h50m5s`` or ``-1d-2h-29m-49s`` (sign per component).
#: ``ms`` must precede ``m`` so milliseconds are not read as minutes.
_GO_DURATION = re.compile(r"(-?\d+(?:\.\d+)?)(ns|us|µs|ms|h|d|m|s)")
_UNIT_SECONDS: Final[dict[str, float]] = {
    "d": 86400.0,
    "h": 3600.0,
    "m": 60.0,
    "s": 1.0,
    "ms": 1e-3,
    "us": 1e-6,
    "µs": 1e-6,
    "ns": 1e-9,
}


def parse_timestamp(raw: str) -> datetime:
    """Parse a portal timestamp into an aware UTC datetime.

    Handles Go's ``2006-01-02 15:04:05.000 -0700 MST`` rendering and the ISO-8601 ``...Z`` form
    the same payload uses elsewhere.

    Raises:
        ValueError: if the string does not match either shape.
    """
    match = _GO_TIME.match(raw.strip())
    if match is None:
        raise ValueError(f"unrecognised timestamp {raw!r}")

    stamp = match.group("stamp").replace("T", " ")
    # strptime's %f accepts at most 6 digits; Go emits up to 9.
    frac = (match.group("frac") or ".0")[:7].ljust(2, "0")
    offset = match.group("offset")
    offset = "+0000" if offset == "Z" else offset.replace(":", "")

    parsed = datetime.strptime(f"{stamp}{frac} {offset}", "%Y-%m-%d %H:%M:%S.%f %z")
    return parsed.astimezone(UTC)


def parse_duration(raw: str) -> timedelta:
    """Parse a Go-style duration. Negative components are summed, so ``-1d-2h`` is -26 hours.

    Raises:
        ValueError: if no duration component can be read.
    """
    parts = _GO_DURATION.findall(raw.strip())
    if not parts:
        raise ValueError(f"unrecognised duration {raw!r}")
    return timedelta(seconds=sum(float(v) * _UNIT_SECONDS[u] for v, u in parts))


@dataclass(frozen=True, slots=True)
class Page:
    """One page of the renewal listing."""

    items: list[dict[str, Any]]
    total: int


def parse_envelope(payload: object) -> Page:
    """Validate the response envelope and return its items.

    The portal wraps every response as ``{"status": 0, "resMsg": "...", "data": {...}}``, where
    a non-zero ``status`` is an application-level error even though the HTTP status was 200.

    Raises:
        ParseError: if the envelope is missing, malformed, or reports failure.
    """
    if not isinstance(payload, dict):
        raise ParseError(f"expected a JSON object at the top level, got {type(payload).__name__}")

    status = payload.get("status")
    if status not in (0, "0"):
        raise ParseError(
            f"portal reported failure: status={status!r} resMsg={payload.get('resMsg')!r}"
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ParseError(f"expected 'data' to be an object, got {type(data).__name__}")

    items = data.get("items")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ParseError(f"expected 'data.items' to be a list, got {type(items).__name__}")

    total = data.get("total")
    if not isinstance(total, int) or total < 0:
        # Not fatal: fall back to what we can see rather than abandoning a usable page.
        total = len(items)

    return Page(items=[i for i in items if isinstance(i, dict)], total=total)


def parse_login_token(payload: object) -> str:
    """Pull the session token out of a login response.

    The login endpoint uses the same ``{status, resMsg, data}`` envelope as the listing, so a
    wrong password comes back as HTTP 200 with a non-zero ``status`` - never as a 401.

    Raises:
        ParseError: if the envelope is malformed or carries no token.
    """
    page_data = payload
    if not isinstance(page_data, dict):
        raise ParseError(f"login response was not a JSON object, got {type(payload).__name__}")
    status = page_data.get("status")
    if status not in (0, "0"):
        raise ParseError(f"login rejected: status={status!r} resMsg={page_data.get('resMsg')!r}")
    data = page_data.get("data")
    if not isinstance(data, dict):
        raise ParseError("login response carried no 'data' object")
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise ParseError("login succeeded but returned no token")
    return token


#: Ids for the synthetic components representing our own credentials.
PASSWORD_COMPONENT_ID: Final[str] = "__account_password__"  # noqa: S105 - an id, not a secret
ACCOUNT_COMPONENT_ID: Final[str] = "__account_expiry__"

#: Epoch-millisecond fields in the login response.
ACCOUNT_FIELDS: Final[dict[str, str]] = {
    "password_expires_at": "passwordExpiryDate",
    "account_expires_at": "expiredDate",
}


def _epoch_millis(value: object) -> datetime | None:
    """Convert the portal's epoch-millisecond timestamps to aware UTC, or None if unusable."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_account(payload: object) -> AccountInfo:
    """Read credential lifetimes from a login response.

    Never raises: a portal that stops returning these fields should not break logging in. The
    absence simply means no credential component is produced, which the runner logs.
    """
    if not isinstance(payload, dict):
        return AccountInfo()
    data = payload.get("data")
    if not isinstance(data, dict):
        return AccountInfo()
    return AccountInfo(
        username=str(data.get("username") or ""),
        password_expires_at=_epoch_millis(data.get(ACCOUNT_FIELDS["password_expires_at"])),
        account_expires_at=_epoch_millis(data.get(ACCOUNT_FIELDS["account_expires_at"])),
    )


def account_components(info: AccountInfo, *, include_account: bool = True) -> list[Component]:
    """Represent our own credentials as components, so they use the same §6 ladder.

    Modelling them as components rather than a special case means they inherit routing,
    acknowledgement, templates and dedup for free.
    """
    who = f" ({info.username})" if info.username else ""
    out: list[Component] = []
    if info.password_expires_at is not None:
        out.append(
            Component(
                component_id=PASSWORD_COMPONENT_ID,
                name=f"Portal password{who}",
                kind="credential",
                expires_at=info.password_expires_at,
            )
        )
    if include_account and info.account_expires_at is not None:
        out.append(
            Component(
                component_id=ACCOUNT_COMPONENT_ID,
                name=f"Portal account{who}",
                kind="credential",
                expires_at=info.account_expires_at,
            )
        )
    return out


def parse_component(item: dict[str, Any], *, index: int = 0) -> Component:
    """Convert one listing entry into a :class:`Component`.

    Resilient and loud: a field we cannot read becomes ``parse_error``, which §6 turns into
    ``UNKNOWN`` and an operator alert. It never returns a default that would read as healthy
    (CLAUDE.md §7.6).

    Args:
        item: One entry from ``data.items``.
        index: Position in the page, used only to label an entry that has no usable id.
    """
    component_id = str(item.get(FIELDS["component_id"]) or "").strip()
    name = str(item.get(FIELDS["name"]) or "").strip()
    service_type = str(item.get(FIELDS["service_type"]) or "").strip()
    portal_status = item.get(FIELDS["portal_status"])
    portal_status = str(portal_status).strip() if portal_status is not None else None

    if not component_id:
        # §1.1 forbids deriving the key from the display name, so use a positional sentinel and
        # flag it. A colliding or invented id would corrupt dedup.
        return Component(
            component_id=f"unidentified-item-{index}",
            name=name,
            portal_status=portal_status,
            parse_error="entry has no 'id'; cannot be tracked across runs",
        )

    label = f"{name} ({service_type})" if service_type and name else name

    errors: list[str] = []

    expires_at: datetime | None = None
    raw_expiry = item.get(FIELDS["expires_at"])
    if raw_expiry in (None, ""):
        errors.append("'expirationTime' is missing")
    else:
        try:
            expires_at = parse_timestamp(str(raw_expiry))
        except ValueError as exc:
            errors.append(f"'expirationTime' unreadable: {exc}")

    activated_at: datetime | None = None
    raw_activation = item.get(FIELDS["activated_at"])
    if raw_activation not in (None, ""):
        try:
            activated_at = parse_timestamp(str(raw_activation))
        except ValueError:
            # Not fatal: expirationTime is the authoritative field (§1.1).
            activated_at = None

    return Component(
        component_id=component_id,
        name=label,
        portal_status=portal_status,
        activated_at=activated_at,
        # Deliberately left unset: the portal's validityPeriod is time remaining, not the
        # purchased duration, so it must not feed §6's expiry derivation.
        validity_period=None,
        expires_at=expires_at,
        parse_error="; ".join(errors) if errors else None,
    )


def parse_components(items: list[dict[str, Any]]) -> list[Component]:
    """Convert a full listing into components, preserving order."""
    return [parse_component(item, index=i) for i, item in enumerate(items)]

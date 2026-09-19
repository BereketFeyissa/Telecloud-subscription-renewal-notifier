"""Watching our own credentials.

An expired portal password does not degrade this service, it silences it: every run fails to
authenticate while the absence of alerts looks like good news. These dates arrive free with
every login, so watching them costs no extra request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tele_scraper.domain.rules import parse_thresholds
from tele_scraper.domain.status import evaluate_all
from tele_scraper.models import AccountInfo, Status
from tele_scraper.notify.base import MessageRenderer
from tele_scraper.scraper.parser import (
    ACCOUNT_COMPONENT_ID,
    PASSWORD_COMPONENT_ID,
    account_components,
    parse_account,
)
from tests.conftest import NOW
from tests.unit.test_notifiers import make_event

LADDER = parse_thresholds("30d,14d,7d,3d,1d")


def login_body(password_ms: int | None = None, account_ms: int | None = None) -> dict:
    data: dict[str, object] = {"username": "svc-account", "token": "t"}
    if password_ms is not None:
        data["passwordExpiryDate"] = password_ms
    if account_ms is not None:
        data["expiredDate"] = account_ms
    return {"status": 0, "data": data}


def ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


# --- parsing -----------------------------------------------------------------------


def test_expiry_dates_are_read_from_the_login_response() -> None:
    expires = datetime(2026, 10, 14, 7, 54, tzinfo=UTC)
    info = parse_account(login_body(password_ms=ms(expires)))
    assert info.username == "svc-account"
    assert info.password_expires_at == expires
    assert info.password_expires_at.tzinfo is UTC


@pytest.mark.parametrize(
    "payload", [None, "text", 42, {}, {"status": 0}, {"status": 0, "data": "nope"}]
)
def test_parsing_never_raises_on_an_unexpected_login_body(payload: object) -> None:
    """A portal that stops returning these must not break logging in."""
    assert parse_account(payload) == AccountInfo()


@pytest.mark.parametrize("bad", [0, -1, "soon", None, 10**18])
def test_unusable_timestamps_are_dropped_not_guessed(bad: object) -> None:
    assert parse_account(login_body(password_ms=bad)).password_expires_at is None  # type: ignore[arg-type]


# --- synthetic components ----------------------------------------------------------


def test_both_credentials_become_components() -> None:
    info = AccountInfo(
        username="svc",
        password_expires_at=NOW + timedelta(days=26),
        account_expires_at=NOW + timedelta(days=90),
    )
    ids = [c.component_id for c in account_components(info)]
    assert ids == [PASSWORD_COMPONENT_ID, ACCOUNT_COMPONENT_ID]
    assert all(c.kind == "credential" for c in account_components(info))


def test_account_expiry_can_be_left_out() -> None:
    info = AccountInfo(password_expires_at=NOW, account_expires_at=NOW)
    ids = [c.component_id for c in account_components(info, include_account=False)]
    assert ids == [PASSWORD_COMPONENT_ID]


def test_no_dates_means_no_components() -> None:
    assert account_components(AccountInfo()) == []


def test_the_username_is_shown_so_the_message_says_which_account() -> None:
    info = AccountInfo(username="svc-account", password_expires_at=NOW)
    assert "svc-account" in account_components(info)[0].name


# --- the wider ladder --------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (90, Status.ACTIVE),
        (31, Status.ACTIVE),
        (26, Status.EXPIRING_SOON),
        (1, Status.EXPIRING_SOON),
        (-1, Status.EXPIRED),
    ],
)
def test_credentials_use_their_own_wider_ladder(days: int, expected: Status) -> None:
    """26 days out must already warn: a password change needs coordination and a redeploy."""
    info = AccountInfo(password_expires_at=NOW + timedelta(days=days))
    (result,) = evaluate_all(
        account_components(info, include_account=False), now=NOW, thresholds=LADDER
    )
    assert result.status is expected


def test_the_component_ladder_would_have_stayed_silent_at_26_days() -> None:
    """Why the wider ladder exists: the default ladder gives no warning until day 14."""
    info = AccountInfo(password_expires_at=NOW + timedelta(days=26))
    (result,) = evaluate_all(
        account_components(info, include_account=False),
        now=NOW,
        thresholds=parse_thresholds("14d,7d,3d,1d,12h"),
    )
    assert result.status is Status.ACTIVE


# --- messaging ---------------------------------------------------------------------


def test_a_credential_message_does_not_call_itself_a_telecloud_component(
    renderer: MessageRenderer,
) -> None:
    event = make_event("email", "ops@example.test")
    component = event.evaluation.component.model_copy(
        update={"kind": "credential", "name": "Portal password (svc)"}
    )
    credential = event.model_copy(
        update={"evaluations": (event.evaluation.model_copy(update={"component": component}),)}
    )
    message = renderer.render(credential)
    assert "telecloud component" not in message.subject
    assert "Portal password (svc)" in message.subject


def test_a_credential_message_explains_what_its_expiry_costs(
    renderer: MessageRenderer,
) -> None:
    event = make_event("email", "ops@example.test")
    component = event.evaluation.component.model_copy(update={"kind": "credential"})
    credential = event.model_copy(
        update={"evaluations": (event.evaluation.model_copy(update={"component": component}),)}
    )
    body = renderer.render(credential).body
    assert "you stop receiving these alerts" in body
    assert "PORTAL_PASSWORD_HASH" in body


def test_an_ordinary_component_message_is_unchanged(renderer: MessageRenderer) -> None:
    message = renderer.render(make_event("email", "ops@example.test"))
    assert "PORTAL_PASSWORD_HASH" not in message.body

"""Channel notifiers: payload shape, rate limiting, and failure handling (CLAUDE.md §11.6)."""

from __future__ import annotations

import asyncio
import json
import smtplib
from datetime import timedelta
from types import TracebackType
from typing import Any, ClassVar

import httpx
import pytest
import respx

from tele_scraper.errors import ProviderNotSelectedError
from tele_scraper.models import ChannelTarget, Evaluation, NotificationEvent, Status
from tele_scraper.notify.base import humanize, post_json, truncate
from tele_scraper.notify.discord import DiscordNotifier
from tele_scraper.notify.email import EmailNotifier
from tele_scraper.notify.slack import SlackNotifier
from tele_scraper.notify.sms import SmsNotifier
from tele_scraper.notify.telegram import TelegramNotifier
from tests.conftest import NOW, make_component

TOKEN = "123456:AAH-fake-bot-token"


def make_event(channel: str, address: str) -> NotificationEvent:
    evaluation = Evaluation(
        component=make_component("comp-42", "Storage Bundle", expires_in=timedelta(days=2)),
        status=Status.EXPIRING_SOON,
        evaluated_at=NOW,
        rung=timedelta(days=3),
        remaining=timedelta(days=2),
    )
    return NotificationEvent(
        evaluation=evaluation,
        recipient="ops",
        target=ChannelTarget(channel=channel, address=address),
    )


# --- helpers -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(days=2, hours=3), "2 days, 3 hours"),
        (timedelta(days=1), "1 day"),
        (timedelta(hours=5), "5 hours"),
        (timedelta(minutes=30), "30 minutes"),
        (timedelta(minutes=1), "1 minute"),
        (timedelta(seconds=10), "less than a minute"),
        (timedelta(days=-1, hours=-2), "1 day, 2 hours ago"),
    ],
)
def test_humanize(delta: timedelta, expected: str) -> None:
    assert humanize(delta) == expected


def test_truncate_marks_what_it_cut() -> None:
    assert truncate("short", 100) == "short"
    out = truncate("x" * 50, 20)
    assert len(out) == 20
    assert out.endswith("[truncated]")


# --- rendering ---------------------------------------------------------------------


def test_body_carries_everything_section_8_5_requires(renderer) -> None:  # type: ignore[no-untyped-def]
    message = renderer.render(make_event("slack", "https://hooks.test/x"))
    body = message.body
    assert "Storage Bundle" in body
    assert "comp-42" in body
    assert "EXPIRING_SOON" in body
    assert "2 days" in body
    assert "UTC+0300" in body, "an expiry shown to a human must carry its offset"


def test_unknown_status_body_says_it_is_not_confirmed_active(renderer) -> None:  # type: ignore[no-untyped-def]
    event = make_event("slack", "https://hooks.test/x")
    unknown = event.model_copy(
        update={"evaluations": (event.evaluation.model_copy(update={"status": Status.UNKNOWN}),)}
    )
    assert "not confirmed active" in renderer.render(unknown).body.lower()


def test_derived_expiry_is_called_out(renderer) -> None:  # type: ignore[no-untyped-def]
    event = make_event("slack", "https://hooks.test/x")
    component = event.evaluation.component.model_copy(update={"expires_at_derived": True})
    derived = event.model_copy(
        update={"evaluations": (event.evaluation.model_copy(update={"component": component}),)}
    )
    assert "derived" in renderer.render(derived).body.lower()


# --- telegram ----------------------------------------------------------------------


@respx.mock
async def test_telegram_payload_shape(renderer) -> None:  # type: ignore[no-untyped-def]
    route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with httpx.AsyncClient() as client:
        notifier = TelegramNotifier(client, renderer, bot_token=TOKEN)
        result = await notifier.send(make_event("telegram", "-1001234567890"))

    assert result.ok is True
    payload = json.loads(route.calls[0].request.content)
    assert payload["chat_id"] == "-1001234567890"
    assert payload["parse_mode"] == "Markdown"
    assert "comp-42" in payload["text"]


@respx.mock
async def test_telegram_rate_limit_is_reported_not_retried(renderer) -> None:  # type: ignore[no-untyped-def]
    route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
        return_value=httpx.Response(429, headers={"retry-after": "42"}, json={"ok": False})
    )
    async with httpx.AsyncClient() as client:
        notifier = TelegramNotifier(client, renderer, bot_token=TOKEN)
        result = await notifier.send(make_event("telegram", "123"))

    assert result.ok is False
    assert result.retry_after == 42.0
    assert route.call_count == 1, "a 429 must not be hammered (CLAUDE.md §8.7)"


@respx.mock
async def test_telegram_rejection_is_a_failure_not_an_exception(renderer) -> None:  # type: ignore[no-untyped-def]
    respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
        return_value=httpx.Response(400, json={"ok": False})
    )
    async with httpx.AsyncClient() as client:
        notifier = TelegramNotifier(client, renderer, bot_token=TOKEN)
        result = await notifier.send(make_event("telegram", "123"))
    assert result.ok is False


@respx.mock
async def test_telegram_transport_error_is_a_failure(renderer) -> None:  # type: ignore[no-untyped-def]
    respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    async with httpx.AsyncClient() as client:
        notifier = TelegramNotifier(client, renderer, bot_token=TOKEN)
        result = await notifier.send(make_event("telegram", "123"))
    assert result.ok is False
    assert "transport error" in (result.error or "")


# --- slack / discord ---------------------------------------------------------------


@respx.mock
async def test_slack_payload_shape(renderer) -> None:  # type: ignore[no-untyped-def]
    url = "https://hooks.example.test/services/T/B/X"
    route = respx.post(url).mock(return_value=httpx.Response(200, text="ok"))
    async with httpx.AsyncClient() as client:
        result = await SlackNotifier(client, renderer).send(make_event("slack", url))
    assert result.ok is True
    assert "comp-42" in json.loads(route.calls[0].request.content)["text"]


@respx.mock
async def test_discord_payload_shape_and_mention_suppression(renderer) -> None:  # type: ignore[no-untyped-def]
    url = "https://discord.example.test/api/webhooks/1/x"
    route = respx.post(url).mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as client:
        result = await DiscordNotifier(client, renderer).send(make_event("discord", url))
    assert result.ok is True
    payload = json.loads(route.calls[0].request.content)
    assert "comp-42" in payload["content"]
    assert payload["allowed_mentions"] == {"parse": []}
    assert len(payload["content"]) <= 2000


@respx.mock
@pytest.mark.parametrize("notifier_cls", [SlackNotifier, DiscordNotifier])
async def test_webhook_rate_limit_is_reported(renderer, notifier_cls: Any) -> None:  # type: ignore[no-untyped-def]
    url = "https://hooks.example.test/x"
    respx.post(url).mock(return_value=httpx.Response(429, headers={"retry-after": "7"}))
    async with httpx.AsyncClient() as client:
        result = await notifier_cls(client, renderer).send(make_event("slack", url))
    assert result.ok is False
    assert result.retry_after == 7.0


@respx.mock
async def test_webhook_rejection_is_a_failure(renderer) -> None:  # type: ignore[no-untyped-def]
    url = "https://hooks.example.test/x"
    respx.post(url).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        result = await SlackNotifier(client, renderer).send(make_event("slack", url))
    assert result.ok is False


@respx.mock
async def test_webhook_transport_error_is_a_failure(renderer) -> None:  # type: ignore[no-untyped-def]
    url = "https://hooks.example.test/x"
    respx.post(url).mock(side_effect=httpx.ConnectTimeout("timed out"))
    async with httpx.AsyncClient() as client:
        result = await DiscordNotifier(client, renderer).send(make_event("discord", url))
    assert result.ok is False


# --- retry policy ------------------------------------------------------------------


@respx.mock
async def test_5xx_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient upstream faults are retried with backoff; the sleep is patched out (§11.5)."""

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)
    url = "https://hooks.example.test/x"
    route = respx.post(url).mock(side_effect=[httpx.Response(503), httpx.Response(200, text="ok")])
    async with httpx.AsyncClient() as client:
        response = await post_json(client, url, {"a": 1}, timeout=5, max_attempts=3)
    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_5xx_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)
    url = "https://hooks.example.test/x"
    route = respx.post(url).mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        with pytest.raises(Exception, match="upstream 500"):
            await post_json(client, url, {"a": 1}, timeout=5, max_attempts=2)
    assert route.call_count == 2


# --- email -------------------------------------------------------------------------


class FakeSmtp:
    """Stand-in for smtplib.SMTP that records the conversation."""

    instances: ClassVar[list[FakeSmtp]] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.started_tls = False
        self.logged_in: tuple[str, str] | None = None
        self.messages: list[Any] = []
        FakeSmtp.instances.append(self)

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        self.logged_in = (username, password)

    def send_message(self, message: Any) -> None:
        self.messages.append(message)


async def test_email_sends_with_starttls_and_login(
    renderer, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    FakeSmtp.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    notifier = EmailNotifier(
        renderer,
        host="smtp.example.test",
        port=587,
        sender="alerts@example.test",
        username="svc",
        password="pw",
    )
    result = await notifier.send(make_event("email", "ops@example.test"))

    assert result.ok is True
    (server,) = FakeSmtp.instances
    assert server.started_tls is True
    assert server.logged_in == ("svc", "pw")
    message = server.messages[0]
    assert message["To"] == "ops@example.test"
    assert "comp-42" in message.get_content()


async def test_email_failure_is_reported_not_raised(
    renderer, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise smtplib.SMTPConnectError(421, "service unavailable")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    notifier = EmailNotifier(
        renderer, host="smtp.example.test", port=587, sender="alerts@example.test"
    )
    result = await notifier.send(make_event("email", "ops@example.test"))
    assert result.ok is False
    assert "smtp error" in (result.error or "")


# --- sms ---------------------------------------------------------------------------


async def test_sms_fails_closed_until_a_provider_is_chosen() -> None:
    """§0: no invented integration. It refuses loudly rather than dropping the alert."""
    with pytest.raises(ProviderNotSelectedError, match="provider has not been chosen"):
        await SmsNotifier().send(make_event("sms", "+251900000000"))


# --- confirm button ----------------------------------------------------------------


@respx.mock
async def test_telegram_message_carries_a_confirm_button(renderer) -> None:  # type: ignore[no-untyped-def]
    route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with httpx.AsyncClient() as client:
        notifier = TelegramNotifier(client, renderer, bot_token=TOKEN)
        event = make_event("telegram", "123")
        await notifier.send(event)

    payload = json.loads(route.calls[0].request.content)
    button = payload["reply_markup"]["inline_keyboard"][0][0]
    assert "Confirm" in button["text"]
    assert button["callback_data"] == f"ack:{event.evaluation.ack_key}"


@respx.mock
async def test_the_confirm_button_can_be_switched_off(renderer) -> None:  # type: ignore[no-untyped-def]
    route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with httpx.AsyncClient() as client:
        notifier = TelegramNotifier(client, renderer, bot_token=TOKEN, offer_ack=False)
        await notifier.send(make_event("telegram", "123"))
    assert "reply_markup" not in json.loads(route.calls[0].request.content)


def test_the_email_body_keeps_its_line_breaks(renderer) -> None:  # type: ignore[no-untyped-def]
    """Regression: trim_blocks ate the newline after {% endif %}, gluing Portal onto Status.

    Only visible by reading a delivered message, which is why the self-test sends real mail.
    """
    event = make_event("email", "ops@example.test")
    with_reason = event.model_copy(
        update={"evaluations": (event.evaluation.model_copy(update={"reason": "inside window"}),)}
    )
    lines = renderer.render(with_reason).body.splitlines()
    status_lines = [ln for ln in lines if ln.startswith("Status")]
    portal_lines = [ln for ln in lines if ln.startswith("Portal")]
    assert status_lines and portal_lines, lines
    assert "Portal" not in status_lines[0], "Portal must start its own line"
    assert "inside window" in status_lines[0]


# --- SMTP transport ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("port", "tls", "expected"),
    [
        (465, "auto", "ssl"),
        (587, "auto", "starttls"),
        (25, "auto", "starttls"),
        (465, "starttls", "starttls"),
        (587, "ssl", "ssl"),
        (25, "none", "none"),
    ],
)
def test_tls_mode_resolution(renderer, port: int, tls: str, expected: str) -> None:  # type: ignore[no-untyped-def]
    """Port 465 is implicit TLS; pointing a STARTTLS client at it hangs."""
    notifier = EmailNotifier(
        renderer,
        host="h",
        port=port,
        sender="a@b.test",
        tls=tls,  # type: ignore[arg-type]
    )
    assert notifier.tls_mode == expected


async def test_port_465_uses_implicit_tls(renderer, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    used: dict[str, Any] = {}

    class FakeSmtpSsl(FakeSmtp):
        def __init__(self, host: str, port: int, timeout: float, context: Any = None) -> None:
            super().__init__(host, port, timeout)
            used["ssl"] = True

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtpSsl)
    monkeypatch.setattr(
        smtplib, "SMTP", lambda *a, **k: pytest.fail("must not open a plaintext socket on 465")
    )
    FakeSmtp.instances.clear()
    notifier = EmailNotifier(renderer, host="mail.test", port=465, sender="a@b.test")
    result = await notifier.send(make_event("email", "ops@example.test"))

    assert result.ok is True
    assert used.get("ssl") is True
    assert FakeSmtp.instances[0].started_tls is False, "no STARTTLS on an already-encrypted link"


async def test_port_587_uses_starttls(renderer, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(
        smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("587 must not use implicit TLS")
    )
    FakeSmtp.instances.clear()
    notifier = EmailNotifier(renderer, host="mail.test", port=587, sender="a@b.test")
    await notifier.send(make_event("email", "ops@example.test"))
    assert FakeSmtp.instances[0].started_tls is True

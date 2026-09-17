"""CLI wiring. The entrypoint is thin by contract, so these tests cover exit codes."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from tele_scraper import __main__ as main_module
from tele_scraper.errors import ConfigError, ScrapeError, StateStoreError
from tele_scraper.observability import logging as logging_module
from tests.conftest import make_settings


def fake_loader(**overrides: Any) -> Any:
    """Stand-in for load_settings that matches its real keyword-only signature."""

    def _loader(*, require_notifications: bool = True) -> Any:
        return make_settings(**overrides)

    return _loader


def test_defaults() -> None:
    args = main_module._parse_args([])
    assert (args.once, args.dry_run, args.check_config) == (False, False, False)


def test_flags() -> None:
    args = main_module._parse_args(["--once", "--dry-run", "--check-config"])
    assert (args.once, args.dry_run, args.check_config) == (True, True, True)


def test_config_error_exits_four(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*, require_notifications: bool = True) -> Any:
        raise ConfigError("PORTAL_BASE_URL is required")

    monkeypatch.setattr(main_module, "load_settings", boom)
    assert main_module.main([]) == main_module.EXIT_CONFIG_ERROR


def test_check_config_validates_and_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "load_settings", fake_loader())
    assert main_module.main(["--check-config"]) == 0


def test_check_config_warns_when_no_suspended_vocabulary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main_module, "load_settings", fake_loader())
    main_module.main(["--check-config"])
    assert "config.no_suspended_tokens" in capsys.readouterr().out


def test_check_config_is_quiet_when_vocabulary_is_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        main_module, "load_settings", fake_loader(portal_suspended_tokens="suspended")
    )
    main_module.main(["--check-config"])
    assert "config.no_suspended_tokens" not in capsys.readouterr().out


def test_dry_run_flag_overrides_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_run(settings: Any, *, once: bool) -> int:
        captured["dry_run"] = settings.dry_run
        captured["once"] = once
        return 0

    monkeypatch.setattr(main_module, "load_settings", fake_loader())
    monkeypatch.setattr(main_module, "_run", fake_run)
    assert main_module.main(["--once", "--dry-run"]) == 0
    assert captured == {"dry_run": True, "once": True}


def test_run_exit_code_is_propagated(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(_settings: Any, *, once: bool) -> int:
        return 2

    monkeypatch.setattr(main_module, "load_settings", fake_loader())
    monkeypatch.setattr(main_module, "_run", fake_run)
    assert main_module.main(["--once"]) == 2


def test_state_store_failure_exits_four(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(_settings: Any, *, once: bool) -> int:
        raise StateStoreError("pvc not mounted")

    monkeypatch.setattr(main_module, "load_settings", fake_loader())
    monkeypatch.setattr(main_module, "_run", fake_run)
    assert main_module.main([]) == main_module.EXIT_CONFIG_ERROR


def test_other_fatal_errors_exit_one(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(_settings: Any, *, once: bool) -> int:
        raise ScrapeError("portal gone")

    monkeypatch.setattr(main_module, "load_settings", fake_loader())
    monkeypatch.setattr(main_module, "_run", fake_run)
    assert main_module.main([]) == 1


def test_keyboard_interrupt_is_a_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(_settings: Any, *, once: bool) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(main_module, "load_settings", fake_loader())
    monkeypatch.setattr(main_module, "_run", fake_run)
    assert main_module.main([]) == 0


def test_credentials_are_registered_for_redaction() -> None:
    settings = make_settings(telegram_bot_token="123:AA-secret-token")
    main_module._register_secrets(settings)
    out = logging_module.redact_processor(
        None, "info", {"event": "using hunter2-hunter2 and 123:AA-secret-token"}
    )
    assert "hunter2-hunter2" not in str(out["event"])
    assert "123:AA-secret-token" not in str(out["event"])


# --- capture mode ------------------------------------------------------------------


def test_capture_flag_defaults_and_overrides() -> None:
    assert main_module._parse_args([]).capture is None
    assert main_module._parse_args(["--capture"]).capture == "portal-capture"
    assert main_module._parse_args(["--capture", "out.html"]).capture == "out.html"
    assert main_module._parse_args(["--capture", "--url", "/x"]).url == "/x"


def test_capture_writes_the_page_and_names_the_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakePortal:
        def __init__(self, _settings: Any) -> None:
            pass

        async def capture(self, path: str | None) -> tuple[str, str, str]:
            return ('{"items": []}', "application/json", "json")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(main_module, "load_settings", fake_loader())
    monkeypatch.setattr(main_module, "HttpPortalClient", FakePortal)
    destination = tmp_path / "capture"
    assert main_module.main(["--capture", str(destination)]) == 0

    saved = destination.with_suffix(".json")
    assert saved.read_text() == '{"items": []}'
    assert "capture.saved" in capsys.readouterr().out


def test_capture_warns_when_the_session_was_not_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    class LoginPagePortal:
        def __init__(self, _settings: Any) -> None:
            pass

        async def capture(self, path: str | None) -> tuple[str, str, str]:
            return ("<html><input type='password'></html>", "text/html", "login_page")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(main_module, "load_settings", fake_loader())
    monkeypatch.setattr(main_module, "HttpPortalClient", LoginPagePortal)
    assert main_module.main(["--capture", str(tmp_path / "c")]) == 0
    assert "capture.looks_like_login" in capsys.readouterr().out


def test_capture_failure_exits_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    class BrokenPortal:
        def __init__(self, _settings: Any) -> None:
            pass

        async def capture(self, path: str | None) -> tuple[str, str, str]:
            raise ScrapeError("portal login flow is not specified")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(main_module, "load_settings", fake_loader())
    monkeypatch.setattr(main_module, "HttpPortalClient", BrokenPortal)
    assert main_module.main(["--capture", str(tmp_path / "c")]) == 1


def test_session_credentials_are_registered_for_redaction() -> None:
    settings = make_settings(portal_session_cookie="SESSIONID=very-secret-session-value")
    main_module._register_secrets(settings)
    out = logging_module.redact_processor(
        None, "info", {"event": "sent SESSIONID=very-secret-session-value"}
    )
    assert "very-secret-session-value" not in str(out["event"])


# --- acknowledgement CLI -----------------------------------------------------------


def test_ack_flags_parse() -> None:
    args = main_module._parse_args(["--ack", "comp-1", "--by", "amanuel"])
    assert (args.ack, args.by) == ("comp-1", "amanuel")
    assert main_module._parse_args(["--ack", "comp-1"]).by == "cli"


def test_ack_confirms_outstanding_alerts(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The universal fallback for recipients who cannot confirm in-channel."""
    from tele_scraper.state.store import MemoryStateStore

    class PersistentMemoryStore(MemoryStateStore):
        """close() must not discard the data here; the real SQLite store closes a connection."""

        async def close(self) -> None:
            return None

    store = PersistentMemoryStore()

    async def seed() -> None:
        await store.record_sent(
            "scope",
            "key",
            recipient="ops",
            channel="slack",
            component_id="comp-1",
            status="EXPIRED",
            rung="-",
            ack_key="comp-1|EXPIRED|-",
            fingerprint="fp",
        )

    asyncio.run(seed())
    monkeypatch.setattr(main_module, "load_settings", fake_loader())
    monkeypatch.setattr(main_module, "build_store", lambda _backend, _dsn: store)

    assert main_module.main(["--ack", "comp-1", "--by", "amanuel"]) == 0
    assert asyncio.run(store.is_acknowledged("comp-1|EXPIRED|-", "fp", now=time.time())) is True


def test_ack_for_an_unknown_component_reports_rather_than_silently_succeeding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tele_scraper.state.store import MemoryStateStore

    monkeypatch.setattr(main_module, "load_settings", fake_loader())
    monkeypatch.setattr(main_module, "build_store", lambda _backend, _dsn: MemoryStateStore())
    assert main_module.main(["--ack", "never-notified"]) == 1
    assert "ack.nothing_outstanding" in capsys.readouterr().out


def test_test_notify_refuses_without_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """It really sends, so §3.5 requires NOTIFY_ENABLED=true."""
    monkeypatch.setattr(main_module, "load_settings", fake_loader(notify_enabled=False))
    assert main_module.main(["--test-notify"]) == main_module.EXIT_CONFIG_ERROR
    assert "test_notify.refused" in capsys.readouterr().out


def test_telegram_chats_needs_a_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main_module, "load_settings", fake_loader(telegram_bot_token=""))
    assert main_module.main(["--telegram-chats"]) == main_module.EXIT_CONFIG_ERROR
    assert "telegram_chats.no_token" in capsys.readouterr().out

"""Configuration and routing-table validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tele_scraper.config import Settings
from tele_scraper.errors import ConfigError
from tests.conftest import make_settings


def test_routing_table_parses_multiple_channels_per_recipient() -> None:
    routes = {
        "routes": [
            {
                "recipient": "amanuel",
                "channels": [
                    {"channel": "email", "address": "a@example.test"},
                    {"channel": "telegram", "address": "12345"},
                    {"channel": "discord", "address": "https://discord.test/webhook"},
                ],
            }
        ]
    }
    settings = make_settings(notify_routes_json=json.dumps(routes))
    (route,) = settings.routing.routes
    assert [t.channel for t in route.targets()] == ["email", "telegram", "discord"]


def test_bare_list_is_accepted_as_a_routing_table() -> None:
    routes = [{"recipient": "ops", "channels": [{"channel": "slack", "address": "https://x.test"}]}]
    settings = make_settings(notify_routes_json=json.dumps(routes))
    assert settings.routing.routes[0].recipient == "ops"


def test_address_env_is_resolved_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_OPS", "https://hooks.example.test/secret")
    routes = {
        "routes": [
            {
                "recipient": "ops",
                "channels": [{"channel": "slack", "address_env": "SLACK_WEBHOOK_OPS"}],
            }
        ]
    }
    settings = make_settings(notify_routes_json=json.dumps(routes))
    assert settings.routing.routes[0].targets()[0].address == "https://hooks.example.test/secret"


def test_missing_address_env_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_OPS", raising=False)
    routes = {
        "routes": [
            {
                "recipient": "ops",
                "channels": [{"channel": "slack", "address_env": "SLACK_WEBHOOK_OPS"}],
            }
        ]
    }
    settings = make_settings(notify_routes_json=json.dumps(routes))
    with pytest.raises(ConfigError, match="SLACK_WEBHOOK_OPS"):
        settings.routing.routes[0].targets()


@pytest.mark.parametrize(
    ("channels", "message"),
    [
        ([{"channel": "carrier-pigeon", "address": "x"}], "unknown channel"),
        ([{"channel": "email"}], "exactly one"),
        ([{"channel": "email", "address": "a", "address_env": "B"}], "exactly one"),
    ],
)
def test_invalid_channel_specs_are_rejected(channels: list[dict], message: str) -> None:
    routes = {"routes": [{"recipient": "ops", "channels": channels}]}
    settings = make_settings(notify_routes_json=json.dumps(routes))
    with pytest.raises(ConfigError, match=message):
        _ = settings.routing


def test_routing_on_active_is_rejected() -> None:
    routes = {
        "routes": [
            {
                "recipient": "ops",
                "channels": [{"channel": "email", "address": "a@b.test"}],
                "statuses": ["ACTIVE"],
            }
        ]
    }
    settings = make_settings(notify_routes_json=json.dumps(routes))
    with pytest.raises(ConfigError, match="ACTIVE"):
        _ = settings.routing


def test_duplicate_recipients_are_rejected() -> None:
    route = {"recipient": "ops", "channels": [{"channel": "email", "address": "a@b.test"}]}
    settings = make_settings(notify_routes_json=json.dumps({"routes": [route, route]}))
    with pytest.raises(ConfigError, match="duplicate recipient"):
        _ = settings.routing


def test_missing_routing_is_a_config_error() -> None:
    settings = make_settings(notify_routes_json="")
    with pytest.raises(ConfigError, match="no routing configured"):
        _ = settings.routing


def test_malformed_routing_json_is_a_config_error() -> None:
    settings = make_settings(notify_routes_json="{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        _ = settings.routing


def test_routes_file_wins_over_inline_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "routes.json"
    path.write_text(
        json.dumps(
            {
                "routes": [
                    {
                        "recipient": "from-file",
                        "channels": [{"channel": "email", "address": "f@example.test"}],
                    }
                ]
            }
        )
    )
    settings = make_settings(notify_routes_file=path)
    assert settings.routing.routes[0].recipient == "from-file"


def test_unreadable_routes_file_is_a_config_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(notify_routes_file=tmp_path / "does-not-exist.json")
    with pytest.raises(ConfigError, match="cannot read"):
        _ = settings.routing


def test_bad_threshold_ladder_is_a_config_error() -> None:
    settings = make_settings(warn_thresholds="14d,nonsense")
    with pytest.raises(ConfigError, match="WARN_THRESHOLDS"):
        _ = settings.thresholds


def test_suspended_tokens_are_normalized() -> None:
    settings = make_settings(portal_suspended_tokens=" Suspended , BLOCKED  BY  Billing ,")
    assert settings.suspended_tokens == frozenset({"suspended", "blocked by billing"})


def test_suspended_tokens_default_to_empty() -> None:
    """Until the portal vocabulary is known, nothing is ever SUSPENDED (CLAUDE.md §2 open 2)."""
    assert make_settings().suspended_tokens == frozenset()


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(Exception, match="unknown timezone"):
        make_settings(app_timezone="Mars/Olympus_Mons")


def test_portal_url_must_be_http() -> None:
    with pytest.raises(Exception, match="http"):
        make_settings(portal_base_url="portal.example.test")


def test_portal_url_trailing_slash_is_stripped() -> None:
    assert make_settings(portal_base_url="https://p.test/").portal_base_url == "https://p.test"


def test_required_channels_reflects_the_routing_table() -> None:
    assert make_settings().required_channels() == frozenset({"slack", "email"})


def test_password_is_not_reprable() -> None:
    """A SecretStr must not render its value in logs or tracebacks (CLAUDE.md §8.6)."""
    settings: Settings = make_settings()
    assert "hunter2" not in repr(settings)
    assert settings.portal_password.get_secret_value() == "hunter2-hunter2"


# --- authentication requirements ---------------------------------------------------


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run in an empty directory so load_settings finds no .env.

    The autouse fixture in conftest already strips the environment; this covers the file.
    """
    monkeypatch.chdir(tmp_path)


def test_credentials_are_not_required_when_a_session_is_borrowed() -> None:
    """--capture must work before the auth flow is known (CLAUDE.md §2 open 1)."""
    settings = make_settings(
        portal_username="", portal_password="", portal_session_cookie="SESSIONID=abc"
    )
    assert settings.portal_session_cookie.get_secret_value() == "SESSIONID=abc"


def test_auth_header_also_satisfies_the_requirement() -> None:
    settings = make_settings(
        portal_username="", portal_password="", portal_auth_header="Bearer xyz"
    )
    assert settings.portal_auth_header.get_secret_value() == "Bearer xyz"


def test_no_authentication_method_at_all_is_rejected() -> None:
    with pytest.raises(Exception, match="no way to authenticate"):
        make_settings(portal_username="", portal_password="")


def test_half_credentials_are_rejected() -> None:
    with pytest.raises(Exception, match="no way to authenticate"):
        make_settings(portal_username="svc", portal_password="")


def test_load_settings_skips_routing_for_read_only_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Capture mode must not demand a routing table it never uses."""
    from tele_scraper.config import load_settings

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("PORTAL_BASE_URL", "https://portal.example.test")
    monkeypatch.setenv("PORTAL_SESSION_COOKIE", "SESSIONID=abc")

    settings = load_settings(require_notifications=False)
    assert settings.portal_base_url == "https://portal.example.test"

    with pytest.raises(ConfigError, match="no routing configured"):
        load_settings(require_notifications=True)

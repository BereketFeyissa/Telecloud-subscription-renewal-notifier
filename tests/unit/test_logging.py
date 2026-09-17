"""Secret redaction. A leaked credential in a cluster log store is an incident (§8.6)."""

from __future__ import annotations

from tele_scraper.observability.logging import (
    REDACTED,
    redact_processor,
    register_secret,
    reset_secrets,
)


def process(**event: object) -> dict[str, object]:
    return dict(redact_processor(None, "info", dict(event)))


def test_secret_looking_keys_are_redacted() -> None:
    out = process(
        password="p",
        bot_token="t",
        api_key="k",
        Authorization="bearer x",
        webhook="https://hooks/x",
        session_cookie="c",
        state_dsn="postgres://u:p@h/db",
        component_id="comp-1",
    )
    for key in (
        "password",
        "bot_token",
        "api_key",
        "Authorization",
        "webhook",
        "session_cookie",
        "state_dsn",
    ):
        assert out[key] == REDACTED, key
    assert out["component_id"] == "comp-1"


def test_registered_secret_is_scrubbed_from_free_text() -> None:
    register_secret("super-secret-token-value")
    out = process(event="calling https://api.telegram.org/botsuper-secret-token-value/sendMessage")
    assert "super-secret-token-value" not in str(out["event"])
    assert REDACTED in str(out["event"])


def test_registered_secret_is_scrubbed_inside_nested_structures() -> None:
    register_secret("another-secret-value")
    out = process(
        payload={"url": "https://x/another-secret-value", "items": ["another-secret-value"]}
    )
    assert "another-secret-value" not in repr(out["payload"])


def test_short_values_are_not_registered() -> None:
    """Redacting a 3-character string would mangle unrelated log text."""
    reset_secrets()
    register_secret("abc")
    assert process(event="abc def")["event"] == "abc def"


def test_none_secret_is_ignored() -> None:
    register_secret(None)
    assert process(event="unchanged")["event"] == "unchanged"


def test_non_string_values_pass_through() -> None:
    out = process(count=3, ok=True, ratio=1.5)
    assert out == {"count": 3, "ok": True, "ratio": 1.5}

"""Portal transport: timeouts, retries, and the credential-incident rule (CLAUDE.md §7)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from tele_scraper.errors import AuthError, ConfigError, ParseError, ScrapeError
from tele_scraper.scraper.client import FixturePortalClient, HttpPortalClient
from tests.conftest import make_settings

BASE = "https://portal.example.test"
API = "/api/cbp/thirdapi/v1/renewal_service/renewlist"


def envelope(items: list[dict], total: int | None = None) -> dict:
    return {
        "status": 0,
        "resMsg": "succeed",
        "data": {"total": total if total is not None else len(items), "items": items},
    }


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)


def build(**overrides: object) -> HttpPortalClient:
    return HttpPortalClient(make_settings(scrape_delay_seconds=0.0, **overrides))


@respx.mock
async def test_get_returns_body() -> None:
    respx.get(f"{BASE}{API}").mock(return_value=httpx.Response(200, text="<html>ok</html>"))
    async with build() as client:
        assert await client.get(API) == "<html>ok</html>"


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_is_never_retried(status: int) -> None:
    """A 401/403 is a credential incident; retrying risks locking the account (§7.3)."""
    route = respx.get(f"{BASE}{API}").mock(return_value=httpx.Response(status))
    async with build() as client:
        with pytest.raises(AuthError):
            await client.get(API)
    assert route.call_count == 1


@respx.mock
async def test_server_error_is_retried_then_succeeds() -> None:
    route = respx.get(f"{BASE}{API}").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text="recovered")]
    )
    async with build() as client:
        assert await client.get(API) == "recovered"
    assert route.call_count == 2


@respx.mock
async def test_rate_limit_is_retried_within_the_attempt_budget() -> None:
    route = respx.get(f"{BASE}{API}").mock(return_value=httpx.Response(429))
    async with build(scrape_max_attempts=2) as client:
        with pytest.raises(ScrapeError):
            await client.get(API)
    assert route.call_count == 2


@respx.mock
async def test_client_error_is_not_retried() -> None:
    route = respx.get(f"{BASE}{API}").mock(return_value=httpx.Response(404))
    async with build() as client:
        with pytest.raises(ScrapeError, match="404"):
            await client.get(API)
    assert route.call_count == 1


@respx.mock
async def test_transport_error_becomes_a_scrape_error() -> None:
    respx.get(f"{BASE}{API}").mock(side_effect=httpx.ConnectError("unreachable"))
    async with build(scrape_max_attempts=1) as client:
        with pytest.raises(ScrapeError, match="failed to fetch"):
            await client.get(API)


async def test_login_without_any_credential_is_a_config_error() -> None:
    client = HttpPortalClient(
        make_settings(
            scrape_delay_seconds=0.0,
            portal_username="svc",
            portal_password="",
            portal_password_hash="",
            portal_session_cookie="x",
        )
    )
    client._has_borrowed_session = False  # force the credential path
    with pytest.raises(ConfigError, match="PORTAL_PASSWORD_HASH"):
        await client.login()
    await client.aclose()


@respx.mock
async def test_debug_dump_writes_only_when_enabled(tmp_path: Path) -> None:
    respx.get(f"{BASE}{API}").mock(return_value=httpx.Response(200, json=envelope([{"id": "a"}])))
    client = build(
        scrape_debug=True, scrape_debug_dir=tmp_path / "dbg", portal_session_cookie="SESSIONID=abc"
    )
    await client.fetch_components()
    await client.aclose()
    assert '"id"' in (tmp_path / "dbg" / "renewlist-page-1.json").read_text()


@respx.mock
async def test_debug_dump_failure_does_not_break_the_run(tmp_path: Path) -> None:
    respx.get(f"{BASE}{API}").mock(return_value=httpx.Response(200, json=envelope([{"id": "a"}])))
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    client = build(
        scrape_debug=True, scrape_debug_dir=blocker / "sub", portal_session_cookie="SESSIONID=abc"
    )
    assert len(await client.fetch_components()) == 1
    await client.aclose()


@respx.mock
async def test_an_injected_client_is_not_closed_by_us() -> None:
    respx.get(f"{BASE}{API}").mock(return_value=httpx.Response(200, text="ok"))
    async with httpx.AsyncClient(base_url=BASE) as shared:
        portal = HttpPortalClient(make_settings(scrape_delay_seconds=0.0), client=shared)
        await portal.get(API)
        await portal.aclose()
        assert shared.is_closed is False


# --- fixture client ----------------------------------------------------------------


async def test_fixture_client_reads_a_local_file(tmp_path: Path) -> None:
    path = tmp_path / "renewlist.json"
    path.write_text(json.dumps(envelope([{"id": "a"}, {"id": "b"}])))
    client = FixturePortalClient(path)
    assert [i["id"] for i in await client.fetch_components()] == ["a", "b"]
    await client.aclose()


async def test_fixture_client_missing_file_is_a_scrape_error(tmp_path: Path) -> None:
    with pytest.raises(ScrapeError, match="cannot read fixture"):
        await FixturePortalClient(tmp_path / "nope.json").fetch_components()


async def test_fixture_client_rejects_non_json(tmp_path: Path) -> None:
    path = tmp_path / "renewlist.json"
    path.write_text("<html>not json</html>")
    with pytest.raises(ParseError):
        await FixturePortalClient(path).fetch_components()


# --- borrowed session and capture --------------------------------------------------


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        ('{"components": []}', "application/json", "json"),
        ("[1,2]", "text/plain", "json"),
        ("<!DOCTYPE html><html><body><table></table></body></html>", "text/html", "html"),
        (
            '<html><form action="/login"><input type="password"></form></html>',
            "text/html",
            "login_page",
        ),
        ("<html><form>please login here</form></html>", "text/html", "login_page"),
        ("plain words", "text/plain", "unknown"),
    ],
)
def test_classify_payload(body: str, content_type: str, expected: str) -> None:
    from tele_scraper.scraper.client import classify_payload

    assert classify_payload(content_type, body) == expected


@respx.mock
async def test_borrowed_cookie_skips_login_and_is_sent() -> None:
    """A session copied from a browser lets us reach the portal before login() is written."""
    route = respx.get(f"{BASE}{API}").mock(
        return_value=httpx.Response(200, text="<html><table>data</table></html>")
    )  # capture() does not parse, so any body is fine here
    client = build(portal_session_cookie="SESSIONID=abc123; csrf=xyz")
    body, _content_type, verdict = await client.capture()
    await client.aclose()

    assert verdict == "html"
    assert "data" in body
    assert route.calls[0].request.headers["cookie"] == "SESSIONID=abc123; csrf=xyz"


@respx.mock
async def test_borrowed_auth_header_is_sent() -> None:
    respx.get(f"{BASE}{API}").mock(return_value=httpx.Response(200, json={"items": []}))
    client = build(portal_auth_header="Bearer token-value-here")
    route = respx.get(f"{BASE}{API}")
    _body, _ct, verdict = await client.capture()
    await client.aclose()
    assert verdict == "json"
    assert route.calls[0].request.headers["authorization"] == "Bearer token-value-here"


@respx.mock
async def test_capture_logs_in_when_no_session_was_borrowed() -> None:
    login = respx.post(f"{BASE}/api/iam/v1/login").mock(
        return_value=httpx.Response(200, json={"status": 0, "data": {"token": "tok-abcdefgh"}})
    )
    respx.get(f"{BASE}{API}").mock(return_value=httpx.Response(200, json={"status": 0}))
    client = HttpPortalClient(
        make_settings(
            scrape_delay_seconds=0.0, portal_username="svc", portal_password_hash="b" * 64
        )
    )
    await client.capture()
    await client.aclose()
    assert login.call_count == 1


@respx.mock
async def test_expired_session_is_reported_as_an_auth_error() -> None:
    respx.get(f"{BASE}{API}").mock(return_value=httpx.Response(403))
    client = build(portal_session_cookie="SESSIONID=stale")
    with pytest.raises(AuthError, match="expired"):
        await client.capture()
    await client.aclose()


@respx.mock
async def test_capture_accepts_an_explicit_path() -> None:
    respx.get(f"{BASE}/api/v2/items").mock(return_value=httpx.Response(200, json=[]))
    client = build(portal_session_cookie="SESSIONID=abc")
    _body, _ct, verdict = await client.capture("/api/v2/items")
    await client.aclose()
    assert verdict == "json"


# --- TLS trust ---------------------------------------------------------------------

CA_BUNDLE = Path(__file__).resolve().parents[2] / "deploy" / "base" / "portal-ca.pem"


def test_no_extra_ca_uses_httpx_default_verification() -> None:
    from tele_scraper.scraper.client import build_ssl_context

    assert build_ssl_context(None) is True


def test_extra_ca_is_added_on_top_of_the_default_trust_store() -> None:
    """Additive, never replacing: the system roots must still be trusted."""
    import ssl

    from tele_scraper.scraper.client import build_ssl_context

    context = build_ssl_context(CA_BUNDLE)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    subjects = {
        name for cert in context.get_ca_certs() for tup in cert["subject"] for _, name in tup
    }
    assert any("GlobalSign GCC R46 EV TLS CA 2025" in s for s in subjects)


def test_missing_ca_bundle_fails_at_startup(tmp_path: Path) -> None:
    from tele_scraper.errors import ConfigError
    from tele_scraper.scraper.client import build_ssl_context

    with pytest.raises(ConfigError, match="PORTAL_CA_BUNDLE"):
        build_ssl_context(tmp_path / "absent.pem")


@respx.mock
async def test_capture_wraps_transport_errors_instead_of_raising_raw() -> None:
    """capture() bypassed get()'s error mapping, so a TLS failure escaped as a traceback."""
    respx.get(f"{BASE}{API}").mock(
        side_effect=httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    )
    client = build(portal_session_cookie="SESSIONID=abc")
    with pytest.raises(ScrapeError) as excinfo:
        await client.capture()
    await client.aclose()
    assert "PORTAL_CA_BUNDLE" in str(excinfo.value), "the error must say how to fix it"
    assert "do not disable verification" in str(excinfo.value)


@respx.mock
async def test_non_tls_transport_errors_do_not_get_the_certificate_hint() -> None:
    respx.get(f"{BASE}{API}").mock(side_effect=httpx.ConnectError("connection refused"))
    client = build(portal_session_cookie="SESSIONID=abc")
    with pytest.raises(ScrapeError) as excinfo:
        await client.capture()
    await client.aclose()
    assert "PORTAL_CA_BUNDLE" not in str(excinfo.value)


# --- pagination --------------------------------------------------------------------


@respx.mock
async def test_all_pages_are_collected() -> None:
    respx.get(f"{BASE}{API}").mock(
        side_effect=[
            httpx.Response(200, json=envelope([{"id": "a"}, {"id": "b"}], total=5)),
            httpx.Response(200, json=envelope([{"id": "c"}, {"id": "d"}], total=5)),
            httpx.Response(200, json=envelope([{"id": "e"}], total=5)),
        ]
    )
    client = build(portal_session_cookie="SESSIONID=abc", portal_page_size=2)
    items = await client.fetch_components()
    await client.aclose()
    assert [i["id"] for i in items] == ["a", "b", "c", "d", "e"]


@respx.mock
async def test_a_single_page_makes_a_single_request() -> None:
    route = respx.get(f"{BASE}{API}").mock(
        return_value=httpx.Response(200, json=envelope([{"id": "a"}], total=1))
    )
    client = build(portal_session_cookie="SESSIONID=abc")
    await client.fetch_components()
    await client.aclose()
    assert route.call_count == 1


@respx.mock
async def test_paging_query_matches_what_the_ui_sends() -> None:
    route = respx.get(f"{BASE}{API}").mock(
        return_value=httpx.Response(200, json=envelope([], total=0))
    )
    client = build(portal_session_cookie="SESSIONID=abc", portal_page_size=25)
    await client.fetch_components()
    await client.aclose()
    query = dict(route.calls[0].request.url.params)
    assert query == {"page": "1", "pageSize": "25", "sortField": "purchaseTime", "asc": "true"}


@respx.mock
async def test_an_empty_page_stops_paging_rather_than_looping() -> None:
    """A portal claiming a large total but returning nothing must not spin."""
    route = respx.get(f"{BASE}{API}").mock(
        return_value=httpx.Response(200, json=envelope([], total=999))
    )
    client = build(portal_session_cookie="SESSIONID=abc")
    assert await client.fetch_components() == []
    await client.aclose()
    assert route.call_count == 1


@respx.mock
async def test_pagination_is_hard_bounded() -> None:
    """A total that never gets satisfied hits PORTAL_MAX_PAGES instead of running forever."""
    route = respx.get(f"{BASE}{API}").mock(
        return_value=httpx.Response(200, json=envelope([{"id": "a"}], total=10_000))
    )
    client = build(portal_session_cookie="SESSIONID=abc", portal_max_pages=4)
    with pytest.raises(ScrapeError, match="PORTAL_MAX_PAGES"):
        await client.fetch_components()
    await client.aclose()
    assert route.call_count == 4


@respx.mock
async def test_non_json_body_is_a_parse_error_not_a_crash() -> None:
    respx.get(f"{BASE}{API}").mock(return_value=httpx.Response(200, text="<html>login</html>"))
    client = build(portal_session_cookie="SESSIONID=abc")
    with pytest.raises(ParseError, match="not valid JSON"):
        await client.fetch_components()
    await client.aclose()


@respx.mock
async def test_application_level_failure_is_surfaced() -> None:
    """status != 0 is an error even though the HTTP status was 200."""
    respx.get(f"{BASE}{API}").mock(
        return_value=httpx.Response(200, json={"status": 401, "resMsg": "token expired"})
    )
    client = build(portal_session_cookie="SESSIONID=abc")
    with pytest.raises(ParseError, match="token expired"):
        await client.fetch_components()
    await client.aclose()


# --- login -------------------------------------------------------------------------

LOGIN = "/api/iam/v1/login"
#: Deliberately not shaped like a real JWT, so credential scans over this repo stay quiet.
JWT = "FAKE-TEST-TOKEN-not-a-jwt-0123456789"


def login_ok(token: str = JWT) -> dict:
    return {"status": 0, "resMsg": "success", "data": {"id": 1, "token": token}}


def creds(**overrides: object) -> HttpPortalClient:
    base: dict[str, object] = {
        "scrape_delay_seconds": 0.0,
        "portal_username": "svc-account",
        "portal_password_hash": "a" * 64,
    }
    base.update(overrides)
    return HttpPortalClient(make_settings(**base))


@respx.mock
async def test_login_posts_username_and_digest() -> None:
    route = respx.post(f"{BASE}{LOGIN}").mock(return_value=httpx.Response(200, json=login_ok()))
    client = creds()
    await client.login()
    await client.aclose()

    body = json.loads(route.calls[0].request.content)
    assert body == {"username": "svc-account", "password": "a" * 64}


@respx.mock
async def test_a_supplied_digest_is_sent_verbatim_and_never_rehashed() -> None:
    """The portal hashes in the browser, so the digest IS the credential."""
    route = respx.post(f"{BASE}{LOGIN}").mock(return_value=httpx.Response(200, json=login_ok()))
    client = creds(portal_password_hash="deadbeef" * 8)
    await client.login()
    await client.aclose()
    assert json.loads(route.calls[0].request.content)["password"] == "deadbeef" * 8


@respx.mock
async def test_a_plaintext_password_is_hashed_before_it_leaves_the_process() -> None:
    import hashlib

    route = respx.post(f"{BASE}{LOGIN}").mock(return_value=httpx.Response(200, json=login_ok()))
    client = creds(portal_password_hash="", portal_password="s3cret-value")
    await client.login()
    await client.aclose()

    sent = json.loads(route.calls[0].request.content)["password"]
    assert sent == hashlib.sha256(b"s3cret-value").hexdigest()
    assert "s3cret-value" not in route.calls[0].request.content.decode()


@respx.mock
async def test_the_token_is_carried_on_later_requests() -> None:
    respx.post(f"{BASE}{LOGIN}").mock(return_value=httpx.Response(200, json=login_ok()))
    listing = respx.get(f"{BASE}{API}").mock(
        return_value=httpx.Response(200, json=envelope([{"id": "a"}], total=1))
    )
    client = creds()
    await client.fetch_components()
    await client.aclose()
    assert listing.calls[0].request.headers["token"] == JWT


@respx.mock
async def test_a_wrong_password_arrives_as_http_200_and_is_an_auth_error() -> None:
    """The trap: the portal signals a bad credential in the envelope, not the status code."""
    route = respx.post(f"{BASE}{LOGIN}").mock(
        return_value=httpx.Response(200, json={"status": 401, "resMsg": "wrong password"})
    )
    client = creds()
    with pytest.raises(AuthError, match="wrong password"):
        await client.login()
    await client.aclose()
    assert route.call_count == 1, "a credential failure must never be retried (§7.3)"


@respx.mock
async def test_a_login_with_no_token_is_an_auth_error() -> None:
    respx.post(f"{BASE}{LOGIN}").mock(
        return_value=httpx.Response(200, json={"status": 0, "data": {"id": 1}})
    )
    client = creds()
    with pytest.raises(AuthError, match="no token"):
        await client.login()
    await client.aclose()


@respx.mock
async def test_a_401_on_login_is_not_retried() -> None:
    route = respx.post(f"{BASE}{LOGIN}").mock(return_value=httpx.Response(401))
    client = creds()
    with pytest.raises(AuthError):
        await client.login()
    await client.aclose()
    assert route.call_count == 1


@respx.mock
async def test_each_run_logs_in_again_because_the_token_is_short_lived() -> None:
    """The live token lives 2 hours, so a cached session would go stale in a long-run pod."""
    login = respx.post(f"{BASE}{LOGIN}").mock(return_value=httpx.Response(200, json=login_ok()))
    respx.get(f"{BASE}{API}").mock(
        return_value=httpx.Response(200, json=envelope([{"id": "a"}], total=1))
    )
    client = creds()
    await client.fetch_components()
    await client.fetch_components()
    await client.aclose()
    assert login.call_count == 2


@respx.mock
async def test_a_borrowed_session_skips_login_entirely() -> None:
    login = respx.post(f"{BASE}{LOGIN}").mock(return_value=httpx.Response(200, json=login_ok()))
    respx.get(f"{BASE}{API}").mock(
        return_value=httpx.Response(200, json=envelope([{"id": "a"}], total=1))
    )
    client = build(portal_session_cookie="SESSIONID=abc")
    await client.fetch_components()
    await client.aclose()
    assert login.call_count == 0


@respx.mock
async def test_the_token_is_registered_for_redaction() -> None:
    from tele_scraper.observability import logging as logging_module

    respx.post(f"{BASE}{LOGIN}").mock(return_value=httpx.Response(200, json=login_ok()))
    client = creds()
    await client.login()
    await client.aclose()

    scrubbed = logging_module.redact_processor(None, "info", {"event": f"used {JWT}"})
    assert JWT not in str(scrubbed["event"])

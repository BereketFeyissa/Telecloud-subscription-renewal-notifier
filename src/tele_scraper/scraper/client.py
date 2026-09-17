"""Portal transport.

Everything here is specified and implemented: one authenticated session per run, mandatory
timeouts, bounded retries on transient faults only, a politeness delay and a concurrency cap
(CLAUDE.md §7).

What is *not* implemented is the login flow itself, because the portal's authentication is an
open question (§2 open 1) and §0 forbids inventing it. :meth:`HttpPortalClient.login` therefore
fails with the exact list of what is needed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, runtime_checkable

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from tele_scraper.config import Settings
from tele_scraper.errors import AuthError, ConfigError, ParseError, ScrapeError
from tele_scraper.models import AccountInfo
from tele_scraper.observability.logging import get_logger, register_secret
from tele_scraper.scraper.parser import parse_account, parse_envelope, parse_login_token

log = get_logger(__name__)

#: What we need from the operator before the login flow can be written.
LOGIN_QUESTIONS = (
    "the login URL and HTTP method",
    "whether it is a form POST, a JSON API, or an SSO/OAuth redirect",
    "the exact field names for username and password",
    "whether a CSRF token or hidden field must be read from the login page first",
    "how the session is carried afterwards (cookie name, bearer token, custom header)",
    "how an expired session presents itself (redirect to login, 401, or an HTML error page)",
)


def build_ssl_context(extra_ca: Path | None) -> ssl.SSLContext | bool:
    """Build the TLS context, optionally trusting extra CA certificates.

    Returns ``True`` (httpx's default verification) when no extra CA is configured.

    When one is, the default trust store is loaded first and the extra certificates are added
    on top. This is what fixes a portal that serves its leaf certificate without the
    intermediate: browsers paper over that by caching intermediates or fetching them via AIA,
    but Python does neither. Supplying the intermediate keeps full chain verification intact -
    unlike disabling verification, which this project deliberately offers no way to do.

    Raises:
        ConfigError: if the bundle is missing or unreadable. Failing at startup beats
            discovering it mid-run.
    """
    if extra_ca is None:
        return True
    context = ssl.create_default_context()
    try:
        context.load_verify_locations(cafile=str(extra_ca))
    except (OSError, ssl.SSLError) as exc:
        raise ConfigError(f"PORTAL_CA_BUNDLE: cannot load {extra_ca}: {exc}") from exc
    return context


def classify_payload(content_type: str, body: str) -> str:
    """Best-effort guess at what the portal just returned.

    A diagnostic hint for the capture tool only - it never drives parsing. Answering
    "is this JSON, HTML, or actually the login page?" is the observation that decides the
    scraper's design (CLAUDE.md §2 open 4), and it is far easier to observe than to recall.
    """
    lowered_type = content_type.lower()
    head = body[:4000].lower()

    if "json" in lowered_type or body.lstrip()[:1] in "[{":
        return "json"
    has_password_field = 'type="password"' in head or "type='password'" in head
    if has_password_field or ("<form" in head and "login" in head):
        return "login_page"
    if "<html" in head or "<!doctype html" in head:
        if "login" in head and "password" in head:
            return "login_page"
        return "html"
    return "unknown"


@runtime_checkable
class PortalClient(Protocol):
    """Fetches the component listing."""

    #: Credential lifetimes from the last login, when there was one.
    account: AccountInfo | None

    async def fetch_components(self) -> list[dict[str, Any]]:
        """Return every raw listing entry, across all pages."""
        ...

    async def aclose(self) -> None: ...


class HttpPortalClient:
    """HTTP transport against the portal.

    A single session is established once per run and reused; we do not re-authenticate per
    component (CLAUDE.md §7.1).
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        headers = {"User-Agent": settings.scrape_user_agent}
        cookie = settings.portal_session_cookie.get_secret_value()
        auth = settings.portal_auth_header.get_secret_value()
        if cookie:
            headers["Cookie"] = cookie
        if auth:
            headers["Authorization"] = auth
        self._client = client or httpx.AsyncClient(
            base_url=settings.portal_base_url,
            timeout=httpx.Timeout(
                settings.scrape_timeout_seconds, connect=settings.scrape_timeout_seconds
            ),
            headers=headers,
            follow_redirects=True,
            verify=build_ssl_context(settings.portal_ca_bundle),
        )
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(settings.scrape_concurrency)
        #: A borrowed session means we are already authenticated and must not call login().
        self._has_borrowed_session = bool(cookie or auth)
        self._authenticated = self._has_borrowed_session
        #: Credential lifetimes from the last login. None when a session was borrowed, since
        #: no login happened and the portal told us nothing about the account.
        self.account: AccountInfo | None = None

    def _password_digest(self) -> str:
        """The value to put in the login payload's ``password`` field.

        The portal hashes the password in the browser and never receives the plaintext, so a
        pre-computed digest is preferred: this service then never handles the real password and
        makes no assumption about the algorithm.

        Raises:
            ConfigError: if neither a digest nor a plaintext password is configured.
        """
        digest = self._settings.portal_password_hash.get_secret_value().strip()
        if digest:
            return digest

        plaintext = self._settings.portal_password.get_secret_value()
        if not plaintext:
            raise ConfigError("set PORTAL_PASSWORD_HASH (preferred) or PORTAL_PASSWORD to log in")

        algo = self._settings.portal_password_hash_algo
        if algo == "none":
            return plaintext
        log.warning(
            "portal.hashing_plaintext",
            algorithm=algo,
            detail="hashing the plaintext locally; the portal's exact scheme is unconfirmed. "
            "Prefer PORTAL_PASSWORD_HASH, which replays the digest the browser sends",
        )
        return hashlib.new(algo, plaintext.encode()).hexdigest()

    async def login(self) -> None:
        """Obtain a session token.

        ``POST /api/iam/v1/login`` with ``{"username", "password"}``, where ``password`` is the
        client-side digest. The reply uses the same envelope as every other endpoint, so a bad
        credential arrives as **HTTP 200 with a non-zero status**, not a 401 - it is turned into
        :class:`AuthError` here and never retried (CLAUDE.md §7.3).

        The token observed on the live portal lives two hours, which is why a fresh one is
        fetched per run rather than cached across runs (§7.1).

        A no-op when a session was borrowed from a browser.
        """
        if self._has_borrowed_session:
            log.warning(
                "portal.borrowed_session",
                detail="using a session copied from a browser; it will expire. Not for "
                "production (CLAUDE.md §2 open 1)",
            )
            return

        payload = {
            "username": self._settings.portal_username,
            "password": self._password_digest(),
        }
        try:
            response = await self._client.post(
                self._settings.portal_login_path,
                json=payload,
                timeout=self._settings.scrape_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ScrapeError(_explain_transport_error("login", exc)) from exc

        if response.status_code in (401, 403):
            raise AuthError(f"portal rejected the credentials: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ScrapeError(f"login failed: HTTP {response.status_code}")

        try:
            body = response.json()
            token = parse_login_token(body)
        except json.JSONDecodeError as exc:
            raise ScrapeError(f"login response was not JSON: {exc}") from exc
        except ParseError as exc:
            # A wrong or expired password lands here, not on a 4xx. Treated as a credential
            # incident so it is never retried against the account.
            raise AuthError(str(exc)) from exc

        register_secret(token)
        self._client.headers[self._settings.portal_token_header] = token
        self.account = parse_account(body)
        log.info("portal.logged_in", username=self._settings.portal_username)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> str:
        """Fetch one path with retries, politeness delay, and a hard timeout.

        Retries only transient faults. A 401 or 403 is never retried - that is a credential
        incident, and hammering it risks locking the account (CLAUDE.md §7.3).
        """

        async def _once() -> str:
            async with self._semaphore:
                if self._settings.scrape_delay_seconds:
                    await asyncio.sleep(self._settings.scrape_delay_seconds)
                response = await self._client.get(path, params=params)
            if response.status_code in (401, 403):
                raise AuthError(
                    f"portal rejected our credentials on {path}: HTTP {response.status_code}"
                )
            if response.status_code == 429 or response.status_code >= 500:
                raise _TransientHttpError(f"{path}: HTTP {response.status_code}")
            if response.status_code >= 400:
                raise ScrapeError(f"{path}: HTTP {response.status_code}")
            return response.text

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._settings.scrape_max_attempts),
                wait=wait_exponential_jitter(initial=1, max=30),
                retry=retry_if_exception_type(
                    (httpx.TimeoutException, httpx.TransportError, _TransientHttpError)
                ),
                reraise=True,
            ):
                with attempt:
                    return await _once()
        except AuthError:
            raise
        except (httpx.HTTPError, _TransientHttpError) as exc:
            raise ScrapeError(f"failed to fetch {path}: {exc}") from exc
        raise AssertionError("unreachable: AsyncRetrying always returns or raises")

    async def fetch_components(self) -> list[dict[str, Any]]:
        """Log in if needed, then page through the whole component listing.

        Pagination is bounded twice over: it stops once ``total`` entries are collected, and
        again at ``PORTAL_MAX_PAGES``, so a portal reporting a nonsensical total cannot spin
        the loop forever.
        """
        # One login per run, not one per process: the portal's token lives two hours, so a
        # cached session would go stale inside a long-running Deployment (CLAUDE.md §7.1).
        await self.login()
        self._authenticated = True

        collected: list[dict[str, Any]] = []
        page = 1
        while page <= self._settings.portal_max_pages:
            raw = await self.get(
                self._settings.portal_components_path,
                params={
                    "page": page,
                    "pageSize": self._settings.portal_page_size,
                    "sortField": "purchaseTime",
                    "asc": "true",
                },
            )
            self._maybe_dump(raw, page)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ParseError(f"page {page} was not valid JSON: {exc}") from exc

            parsed = parse_envelope(payload)
            collected.extend(parsed.items)
            log.info("scrape.page_fetched", page=page, items=len(parsed.items), total=parsed.total)

            if not parsed.items or len(collected) >= parsed.total:
                return collected
            page += 1

        raise ScrapeError(
            f"pagination exceeded PORTAL_MAX_PAGES ({self._settings.portal_max_pages}); "
            "the portal may be reporting an incorrect total"
        )

    async def capture(self, path: str | None = None) -> tuple[str, str, str]:
        """Fetch one page and report what came back, without parsing it.

        Returns ``(body, content_type, verdict)``. Used by ``--capture`` to produce the
        fixture the parser will be written against, and to answer what the portal actually
        serves - rather than guessing (CLAUDE.md §0).
        """
        target = path or self._settings.portal_components_path
        if not self._authenticated:
            await self.login()
            self._authenticated = True
        try:
            async with self._semaphore:
                response = await self._client.get(target)
        except httpx.HTTPError as exc:
            raise ScrapeError(_explain_transport_error(target, exc)) from exc
        if response.status_code in (401, 403):
            raise AuthError(
                f"portal rejected the supplied session on {target}: "
                f"HTTP {response.status_code}. The cookie has probably expired - copy a "
                "fresh one from the browser."
            )
        if response.status_code >= 400:
            raise ScrapeError(f"{target}: HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "")
        return response.text, content_type, classify_payload(content_type, response.text)

    def _maybe_dump(self, body: str, page: int) -> None:
        """Write a raw response to disk only when explicitly enabled.

        Raw payloads are never logged and never committed (CLAUDE.md §7.9).
        """
        if not self._settings.scrape_debug:
            return
        directory = self._settings.scrape_debug_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"renewlist-page-{page}.json").write_text(body, encoding="utf-8")
        except OSError as exc:
            log.warning("scrape.debug_dump_failed", error=str(exc))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> HttpPortalClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


class FixturePortalClient:
    """Reads a saved response from a local file instead of the network.

    Lets the whole pipeline be exercised deterministically, with no portal and no credentials.
    Never used in production.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self.account: AccountInfo | None = None

    async def fetch_components(self) -> list[dict[str, Any]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ScrapeError(f"cannot read fixture {self._path}: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ParseError(f"fixture {self._path} is not valid JSON: {exc}") from exc
        return parse_envelope(payload).items

    async def aclose(self) -> None:
        return None


def _explain_transport_error(target: str, exc: Exception) -> str:
    """Turn a transport failure into something actionable rather than a stack trace."""
    detail = str(exc)
    message = f"failed to fetch {target}: {detail}"
    if "CERTIFICATE_VERIFY_FAILED" in detail:
        message += (
            " - the portal's TLS chain could not be verified. This usually means it serves "
            "its certificate without the intermediate CA. Fetch the intermediate from the "
            "certificate's AIA 'CA Issuers' URL and point PORTAL_CA_BUNDLE at it; do not "
            "disable verification."
        )
    return message


class _TransientHttpError(Exception):
    """Internal marker for a retryable HTTP response."""

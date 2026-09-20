"""Application settings.

This is the ONLY module permitted to read ``os.environ`` (CLAUDE.md §9). Everything else
receives a :class:`Settings` instance by injection. Invalid configuration fails at startup with
a message naming the offending variable.
"""

from __future__ import annotations

import json
import os
from datetime import time, timedelta
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from tele_scraper.domain.rules import parse_thresholds
from tele_scraper.errors import ConfigError
from tele_scraper.models import NOTIFIABLE_STATUSES, ChannelTarget, Status

#: Channels the router knows how to build. ``sms`` is declared but unimplemented until a
#: provider is chosen (CLAUDE.md §2 open 3).
KNOWN_CHANNELS: frozenset[str] = frozenset({"email", "telegram", "slack", "discord", "sms"})


class QuietHours(BaseModel):
    """A local-time window during which non-critical notifications are held."""

    model_config = ConfigDict(frozen=True)

    start: time
    end: time
    tz: str

    @field_validator("tz")
    @classmethod
    def _known_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v


class ChannelSpec(BaseModel):
    """One channel for one recipient.

    The address is given either inline (``address``) or, for anything secret such as a webhook
    URL, indirectly through an environment variable (``address_env``) so it can come from a
    Kubernetes Secret rather than a ConfigMap (CLAUDE.md §8.6, §14.6).
    """

    model_config = ConfigDict(frozen=True)

    channel: str
    address: str | None = None
    address_env: str | None = None

    @field_validator("channel")
    @classmethod
    def _known_channel(cls, v: str) -> str:
        channel = v.strip().lower()
        if channel not in KNOWN_CHANNELS:
            raise ValueError(
                f"unknown channel {v!r}; known channels: {', '.join(sorted(KNOWN_CHANNELS))}"
            )
        return channel

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ChannelSpec:
        if (self.address is None) == (self.address_env is None):
            raise ValueError(
                f"channel {self.channel!r} must set exactly one of 'address' or 'address_env'"
            )
        return self

    def resolve(self) -> ChannelTarget:
        """Resolve the address, reading the environment when indirected.

        Raises:
            ConfigError: if ``address_env`` names a variable that is unset or blank.
        """
        if self.address is not None:
            return ChannelTarget(channel=self.channel, address=self.address)
        assert self.address_env is not None
        value = os.environ.get(self.address_env, "").strip()
        if not value:
            raise ConfigError(
                f"channel {self.channel!r} references address_env "
                f"{self.address_env!r}, which is unset or empty"
            )
        return ChannelTarget(channel=self.channel, address=value)


class Route(BaseModel):
    """Which statuses of which components reach one recipient, on which channels."""

    model_config = ConfigDict(frozen=True)

    recipient: str = Field(min_length=1)
    channels: tuple[ChannelSpec, ...] = Field(min_length=1)
    statuses: tuple[Status, ...] = tuple(sorted(NOTIFIABLE_STATUSES))
    components: tuple[str, ...] = ("*",)
    locale: str = "en"
    quiet_hours: QuietHours | None = None
    #: ``detailed`` sends one message per component; ``summary`` sends one per status group.
    #: Defaults to detailed so existing routes keep behaving exactly as before.
    mode: Literal["detailed", "summary"] = "detailed"
    #: What the Confirm button on a summary message acknowledges.
    #: ``components`` - each item listed, so the next digest only shows what is still
    #: outstanding. ``digest`` - the set as a unit, re-sending in full if it changes.
    #: ``none`` - informational, no button, repeats every run.
    summary_ack: Literal["components", "digest", "none"] = "components"

    @field_validator("statuses")
    @classmethod
    def _not_active(cls, v: tuple[Status, ...]) -> tuple[Status, ...]:
        if Status.ACTIVE in v:
            raise ValueError(
                "routing on ACTIVE would notify on every healthy component on every run; "
                "if you want periodic health digests, ask for them explicitly"
            )
        return v

    def targets(self) -> tuple[ChannelTarget, ...]:
        return tuple(spec.resolve() for spec in self.channels)


class RoutingTable(BaseModel):
    """The whole recipient x channel matrix, loaded from config, never from code."""

    model_config = ConfigDict(frozen=True)

    routes: tuple[Route, ...] = ()

    @model_validator(mode="after")
    def _unique_recipients(self) -> RoutingTable:
        seen = [r.recipient for r in self.routes]
        duplicates = {name for name in seen if seen.count(name) > 1}
        if duplicates:
            raise ValueError(f"duplicate recipient(s) in routing table: {sorted(duplicates)}")
        return self


class Settings(BaseSettings):
    """Runtime configuration, read once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- portal -----------------------------------------------------------------
    portal_base_url: str
    portal_username: str = ""
    portal_password: SecretStr = SecretStr("")
    #: The already-hashed password exactly as the browser sends it. Preferred: the portal
    #: hashes client-side, so supplying the digest means this service never handles the
    #: plaintext at all, and no assumption is made about the algorithm.
    portal_password_hash: SecretStr = SecretStr("")
    #: Used only when a plaintext password is supplied. The digest length observed on the live
    #: portal is consistent with SHA-256, but that has not been confirmed against the login
    #: page's source - prefer PORTAL_PASSWORD_HASH.
    portal_password_hash_algo: Literal["sha256", "sha512", "md5", "none"] = "sha256"  # noqa: S105
    #: Header carrying the session token on subsequent requests. Observed as ``token``.
    portal_token_header: str = "token"  # noqa: S105 - a header name, not a secret
    #: Confirmed 2026-09-17 from DevTools: POST JSON {"username", "password"} where the
    #: password is hashed client-side by the browser, not sent in plaintext.
    portal_login_path: str = "/api/iam/v1/login"
    #: A session copied from a logged-in browser, for capture and live testing before the
    #: real auth flow is known (CLAUDE.md §2 open 1). Supply the FULL value of the request's
    #: ``Cookie`` header. Testing only: browser sessions expire, so this is never the
    #: production path.
    portal_session_cookie: SecretStr = SecretStr("")
    #: Alternative to the cookie for token-based portals: the full ``Authorization`` header
    #: value, e.g. ``Bearer eyJ...``.
    portal_auth_header: SecretStr = SecretStr("")
    portal_components_path: str = "/api/cbp/thirdapi/v1/renewal_service/renewlist"
    #: Items per request. The UI uses 10; a larger page means fewer round trips while
    #: staying well inside the politeness budget (§7.4).
    portal_page_size: Annotated[int, Field(ge=1, le=500)] = 50
    #: Hard bound on pagination so a portal reporting a bogus total cannot loop forever.
    portal_max_pages: Annotated[int, Field(ge=1, le=1000)] = 50
    #: Portal strings meaning suspended/blocked/inactive. Empty until the vocabulary is known
    #: (CLAUDE.md §2 open 2); while empty, nothing is ever classified SUSPENDED.
    portal_suspended_tokens: str = ""
    #: Extra CA certificates to ADD to the default trust store - typically an intermediate the
    #: portal fails to send. Additive: it never replaces the system roots, and verification
    #: stays fully enabled. There is deliberately no "disable TLS verification" setting.
    portal_ca_bundle: Path | None = None

    # --- scraping ---------------------------------------------------------------
    scrape_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0
    scrape_delay_seconds: Annotated[float, Field(ge=0, le=60)] = 1.0
    scrape_concurrency: Annotated[int, Field(ge=1, le=16)] = 2
    scrape_max_attempts: Annotated[int, Field(ge=1, le=10)] = 3
    scrape_user_agent: str = "tele-scraper/0.1 (+ops contact configured via SCRAPE_USER_AGENT)"
    scrape_debug: bool = False
    scrape_debug_dir: Path = Path("/tmp/tele-scraper-debug")  # noqa: S108

    # --- evaluation -------------------------------------------------------------
    app_timezone: str = "Africa/Addis_Ababa"
    warn_thresholds: str = "14d,7d,3d,1d,12h"
    #: Wider ladder for our own credentials: changing a portal password needs coordination and
    #: a redeploy of the Secret, so it wants more lead time than a renewable component.
    credential_warn_thresholds: str = "30d,14d,7d,3d,1d"
    #: Watch the credential lifetimes the login response returns.
    monitor_credentials: bool = True
    #: Also watch the account's own expiry, not just the password's.
    monitor_account_expiry: bool = True

    # --- scheduling -------------------------------------------------------------
    run_interval_seconds: Annotated[int, Field(ge=30, le=86_400)] = 3600
    run_jitter_seconds: Annotated[int, Field(ge=0, le=3600)] = 30
    run_timeout_seconds: Annotated[int, Field(ge=30, le=7200)] = 900
    #: After a failed run the scheduler retries on this delay rather than sleeping the whole
    #: interval. A pod that fails its first cycle - a startup race with the CNI will do it -
    #: would otherwise be blind until the next scheduled run.
    retry_backoff_seconds: Annotated[int, Field(ge=5, le=3600)] = 60
    #: The backoff doubles per consecutive failure, capped here, so a portal outage does not
    #: turn into a retry storm.
    retry_backoff_max_seconds: Annotated[int, Field(ge=30, le=86_400)] = 900

    # --- notification -----------------------------------------------------------
    notify_enabled: bool = False
    dry_run: bool = False
    notify_routes_json: str = ""
    notify_routes_file: Path | None = None
    notify_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 15.0
    #: An unconfirmed alert repeats every run. Acknowledging it silences that exact state.
    require_acknowledgement: bool = True
    #: Notice when a component the portal used to list disappears. A component that silently
    #: stops being watched is the same class of failure as a scraper that silently stops
    #: running (CLAUDE.md §6).
    detect_missing_components: bool = True
    #: How long an acknowledgement holds before the alert returns, so a forgotten problem
    #: resurfaces on its own. An ack is a snooze, never permanent silence.
    ack_ttl_days: Annotated[int, Field(ge=1, le=365)] = 7
    #: Poll Telegram for Confirm-button presses. Long polling: outbound only, no public
    #: endpoint, no ingress.
    telegram_ack_enabled: bool = True
    telegram_poll_timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 25
    default_locale: str = "en"

    smtp_host: str = ""
    smtp_port: Annotated[int, Field(ge=1, le=65535)] = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    #: How TLS is established. ``auto`` picks implicit TLS on the conventional SMTPS port
    #: 465 and STARTTLS elsewhere, which is what almost every provider expects. The two are
    #: not interchangeable: STARTTLS on 465 hangs, because the server expects TLS immediately.
    smtp_tls: Literal["auto", "ssl", "starttls", "none"] = "auto"

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_api_base: str = "https://api.telegram.org"

    # --- state ------------------------------------------------------------------
    state_backend: Literal["sqlite"] = "sqlite"
    state_dsn: Path = Path("/var/lib/tele-scraper/state.sqlite3")
    state_retention_days: Annotated[int, Field(ge=1, le=365)] = 30

    # --- observability ----------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    metrics_port: Annotated[int, Field(ge=1, le=65535)] = 9100
    metrics_host: str = "0.0.0.0"  # noqa: S104

    @model_validator(mode="after")
    def _some_way_to_authenticate(self) -> Settings:
        """Require either real credentials or a borrowed browser session.

        Credentials are consumed only by ``login()``. A session supplied through
        ``PORTAL_SESSION_COOKIE`` / ``PORTAL_AUTH_HEADER`` replaces them outright, which is what
        makes ``--capture`` usable before the auth flow is known (CLAUDE.md §2 open 1).
        """
        has_secret = bool(
            self.portal_password.get_secret_value() or self.portal_password_hash.get_secret_value()
        )
        has_credentials = bool(self.portal_username and has_secret)
        has_session = bool(
            self.portal_session_cookie.get_secret_value()
            or self.portal_auth_header.get_secret_value()
        )
        if not has_credentials and not has_session:
            raise ValueError(
                "no way to authenticate to the portal: set PORTAL_USERNAME with either "
                "PORTAL_PASSWORD_HASH (preferred - the digest the browser sends) or "
                "PORTAL_PASSWORD, or paste a browser session into PORTAL_SESSION_COOKIE "
                "(or PORTAL_AUTH_HEADER) for capture and live testing"
            )
        return self

    @field_validator("notify_routes_file", "portal_ca_bundle", mode="before")
    @classmethod
    def _blank_path_is_unset(cls, v: object) -> object:
        """Treat an empty env var as unset.

        ``NOTIFY_ROUTES_FILE=`` in a .env or a ConfigMap otherwise becomes ``Path(".")``, and
        the routing loader then fails trying to read a directory.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("app_timezone")
    @classmethod
    def _known_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"APP_TIMEZONE: unknown timezone {v!r}") from exc
        return v

    @field_validator("portal_base_url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("PORTAL_BASE_URL must start with http:// or https://")
        return v.rstrip("/")

    @cached_property
    def thresholds(self) -> tuple[timedelta, ...]:
        """Warning ladder, widest first."""
        try:
            return parse_thresholds(self.warn_thresholds)
        except ValueError as exc:
            raise ConfigError(f"WARN_THRESHOLDS: {exc}") from exc

    @cached_property
    def credential_thresholds(self) -> tuple[timedelta, ...]:
        """Warning ladder for credential expiry, widest first."""
        try:
            return parse_thresholds(self.credential_warn_thresholds)
        except ValueError as exc:
            raise ConfigError(f"CREDENTIAL_WARN_THRESHOLDS: {exc}") from exc

    @cached_property
    def suspended_tokens(self) -> frozenset[str]:
        """Normalized portal strings that mean suspended."""
        return frozenset(
            " ".join(t.strip().lower().split())
            for t in self.portal_suspended_tokens.split(",")
            if t.strip()
        )

    @cached_property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    @cached_property
    def routing(self) -> RoutingTable:
        """Parse the routing table from a mounted file or an inline JSON string.

        A file always wins over the inline value, because mounted ConfigMaps are the intended
        production path.
        """
        raw: str
        if self.notify_routes_file is not None:
            try:
                raw = self.notify_routes_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(
                    f"NOTIFY_ROUTES_FILE: cannot read {self.notify_routes_file}: {exc}"
                ) from exc
        elif self.notify_routes_json.strip():
            raw = self.notify_routes_json
        else:
            raise ConfigError(
                "no routing configured: set NOTIFY_ROUTES_FILE or NOTIFY_ROUTES_JSON "
                "(CLAUDE.md §8 - recipients and channels are configuration, not code)"
            )

        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"routing table is not valid JSON: {exc}") from exc
        if isinstance(payload, list):
            payload = {"routes": payload}
        try:
            return RoutingTable.model_validate(payload)
        except ValidationError as exc:
            raise ConfigError(f"routing table is invalid: {exc}") from exc

    def required_channels(self) -> frozenset[str]:
        """Every channel name referenced by the routing table."""
        return frozenset(spec.channel for route in self.routing.routes for spec in route.channels)


def _export_dotenv_extras(path: Path = Path(".env")) -> None:
    """Export ``.env`` keys that are not Settings fields into the process environment.

    pydantic-settings maps ``.env`` entries onto declared fields only. Keys referenced
    indirectly by the routing table through ``address_env`` are not fields, so without this
    they would be invisible locally even though they work in the cluster, where a Secret
    supplies them as real environment variables. Existing environment variables always win.

    Local-development convenience only; it is a no-op when there is no ``.env``.
    """
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        key = key.strip()
        if key and value:
            os.environ.setdefault(key, value)


def load_settings(*, require_notifications: bool = True) -> Settings:
    """Build settings, converting any validation failure into a fatal :class:`ConfigError`.

    Eagerly touches the derived values so a bad ladder, timezone, or routing table is caught at
    startup rather than mid-run (CLAUDE.md §9).

    Args:
        require_notifications: Validate the routing table too. Set False for read-only
            diagnostics such as ``--capture``, which never build the notification stack and so
            must not demand a routing table to run.
    """
    _export_dotenv_extras()
    try:
        settings = Settings()  # values come from the environment and .env
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc

    _ = settings.thresholds
    _ = settings.credential_thresholds
    _ = settings.suspended_tokens
    _ = settings.timezone
    if require_notifications:
        _ = settings.routing
        for route in settings.routing.routes:
            route.targets()
    return settings

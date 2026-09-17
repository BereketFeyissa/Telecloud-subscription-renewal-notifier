"""Structured logging.

JSON to stdout, one event per line (CLAUDE.md §12). Secret redaction is enforced here rather
than left to call sites, because a single careless ``log.info(token=...)`` would otherwise leak
a credential into the cluster's log store (CLAUDE.md §8.6).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

REDACTED = "***REDACTED***"

#: Substrings that mark a key as secret-bearing. Matched case-insensitively.
_SECRET_KEY_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "cookie",
    "session",
    "credential",
    "webhook",
    "dsn",
)

#: Literal secret values registered at startup, redacted wherever they appear in a message.
_SECRET_VALUES: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a literal secret so it is scrubbed from log output anywhere it appears.

    Short values are ignored: redacting a 3-character string would mangle unrelated text.
    """
    if value and len(value) >= 8:
        _SECRET_VALUES.add(value)


def reset_secrets() -> None:
    """Clear registered secrets. Tests only."""
    _SECRET_VALUES.clear()


def _scrub_text(text: str) -> str:
    for secret in _SECRET_VALUES:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        return {k: _scrub_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_value(v) for v in value)
    return value


def redact_processor(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Redact secret-looking keys and any registered secret literal."""
    out: structlog.types.EventDict = {}
    for key, value in event_dict.items():
        lowered = key.lower()
        if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
            out[key] = REDACTED
        else:
            out[key] = _scrub_value(value)
    return out


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog and the stdlib root logger to emit one JSON line per event."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)

    # Chatty third-party loggers emit plain text, which would break the "one JSON event per
    # line" contract (CLAUDE.md §12). httpx logs every request at INFO; httpcore is noisier
    # still at DEBUG.
    for noisy in ("httpx", "httpcore", "hpack", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for ``name``."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_run(run_id: str) -> None:
    """Bind ``run_id`` to every subsequent log line in this context (CLAUDE.md §12)."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(run_id=run_id)

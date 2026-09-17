"""SMTP email channel.

Uses the standard library rather than pulling in an async SMTP dependency; the blocking call is
dispatched to a worker thread so the event loop is never blocked (CLAUDE.md §4 - no new
dependencies without approval).
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from typing import Literal

from tele_scraper.models import DeliveryResult, NotificationEvent
from tele_scraper.notify.base import MessageRenderer
from tele_scraper.observability.logging import get_logger

log = get_logger(__name__)

#: The conventional implicit-TLS (SMTPS) port.
SMTPS_PORT = 465


class EmailNotifier:
    """Sends plain-text mail over SMTP."""

    name = "email"

    def __init__(
        self,
        renderer: MessageRenderer,
        *,
        host: str,
        port: int,
        sender: str,
        username: str = "",
        password: str = "",
        tls: Literal["auto", "ssl", "starttls", "none"] = "auto",
        timeout: float = 15.0,
    ) -> None:
        self._renderer = renderer
        self._host = host
        self._port = port
        self._sender = sender
        self._username = username
        self._password = password
        self._tls = tls
        self._timeout = timeout

    @property
    def tls_mode(self) -> str:
        """Resolved TLS mode. ``auto`` means implicit TLS on 465, STARTTLS anywhere else."""
        if self._tls != "auto":
            return self._tls
        return "ssl" if self._port == SMTPS_PORT else "starttls"

    def _send_sync(self, to_address: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = to_address
        message["Subject"] = subject
        message.set_content(body)

        mode = self.tls_mode
        server: smtplib.SMTP
        if mode == "ssl":
            # Implicit TLS: the connection is encrypted from the first byte. Issuing STARTTLS
            # here instead would hang, which is the usual symptom of pointing a STARTTLS client
            # at port 465.
            server = smtplib.SMTP_SSL(
                self._host,
                self._port,
                timeout=self._timeout,
                context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(self._host, self._port, timeout=self._timeout)

        with server:
            if mode == "starttls":
                server.starttls(context=ssl.create_default_context())
            if self._username:
                server.login(self._username, self._password)
            server.send_message(message)

    async def send(self, event: NotificationEvent) -> DeliveryResult:
        message = self._renderer.render(event)
        try:
            await asyncio.to_thread(
                self._send_sync, event.target.address, message.subject, message.body
            )
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("email.send_failed", recipient=event.recipient, error=str(exc))
            return DeliveryResult.failure(event, f"smtp error: {exc}")
        return DeliveryResult.success(event)

    async def aclose(self) -> None:
        return None

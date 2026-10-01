"""Email via SMTP (any provider: SES, SendGrid, Office 365, Gmail Workspace relay...).

Uses STARTTLS on the submission port (587) by default. For implicit TLS on port 465 pass
``smtp_factory=smtplib.SMTP_SSL`` and set ``SMTP_STARTTLS=false``.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import make_msgid

from callingbot.messaging.base import MessageSender, OutgoingMessage, SendResult
from callingbot.models import MessageChannel
from callingbot.settings import Settings

log = logging.getLogger(__name__)

DEFAULT_SUBJECT = "Message from your mutual fund partner team"
_TIMEOUT_SECONDS = 15.0
_MAX_ERROR_CHARS = 300


class SMTPEmailSender(MessageSender):
    channel = MessageChannel.EMAIL

    def __init__(self, settings: Settings, smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP):
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._username = settings.smtp_username
        self._password = settings.smtp_password
        self._from = settings.smtp_from
        self._starttls = settings.smtp_starttls
        self._smtp_factory = smtp_factory

    def build_email(self, message: OutgoingMessage) -> EmailMessage:
        email = EmailMessage()
        email["From"] = self._from
        email["To"] = message.to
        email["Subject"] = message.subject or DEFAULT_SUBJECT
        domain = self._from.rpartition("@")[2] if self._from and "@" in self._from else None
        email["Message-ID"] = make_msgid(domain=domain)
        email.set_content(message.body)
        return email

    def send(self, message: OutgoingMessage) -> SendResult:
        if not (self._host and self._from):
            return SendResult(ok=False, error="SMTP email is not configured (host, from address)")
        try:
            # Built inside the try: header values with CR/LF (header injection) raise ValueError.
            email = self.build_email(message)
            with self._smtp_factory(self._host, self._port, timeout=_TIMEOUT_SECONDS) as smtp:
                if self._starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if self._username:
                    smtp.login(self._username, self._password or "")
                smtp.send_message(email)
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            log.warning("SMTP send failed: %s", type(exc).__name__)
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS])
        return SendResult(ok=True, provider_message_id=email["Message-ID"])

"""Messenger: send through the configured provider and always record an outbox row.

Whatever happens at the provider, the result is an :class:`~callingbot.models.OutboundMessage`
row - ``queued`` (outbox mode), ``sent`` or ``failed`` with the error - so ops and compliance
can always see what was sent to whom, and the conversation engine never has to handle a
provider exception mid-call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from callingbot.messaging.base import MessageSender, OutgoingMessage, SendResult
from callingbot.messaging.email_smtp import SMTPEmailSender
from callingbot.messaging.outbox import OutboxSender
from callingbot.messaging.sms_twilio import TwilioSMSSender
from callingbot.messaging.whatsapp_meta import MetaWhatsAppSender
from callingbot.models import MessageChannel, MessageStatus, OutboundMessage
from callingbot.phone import mask_phone
from callingbot.settings import Settings
from callingbot.timeutil import utcnow

log = logging.getLogger(__name__)

_MAX_PROVIDER_ID_CHARS = 100  # OutboundMessage.provider_message_id is String(100)


class Messenger:
    def __init__(
        self, senders: Mapping[MessageChannel, MessageSender], *, now: Callable[[], datetime] = utcnow
    ):
        self.senders: dict[MessageChannel, MessageSender] = dict(senders)
        self._now = now

    def send(
        self,
        session: Session,
        *,
        channel: MessageChannel,
        to: str,
        body: str,
        subject: str | None = None,
        link: str | None = None,
        distributor_id: int | None = None,
        call_id: int | None = None,
        template_vars: dict[str, str] | None = None,
    ) -> OutboundMessage:
        """Send one message and persist it. Flushes (row id available) but does not commit.

        Never raises for provider problems - including a sender that raises unexpectedly or a
        channel with no configured sender; those become ``failed`` rows. A ``channel`` value that
        is not a :class:`MessageChannel` at all is a programming error and raises ``ValueError``.
        """
        channel = MessageChannel(channel)
        row = OutboundMessage(
            distributor_id=distributor_id,
            call_id=call_id,
            channel=channel,
            destination=to,
            body=body,
            link=link,
        )
        message = OutgoingMessage(
            channel=channel,
            to=to,
            body=body,
            subject=subject,
            link=link,
            template_vars=dict(template_vars or {}),
        )
        result = self._dispatch(message)

        if result.ok and result.queued_only:
            row.status = MessageStatus.QUEUED
        elif result.ok:
            row.status = MessageStatus.SENT
            row.sent_at = self._now()
            if result.provider_message_id:
                row.provider_message_id = str(result.provider_message_id)[:_MAX_PROVIDER_ID_CHARS]
        else:
            row.status = MessageStatus.FAILED
            row.error = result.error or "unknown error"
        session.add(row)
        session.flush()
        log.info(
            "%s message %s to %s: %s%s",
            channel.value,
            row.id,
            _mask_destination(channel, to),
            row.status.value,
            f" ({row.error})" if row.error else "",
        )
        return row

    def _dispatch(self, message: OutgoingMessage) -> SendResult:
        sender = self.senders.get(message.channel)
        if sender is None:
            return SendResult(ok=False, error=f"no sender configured for channel {message.channel.value!r}")
        try:
            return sender.send(message)
        except Exception as exc:  # a buggy adapter must not break a live call
            log.exception("%s sender raised", message.channel.value)
            return SendResult(ok=False, error=f"sender error: {type(exc).__name__}: {exc}"[:300])


def _mask_destination(channel: MessageChannel, to: str) -> str:
    # PII minimisation: never log full phone numbers or email addresses.
    if channel == MessageChannel.EMAIL:
        local, _, domain = to.partition("@")
        return f"{local[:1]}***@{domain}" if domain else "***"
    return mask_phone(to)


def _missing_settings(settings: Settings) -> list[str]:
    missing: list[str] = []
    if settings.sms_provider == "twilio":
        if not settings.twilio_account_sid:
            missing.append("TWILIO_ACCOUNT_SID")
        if not settings.twilio_auth_token:
            missing.append("TWILIO_AUTH_TOKEN")
        if not (settings.twilio_sms_from or settings.twilio_from_number):
            missing.append("TWILIO_SMS_FROM (or TWILIO_FROM_NUMBER)")
    if settings.whatsapp_provider == "meta":
        if not settings.meta_whatsapp_token:
            missing.append("META_WHATSAPP_TOKEN")
        if not settings.meta_whatsapp_phone_number_id:
            missing.append("META_WHATSAPP_PHONE_NUMBER_ID")
        if not settings.meta_whatsapp_template_name:
            missing.append("META_WHATSAPP_TEMPLATE_NAME")
    if settings.email_provider == "smtp":
        if not settings.smtp_host:
            missing.append("SMTP_HOST")
        if not settings.smtp_from:
            missing.append("SMTP_FROM")
        if settings.smtp_username and not settings.smtp_password:
            missing.append("SMTP_PASSWORD (SMTP_USERNAME is set)")
    return missing


def build_messenger(settings: Settings, *, http_client: httpx.Client | None = None) -> Messenger:
    """Build a Messenger from ``SMS_PROVIDER`` / ``WHATSAPP_PROVIDER`` / ``EMAIL_PROVIDER``.

    Defaults to the outbox for every channel. Selecting a real provider without its credentials
    raises ``ValueError`` naming every missing setting, so misconfiguration fails at startup
    instead of on the first message of a live call. ``http_client`` is shared by the HTTP
    providers (tests pass one backed by ``httpx.MockTransport``).
    """
    missing = _missing_settings(settings)
    if missing:
        raise ValueError("Messaging provider selected but settings are missing: " + ", ".join(missing))

    senders: dict[MessageChannel, MessageSender] = {
        MessageChannel.SMS: (
            TwilioSMSSender(settings, http_client)
            if settings.sms_provider == "twilio"
            else OutboxSender(MessageChannel.SMS)
        ),
        MessageChannel.WHATSAPP: (
            MetaWhatsAppSender(settings, http_client)
            if settings.whatsapp_provider == "meta"
            else OutboxSender(MessageChannel.WHATSAPP)
        ),
        MessageChannel.EMAIL: (
            SMTPEmailSender(settings)
            if settings.email_provider == "smtp"
            else OutboxSender(MessageChannel.EMAIL)
        ),
    }
    return Messenger(senders)

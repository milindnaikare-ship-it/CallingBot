"""Outbound messaging contract (empanelment links, follow-ups).

Every message is persisted as an :class:`~callingbot.models.OutboundMessage` row ("outbox")
regardless of provider, so there is always an auditable record of what was sent to whom.

In India, SMS requires DLT-registered sender headers and content templates, and WhatsApp
Business requires pre-approved templates - see docs/COMPLIANCE.md. Until those are in place
use the default ``outbox`` providers, which only store the message for manual sending.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from callingbot.models import MessageChannel


@dataclass
class OutgoingMessage:
    channel: MessageChannel
    to: str  # E.164 phone for SMS/WhatsApp, email address for email
    body: str
    subject: str | None = None  # email only
    link: str | None = None
    template_vars: dict[str, str] = field(default_factory=dict)  # for template-based channels


@dataclass
class SendResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None
    queued_only: bool = False  # True when stored in the outbox without contacting a provider


class MessageSender(ABC):
    channel: MessageChannel

    @abstractmethod
    def send(self, message: OutgoingMessage) -> SendResult:
        """Send one message. Must not raise for provider errors - return ok=False instead."""

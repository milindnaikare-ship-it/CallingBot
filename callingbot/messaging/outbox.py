"""Outbox "provider": store the message only, for manual or batch sending.

This is the default for every channel. Indian SMS needs DLT-registered headers and templates
and WhatsApp needs Meta-approved templates; until those exist, ops staff send the queued
:class:`~callingbot.models.OutboundMessage` rows (status ``queued``) by hand from the admin UI.
"""

from __future__ import annotations

from callingbot.messaging.base import MessageSender, OutgoingMessage, SendResult
from callingbot.models import MessageChannel


class OutboxSender(MessageSender):
    def __init__(self, channel: MessageChannel):
        self.channel = MessageChannel(channel)

    def send(self, message: OutgoingMessage) -> SendResult:
        return SendResult(ok=True, queued_only=True)

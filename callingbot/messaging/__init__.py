"""Outbound messaging: empanelment links and follow-ups by SMS, WhatsApp and email.

Use :func:`build_messenger` to get a :class:`Messenger` wired to the providers selected in
settings (the outbox by default). See :mod:`callingbot.messaging.base` for the sender contract.
"""

from __future__ import annotations

from callingbot.messaging.base import MessageSender, OutgoingMessage, SendResult
from callingbot.messaging.service import Messenger, build_messenger

__all__ = ["MessageSender", "Messenger", "OutgoingMessage", "SendResult", "build_messenger"]

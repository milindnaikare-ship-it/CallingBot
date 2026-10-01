"""SMS via the Twilio Messages REST API.

India: commercial SMS to Indian numbers must carry a DLT-registered sender header (entity and
header registered with an operator's DLT platform, mapped in the Twilio console) and the body
must match a DLT-approved content template - variables included. Messages that do not match
are silently dropped by Indian operators, so register the empanelment-link template before
switching ``SMS_PROVIDER`` to ``twilio``.
"""

from __future__ import annotations

import logging

import httpx

from callingbot.messaging.base import MessageSender, OutgoingMessage, SendResult
from callingbot.models import MessageChannel
from callingbot.phone import mask_phone
from callingbot.settings import Settings

log = logging.getLogger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"
_TIMEOUT_SECONDS = 10.0
_MAX_ERROR_CHARS = 300


class TwilioSMSSender(MessageSender):
    channel = MessageChannel.SMS

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None):
        self._account_sid = settings.twilio_account_sid
        self._auth_token = settings.twilio_auth_token
        # A dedicated SMS sender (DLT header / messaging service) may differ from the voice caller id.
        self._from = settings.twilio_sms_from or settings.twilio_from_number
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=_TIMEOUT_SECONDS)

    def send(self, message: OutgoingMessage) -> SendResult:
        if not (self._account_sid and self._auth_token and self._from):
            return SendResult(
                ok=False, error="Twilio SMS is not configured (account sid, auth token, sender)"
            )
        url = f"{TWILIO_API_BASE}/Accounts/{self._account_sid}/Messages.json"
        try:
            resp = self._client.post(
                url,
                data={"To": self._address(message.to), "From": self._from, "Body": message.body},
                auth=(self._account_sid, self._auth_token),
            )
        except httpx.HTTPError as exc:
            log.warning("Twilio SMS to %s failed: %s", mask_phone(message.to), type(exc).__name__)
            return SendResult(
                ok=False, error=f"network error: {type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
            )

        data = _json_or_empty(resp)
        if resp.is_error:
            # Twilio error bodies look like {"code": 21211, "message": "The 'To' number ...", ...}
            detail = data.get("message") or resp.reason_phrase
            code = f" (code {data['code']})" if data.get("code") else ""
            log.warning(
                "Twilio SMS to %s rejected: HTTP %s%s", mask_phone(message.to), resp.status_code, code
            )
            return SendResult(ok=False, error=f"HTTP {resp.status_code}{code}: {detail}"[:_MAX_ERROR_CHARS])
        return SendResult(ok=True, provider_message_id=data.get("sid"))

    def _address(self, number: str) -> str:
        return number

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class TwilioWhatsAppSender(TwilioSMSSender):
    """WhatsApp via Twilio's Messages API - the Twilio Sandbox for testing, or an approved Twilio
    WhatsApp sender.

    Free-form text is delivered only inside a 24-hour session the recipient opened (for the sandbox:
    after sending the "join <code>" message). Business-initiated messages outside a session need a
    WhatsApp-approved template, which is a go-live task.
    """

    channel = MessageChannel.WHATSAPP

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None):
        super().__init__(settings, http_client)
        sender = settings.twilio_whatsapp_from
        self._from = self._address(sender) if sender else None

    def _address(self, number: str) -> str:
        return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


def _json_or_empty(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}

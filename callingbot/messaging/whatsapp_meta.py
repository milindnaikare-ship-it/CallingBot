"""WhatsApp via the Meta (WhatsApp Business) Cloud API, using an approved template message.

Business-initiated WhatsApp messages outside a 24-hour customer-service window must use a
template pre-approved by Meta, so this sender always sends ``type: template``. The free-text
``body`` of the :class:`OutgoingMessage` is *not* transmitted; it is still stored on the
outbox row as the human-readable record of what the template says. ``template_vars`` values
fill the template's body placeholders ``{{1}}, {{2}}, ...`` in insertion order, so callers must
build the dict in placeholder order (e.g. ``{"name": ..., "link": ...}``).
"""

from __future__ import annotations

import logging
import re

import httpx

from callingbot.messaging.base import MessageSender, OutgoingMessage, SendResult
from callingbot.models import MessageChannel
from callingbot.phone import mask_phone
from callingbot.settings import Settings

log = logging.getLogger(__name__)

# Meta retires Graph API versions roughly two years after release; keep this current.
GRAPH_API_VERSION = "v20.0"
GRAPH_API_BASE = "https://graph.facebook.com"
_TIMEOUT_SECONDS = 10.0
_MAX_ERROR_CHARS = 300
_NON_DIGITS = re.compile(r"\D+")
_WHITESPACE = re.compile(r"\s+")


class MetaWhatsAppSender(MessageSender):
    channel = MessageChannel.WHATSAPP

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None):
        self._token = settings.meta_whatsapp_token
        self._phone_number_id = settings.meta_whatsapp_phone_number_id
        self._template_name = settings.meta_whatsapp_template_name
        self._template_language = settings.meta_whatsapp_template_language
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=_TIMEOUT_SECONDS)

    def build_payload(self, message: OutgoingMessage) -> dict:
        template: dict = {"name": self._template_name, "language": {"code": self._template_language}}
        if message.template_vars:
            # Meta rejects template parameters containing newlines, tabs or 4+ consecutive spaces.
            params = [
                {"type": "text", "text": _WHITESPACE.sub(" ", str(v)).strip()}
                for v in message.template_vars.values()
            ]
            template["components"] = [{"type": "body", "parameters": params}]
        return {
            "messaging_product": "whatsapp",
            "to": _NON_DIGITS.sub("", message.to),  # Cloud API wants the number without "+"
            "type": "template",
            "template": template,
        }

    def send(self, message: OutgoingMessage) -> SendResult:
        if not (self._token and self._phone_number_id and self._template_name):
            return SendResult(
                ok=False, error="Meta WhatsApp is not configured (token, phone number id, template)"
            )
        url = f"{GRAPH_API_BASE}/{GRAPH_API_VERSION}/{self._phone_number_id}/messages"
        try:
            resp = self._client.post(
                url, json=self.build_payload(message), headers={"Authorization": f"Bearer {self._token}"}
            )
        except httpx.HTTPError as exc:
            log.warning("WhatsApp to %s failed: %s", mask_phone(message.to), type(exc).__name__)
            return SendResult(
                ok=False, error=f"network error: {type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
            )

        data = _json_or_empty(resp)
        if resp.is_error:
            # Graph errors look like {"error": {"message": "...", "code": 131026, ...}}
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            detail = err.get("message") or resp.reason_phrase
            code = f" (code {err['code']})" if err.get("code") else ""
            log.warning("WhatsApp to %s rejected: HTTP %s%s", mask_phone(message.to), resp.status_code, code)
            return SendResult(ok=False, error=f"HTTP {resp.status_code}{code}: {detail}"[:_MAX_ERROR_CHARS])

        message_id = None
        messages = data.get("messages")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            message_id = messages[0].get("id")
        return SendResult(ok=True, provider_message_id=message_id)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _json_or_empty(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}

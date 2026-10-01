"""Exotel adapter (Indian CPaaS; supports DLT-registered 140-series telemarketing numbers).

What this adapter does with Exotel's v1 Calls API, and what it deliberately does not:

* **Placing calls - implemented.** ``POST https://{EXOTEL_SUBDOMAIN}/v1/Accounts/{sid}/Calls/connect.json``
  dials the distributor (``From``) from our ExoPhone (``CallerId``) and connects the answered
  call to an Exotel flow (``EXOTEL_APP_ID``, built in Exotel's App Bazaar). ``CustomField``
  carries our ``Call.id`` and ``StatusCallback`` points at our ``status`` webhook. Use the
  subdomain of your account's cluster (``api.exotel.com`` Singapore, ``api.in.exotel.com``
  India). ``CallType=trans`` is the value the v1 connect API documents; whether a call counts as
  promotional or service under TRAI TCCCPR depends on the DLT registration of the ``CallerId``
  number series, so run NFO/empanelment campaigns only from a 140-series/DLT-registered number.
* **Status callbacks - implemented.** We subscribe to the ``terminal`` event only (Exotel v1 does
  not post intermediate ringing/answered events here). :meth:`ExotelProvider.parse_status` maps
  ``Status`` and reads the duration from ``ConversationDuration`` / ``Duration`` /
  ``DialCallDuration``. Exotel has no answering-machine detection in this flow, so
  ``answered_by`` is always ``None`` and voicemail cannot be told apart from a short completed call.
* **Hang-up - not available.** v1 has no simple "end this call" endpoint for a call connected to
  a flow; the flow ends the call. :meth:`ExotelProvider.hangup` only logs.
* **Conversation turns - Phase 3.** The bot's spoken turns on Exotel need the Voicebot applet
  (bidirectional audio streaming over WebSocket), which does not fit the request/response
  ``render`` model. :meth:`ExotelProvider.render` raises ``NotImplementedError``; see
  docs/DEVELOPMENT_PLAN.md. Until then use Exotel for dialling + status only, or Twilio/simulator.
* **Webhook authenticity - not signed.** Exotel does not sign webhooks the way Twilio does, so
  :meth:`ExotelProvider.verify_webhook` accepts everything. Protect the ``/telephony/exotel/*``
  routes with an IP allow-list of Exotel's egress addresses at the reverse proxy and/or an
  unguessable secret path segment in ``PUBLIC_BASE_URL``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from urllib.parse import urlencode

import httpx

from callingbot.models import CallStatus
from callingbot.phone import mask_phone
from callingbot.settings import Settings
from callingbot.telephony.base import (
    CallStatusUpdate,
    PlaceCallResult,
    TelephonyError,
    TelephonyProvider,
    VoiceInput,
    VoiceResponse,
)
from callingbot.telephony.twilio import (
    clean_speech,
    describe_settings,
    mask_numbers,
    missing_settings,
    parse_float_field,
    parse_int_field,
)

log = logging.getLogger(__name__)

EXOTEL_CALL_STATUS: dict[str, CallStatus] = {
    "queued": CallStatus.QUEUED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "failed": CallStatus.FAILED,
    "busy": CallStatus.BUSY,
    "no-answer": CallStatus.NO_ANSWER,
    "canceled": CallStatus.CANCELED,
    "cancelled": CallStatus.CANCELED,
}

# Exotel reports duration under different names depending on the callback/applet; first wins.
_DURATION_FIELDS = ("ConversationDuration", "Duration", "DialCallDuration")

RENDER_NOT_IMPLEMENTED = (
    "Exotel conversational turns need the Exotel Voicebot applet (bidirectional audio streaming), "
    "planned for Phase 3 - see docs/DEVELOPMENT_PLAN.md. Use the 'twilio' or 'simulator' provider "
    "for conversations until then."
)


def _api_host(subdomain: str) -> str:
    # Operators often paste "https://api.in.exotel.com/"; we only want the host.
    host = subdomain.strip()
    for scheme in ("https://", "http://"):
        if host.lower().startswith(scheme):
            host = host[len(scheme) :]
    return host.strip("/")


class ExotelProvider(TelephonyProvider):
    """Exotel v1: outbound dialling into an Exotel flow plus terminal status callbacks."""

    name = "exotel"
    required_settings: tuple[str, ...] = (
        "exotel_account_sid",
        "exotel_api_key",
        "exotel_api_token",
        "exotel_caller_id",
        "exotel_app_id",
    )

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None):
        self.settings = settings
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))

    def close(self) -> None:
        """Close the HTTP client if this provider created it."""
        if self._owns_client:
            self._http.close()

    # ---------------------------------------------------------------- REST: call control
    def connect_url(self) -> str:
        host = _api_host(self.settings.exotel_subdomain)
        return f"https://{host}/v1/Accounts/{self.settings.exotel_account_sid}/Calls/connect.json"

    def flow_url(self) -> str:
        """ExoML start URL of the configured flow (Exotel documents this as plain http)."""
        return f"http://my.exotel.com/{self.settings.exotel_account_sid}/exoml/start_voice/{self.settings.exotel_app_id}"

    def build_call_form(self, *, to_number: str, call_id: int) -> list[tuple[str, str]]:
        """Form parameters for the connect.json request (exposed for tests and diagnostics)."""
        return [
            ("From", to_number),  # Exotel dials "From" first: for us that is the distributor
            ("CallerId", self.settings.exotel_caller_id or ""),
            ("Url", self.flow_url()),
            ("CallType", "trans"),
            ("StatusCallback", self.webhook_url(self.settings.base_url, self.name, "status", call_id)),
            ("StatusCallbackEvents[0]", "terminal"),
            ("StatusCallbackContentType", "multipart/form-data"),
            ("CustomField", str(call_id)),
        ]

    def place_call(self, *, to_number: str, call_id: int) -> PlaceCallResult:
        missing = missing_settings(self.settings, self.required_settings)
        if missing:
            raise TelephonyError(f"Exotel is not configured; missing {describe_settings(missing)}")
        form = self.build_call_form(to_number=to_number, call_id=call_id)
        try:
            response = self._http.post(
                self.connect_url(),
                content=urlencode(form).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                auth=(self.settings.exotel_api_key or "", self.settings.exotel_api_token or ""),
            )
        except httpx.HTTPError as exc:
            raise TelephonyError(mask_numbers(f"Exotel place_call request failed: {exc}")) from exc

        try:
            body = response.json()
        except ValueError:
            body = None
        if not response.is_success or (isinstance(body, dict) and "RestException" in body):
            raise TelephonyError(mask_numbers(f"Exotel place_call failed: {_exotel_error(response, body)}"))

        call = body.get("Call") if isinstance(body, dict) else None
        sid = call.get("Sid") if isinstance(call, dict) else None
        if not sid:
            raise TelephonyError(f"Exotel place_call response has no Call.Sid (HTTP {response.status_code})")
        raw_status = str(call.get("Status") or "").strip().lower()
        # Exotel says "in-progress" as soon as it starts dialling, before anyone answers; reporting
        # IN_PROGRESS here would make the call look connected. Terminal statuses are kept as-is.
        status = EXOTEL_CALL_STATUS.get(raw_status, CallStatus.INITIATED)
        if status in (CallStatus.IN_PROGRESS, CallStatus.RINGING):
            status = CallStatus.INITIATED
        log.info(
            "Exotel call %s placed to %s for call_id=%s (%s)", sid, mask_phone(to_number), call_id, status
        )
        return PlaceCallResult(provider_call_id=str(sid), status=status)

    def hangup(self, provider_call_id: str) -> None:
        # No v1 endpoint ends a flow-connected call; it ends when the flow does or the callee hangs up.
        log.info("Exotel hangup requested for %s: not supported by the v1 API, ignoring", provider_call_id)

    # ---------------------------------------------------------------- webhooks: inbound
    def verify_webhook(self, *, url: str, params: Mapping[str, str], headers: Mapping[str, str]) -> bool:
        # Exotel webhooks are unsigned; authenticity must come from the network layer (see module docs).
        return True

    def parse_voice_input(self, params: Mapping[str, str]) -> VoiceInput:
        speech = clean_speech(params.get("SpeechResult"))
        if speech is None:
            # Exotel's passthru applet sends keypad input as digits, wrapped in quotes: "\"1\"".
            speech = clean_speech(str(params.get("digits") or "").strip().strip('"'))
        return VoiceInput(
            provider_call_id=clean_speech(params.get("CallSid")),
            speech_text=speech,
            confidence=parse_float_field(params.get("Confidence")),
            answered_by=None,  # no answering-machine detection in this flow
            raw=dict(params),
        )

    def parse_status(self, params: Mapping[str, str]) -> CallStatusUpdate:
        call_sid = clean_speech(params.get("CallSid"))
        raw_status = params.get("Status") or params.get("CallStatus")
        status = EXOTEL_CALL_STATUS.get(str(raw_status or "").strip().lower())
        if status is None:
            # Same policy as Twilio: never crash the webhook; keep the original value in raw.
            log.warning("Unrecognised Exotel Status %r for %s; treating it as failed", raw_status, call_sid)
            status = CallStatus.FAILED
        duration = None
        for field_name in _DURATION_FIELDS:
            duration = parse_int_field(params.get(field_name))
            if duration is not None:
                break
        return CallStatusUpdate(
            provider_call_id=call_sid,
            status=status,
            duration_seconds=duration,
            answered_by=None,
            recording_url=clean_speech(params.get("RecordingUrl")),
            raw=dict(params),
        )

    def render(self, response: VoiceResponse, *, call_id: int) -> tuple[str, str]:
        raise NotImplementedError(RENDER_NOT_IMPLEMENTED)


def _exotel_error(response: httpx.Response, body: object) -> str:
    """Readable error from Exotel's ``{"RestException": {"Status", "Message", "Code"}}`` body."""
    exc = body.get("RestException") if isinstance(body, dict) else None
    if isinstance(exc, dict):
        code = exc.get("Code") or exc.get("Status")
        return f"HTTP {response.status_code}, Exotel error {code}: {exc.get('Message')}"
    text = response.text.strip()[:200]
    return f"HTTP {response.status_code}: {text or response.reason_phrase}"

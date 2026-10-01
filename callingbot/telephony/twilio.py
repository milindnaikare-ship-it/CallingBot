"""Twilio Programmable Voice adapter.

Call flow (turn based, see :mod:`callingbot.telephony.base`):

1. :meth:`TwilioProvider.place_call` creates the call with the REST API
   (``POST /2010-04-01/Accounts/{sid}/Calls.json``). ``Url`` is our ``answer`` webhook and
   ``StatusCallback`` our ``status`` webhook. Answering-machine detection
   (``TWILIO_MACHINE_DETECTION``) and whole-call recording (``TWILIO_RECORD_CALLS``) are switched
   by settings. The greeting must disclose recording when recording is on.
2. Twilio posts the answer/turn webhooks. :meth:`TwilioProvider.render` replies with TwiML: the
   bot's text as ``<Say>`` *nested inside* ``<Gather input="speech">`` so the distributor can
   barge in, and the transcript is posted to our ``turn`` webhook.
3. Lifecycle callbacks are normalised by :meth:`TwilioProvider.parse_status`.

Normalisation choices (relied upon by the engine and the lifecycle service):

* ``answered_by`` is ``"human"``, ``"machine"`` (any ``machine_*`` value), ``"fax"`` or
  ``"unknown"`` (detection ran but was inconclusive). It is ``None`` when Twilio sent no
  ``AnsweredBy`` at all, i.e. machine detection was off or has not reported yet.
* A ``completed`` call answered by a machine is reported as :attr:`CallStatus.VOICEMAIL`.
* An unrecognised ``CallStatus`` is mapped to :attr:`CallStatus.FAILED` (logged; the original
  value stays in ``raw``) rather than raising, so a future Twilio status cannot crash the
  webhook and leave the call dangling.

Security: verify every webhook with :meth:`TwilioProvider.verify_webhook` (``X-Twilio-Signature``
= base64 HMAC-SHA1 of the URL plus the sorted POST parameters). The URL must be the exact public
URL Twilio called, i.e. built from ``PUBLIC_BASE_URL``, not the internal URL behind a proxy.

India / TRAI note: telemarketing calls to Indian numbers must originate from a DLT-registered
number series (140-series) and respect NCPR preferences and calling hours. Confirm with Twilio
that ``TWILIO_FROM_NUMBER`` is permitted for domestic commercial calls in India before using this
adapter beyond pilots; :mod:`callingbot.telephony.exotel` targets Indian 140-series numbers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

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

log = logging.getLogger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"

# Lifecycle events we want posted to the status webhook (Twilio sends only "completed" by default).
STATUS_CALLBACK_EVENTS: tuple[str, ...] = ("initiated", "ringing", "answered", "completed")

# Ring time before Twilio gives up with "no-answer".
RING_TIMEOUT_SECONDS = 30
# Ring time for a warm transfer to the relationship-manager desk.
TRANSFER_DIAL_TIMEOUT_SECONDS = 25

# Domain vocabulary passed to Twilio speech recognition. Distributors use these words constantly
# and generic STT models mis-hear them ("ARN" -> "urn", "NFO" -> "and if oh").
SPEECH_HINTS = (
    "ARN, empanelment, empanel, NFO, SIP, WhatsApp, email, SMS, callback, relationship manager, "
    "brokerage, commission"
)

# Spoken after a <Dial> ends. Deliberately makes no promise (no callback commitment), because the
# adapter cannot know whether the engine scheduled one. Keyed by primary language subtag.
_TRANSFER_FALLBACK: dict[str, str] = {
    "en": "Sorry, I could not connect you to our team right now. Thank you for your time. Goodbye.",
    "hi": "क्षमा करें, अभी हमारी टीम से संपर्क नहीं हो पाया। आपके समय के लिए धन्यवाद।",
}

TWILIO_CALL_STATUS: dict[str, CallStatus] = {
    "queued": CallStatus.QUEUED,
    "initiated": CallStatus.INITIATED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.BUSY,
    "failed": CallStatus.FAILED,
    "no-answer": CallStatus.NO_ANSWER,
    "canceled": CallStatus.CANCELED,
}

_MACHINE_ANSWERED_BY = frozenset(
    {"machine", "machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other"}
)

# Characters that are illegal in XML 1.0 even when escaped; an LLM reply containing one would
# otherwise make the whole TwiML document unparseable and drop the call.
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]")

_E164_IN_TEXT = re.compile(r"\+\d{8,15}")


# --------------------------------------------------------------------------------------------
# Shared helpers. Public because the exotel/simulator adapters and get_provider reuse them
# (the simulator deliberately accepts Twilio's webhook field names).
# --------------------------------------------------------------------------------------------
def normalize_answered_by(raw: str | None) -> str | None:
    """Collapse Twilio ``AnsweredBy`` values to ``human`` / ``machine`` / ``fax`` / ``unknown``.

    Returns ``None`` when the field is absent or blank (detection not enabled or not reported).
    """
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value:
        return None
    if value == "human":
        return "human"
    if value in _MACHINE_ANSWERED_BY or value.startswith("machine"):
        return "machine"
    if value == "fax":
        return "fax"
    return "unknown"


def parse_int_field(raw: Any) -> int | None:
    """Non-negative int from a form value (``"42"``, ``"42.0"``); ``None`` when absent/invalid."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def parse_float_field(raw: Any) -> float | None:
    """Finite float from a form value; ``None`` when absent/invalid."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def clean_speech(raw: Any) -> str | None:
    """Stripped transcript, or ``None`` for silence (missing/blank)."""
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def compute_signature(
    auth_token: str, url: str, params: Mapping[str, Any] | Iterable[tuple[str, Any]]
) -> str:
    """Twilio request signature: base64(HMAC-SHA1(auth_token, url + name1value1 + name2value2 ...)).

    Parameters are sorted by name (Unicode code point order); a repeated name contributes every
    value, sorted.
    """
    data = url + "".join(name + value for name, value in sorted(_form_items(params)))
    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _form_items(params: Mapping[str, Any] | Iterable[tuple[str, Any]]) -> list[tuple[str, str]]:
    # Starlette's FormData (and httpx.QueryParams) keep repeated keys only via multi_items().
    multi_items = getattr(params, "multi_items", None)
    if callable(multi_items):
        pairs: Iterable[tuple[Any, Any]] = multi_items()
    elif isinstance(params, Mapping):
        pairs = params.items()
    else:
        pairs = params
    items: list[tuple[str, str]] = []
    for name, value in pairs:
        values = value if isinstance(value, (list, tuple)) else [value]
        items.extend((str(name), "" if v is None else str(v)) for v in values)
    return items


def _url_variants(url: str) -> list[str]:
    """The URL as given plus the same URL with the default port added/removed.

    Twilio sometimes signs ``https://host:443/path`` while we reconstruct ``https://host/path``
    (or vice versa); Twilio's own validators try both forms, so we do too.
    """
    variants = [url]
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return variants
    default_port = {"https": 443, "http": 80}.get(parts.scheme)
    if default_port is None or not parts.netloc:
        return variants
    if port is None:
        variants.append(urlunsplit(parts._replace(netloc=f"{parts.netloc}:{default_port}")))
    elif port == default_port:
        variants.append(urlunsplit(parts._replace(netloc=parts.netloc.rsplit(":", 1)[0])))
    return variants


def _header(headers: Mapping[str, str], name: str) -> str | None:
    # Starlette's Headers is case-insensitive already, but plain dicts (tests, other frameworks) are not.
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return value
    return None


def mask_numbers(text: str) -> str:
    """Mask E.164 numbers inside free text (``+91******3210``)."""
    # Provider error messages often echo the dialled number; keep PII out of logs and call.error.
    return _E164_IN_TEXT.sub(lambda m: mask_phone(m.group(0)), text)


def _xml_text(text: str | None) -> str:
    return _XML_ILLEGAL.sub("", text or "")


def missing_settings(settings: Settings, names: Iterable[str]) -> list[str]:
    """Names of settings that are unset or blank."""
    missing = []
    for name in names:
        value = getattr(settings, name, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return missing


def describe_settings(names: Iterable[str]) -> str:
    """``"twilio_auth_token (TWILIO_AUTH_TOKEN)"`` - field name plus the env var operators set."""
    return ", ".join(f"{n} ({n.upper()})" for n in names)


class TwilioProvider(TelephonyProvider):
    """Twilio Voice: REST API for call control, TwiML ``<Say>``/``<Gather>`` for the voice turns."""

    name = "twilio"
    required_settings: tuple[str, ...] = ("twilio_account_sid", "twilio_auth_token", "twilio_from_number")

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None):
        self.settings = settings
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))

    def close(self) -> None:
        """Close the HTTP client if this provider created it."""
        if self._owns_client:
            self._http.close()

    # ---------------------------------------------------------------- REST: call control
    def _calls_url(self, provider_call_id: str | None = None) -> str:
        base = f"{TWILIO_API_BASE}/Accounts/{self.settings.twilio_account_sid}/Calls"
        return f"{base}/{provider_call_id}.json" if provider_call_id else f"{base}.json"

    def _auth(self) -> tuple[str, str]:
        return (self.settings.twilio_account_sid or "", self.settings.twilio_auth_token or "")

    def _post_form(self, url: str, form: list[tuple[str, str]]) -> httpx.Response:
        # httpx's ``data=`` only takes a mapping; encoding the pair list ourselves keeps repeated
        # keys (StatusCallbackEvent) and their order exactly as built.
        return self._http.post(
            url,
            content=urlencode(form).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            auth=self._auth(),
        )

    def build_call_form(self, *, to_number: str, call_id: int) -> list[tuple[str, str]]:
        """Form parameters for the Calls.json request (exposed for tests and diagnostics)."""
        base = self.settings.base_url
        form: list[tuple[str, str]] = [
            ("To", to_number),
            ("From", self.settings.twilio_from_number or ""),
            ("Url", self.webhook_url(base, self.name, "answer", call_id)),
            ("Method", "POST"),
            ("StatusCallback", self.webhook_url(base, self.name, "status", call_id)),
            ("StatusCallbackMethod", "POST"),
        ]
        form.extend(("StatusCallbackEvent", event) for event in STATUS_CALLBACK_EVENTS)
        form.append(("Timeout", str(RING_TIMEOUT_SECONDS)))
        if self.settings.twilio_machine_detection:
            form.append(("MachineDetection", "Enable"))
        if self.settings.twilio_record_calls:
            form.append(("Record", "true"))
        return form

    def place_call(self, *, to_number: str, call_id: int) -> PlaceCallResult:
        missing = missing_settings(self.settings, self.required_settings)
        if missing:
            raise TelephonyError(f"Twilio is not configured; missing {describe_settings(missing)}")
        form = self.build_call_form(to_number=to_number, call_id=call_id)
        try:
            response = self._post_form(self._calls_url(), form)
        except httpx.HTTPError as exc:
            raise TelephonyError(mask_numbers(f"Twilio place_call request failed: {exc}")) from exc
        if not response.is_success:
            raise TelephonyError(mask_numbers(f"Twilio place_call failed: {_twilio_error(response)}"))
        try:
            body = response.json()
        except ValueError as exc:
            raise TelephonyError(
                f"Twilio place_call returned a non-JSON body (HTTP {response.status_code})"
            ) from exc
        sid = body.get("sid") if isinstance(body, dict) else None
        if not sid:
            raise TelephonyError(f"Twilio place_call response has no call sid (HTTP {response.status_code})")
        raw_status = str(body.get("status") or "").strip().lower()
        # A freshly created call is "queued"; anything unexpected is still a live call attempt.
        status = TWILIO_CALL_STATUS.get(raw_status, CallStatus.INITIATED)
        log.info(
            "Twilio call %s placed to %s for call_id=%s (%s)", sid, mask_phone(to_number), call_id, status
        )
        return PlaceCallResult(provider_call_id=str(sid), status=status)

    def hangup(self, provider_call_id: str) -> None:
        if not provider_call_id:
            log.warning("Twilio hangup skipped: no provider call id")
            return
        if missing_settings(self.settings, ("twilio_account_sid", "twilio_auth_token")):
            log.warning(
                "Twilio hangup of %s skipped: Twilio credentials are not configured", provider_call_id
            )
            return
        try:
            response = self._post_form(self._calls_url(provider_call_id), [("Status", "completed")])
        except httpx.HTTPError as exc:
            log.warning("Twilio hangup of %s failed: %s", provider_call_id, exc)
            return
        if not response.is_success:
            log.warning("Twilio hangup of %s failed: %s", provider_call_id, _twilio_error(response))

    # ---------------------------------------------------------------- webhooks: inbound
    def verify_webhook(self, *, url: str, params: Mapping[str, str], headers: Mapping[str, str]) -> bool:
        if not self.settings.twilio_validate_signature:
            return True
        token = self.settings.twilio_auth_token
        signature = _header(headers, "X-Twilio-Signature")
        if not token or not signature:
            return False
        items = _form_items(params)
        return any(
            hmac.compare_digest(compute_signature(token, candidate, items).encode(), signature.encode())
            for candidate in _url_variants(url)
        )

    def parse_voice_input(self, params: Mapping[str, str]) -> VoiceInput:
        return VoiceInput(
            provider_call_id=clean_speech(params.get("CallSid")),
            speech_text=clean_speech(params.get("SpeechResult")),
            confidence=parse_float_field(params.get("Confidence")),
            answered_by=normalize_answered_by(params.get("AnsweredBy")),
            raw=dict(params),
        )

    def parse_status(self, params: Mapping[str, str]) -> CallStatusUpdate:
        call_sid = clean_speech(params.get("CallSid"))
        raw_status = params.get("CallStatus")
        status = TWILIO_CALL_STATUS.get(str(raw_status or "").strip().lower())
        if status is None:
            log.warning(
                "Unrecognised Twilio CallStatus %r for %s; treating it as failed", raw_status, call_sid
            )
            status = CallStatus.FAILED
        answered_by = normalize_answered_by(params.get("AnsweredBy"))
        if status == CallStatus.COMPLETED and answered_by == "machine":
            status = CallStatus.VOICEMAIL
        return CallStatusUpdate(
            provider_call_id=call_sid,
            status=status,
            duration_seconds=parse_int_field(params.get("CallDuration")),
            answered_by=answered_by,
            recording_url=clean_speech(params.get("RecordingUrl")),
            raw=dict(params),
        )

    # ---------------------------------------------------------------- TwiML
    def render(self, response: VoiceResponse, *, call_id: int) -> tuple[str, str]:
        root = ET.Element("Response")
        turn_url = self.webhook_url(self.settings.base_url, self.name, "turn", call_id)

        if response.action == "gather":
            gather = ET.SubElement(
                root,
                "Gather",
                {
                    "input": "speech",
                    "action": turn_url,
                    "method": "POST",
                    "language": response.stt_language or response.language,
                    "speechTimeout": "auto",
                    "timeout": str(response.gather_timeout_seconds),
                    "actionOnEmptyResult": "true",
                    "hints": SPEECH_HINTS,
                },
            )
            self._add_says(gather, response.say, response.language, response.voice)
            # Only reached if Gather ends without posting (e.g. a Twilio-side error); keeps the loop alive.
            redirect = ET.SubElement(root, "Redirect", {"method": "POST"})
            redirect.text = turn_url
        elif response.action == "transfer":
            self._add_says(root, response.say, response.language, response.voice)
            if response.transfer_to:
                dial_attrs = {"timeout": str(TRANSFER_DIAL_TIMEOUT_SECONDS)}
                if self.settings.twilio_from_number:
                    dial_attrs["callerId"] = self.settings.twilio_from_number
                dial = ET.SubElement(root, "Dial", dial_attrs)
                dial.text = _xml_text(response.transfer_to)
            else:
                log.warning("Transfer requested for call_id=%s without a number; hanging up instead", call_id)
            self._add_transfer_fallback(root, response)
            ET.SubElement(root, "Hangup")
        else:  # "hangup" (and anything unexpected: ending politely is the safe default)
            if response.action != "hangup":
                log.warning("Unknown voice action %r for call_id=%s; hanging up", response.action, call_id)
            self._add_says(root, response.say, response.language, response.voice)
            ET.SubElement(root, "Hangup")

        body = ET.tostring(root, encoding="unicode")
        return '<?xml version="1.0" encoding="UTF-8"?>' + body, "application/xml"

    @staticmethod
    def _add_says(parent: ET.Element, utterances: Iterable[str], language: str, voice: str | None) -> None:
        for utterance in utterances:
            text = _xml_text(utterance).strip()
            if not text:
                continue  # Twilio rejects an empty <Say> (error 13520)
            attrs = {"voice": voice} if voice else {}
            attrs["language"] = language
            say = ET.SubElement(parent, "Say", attrs)
            say.text = text

    def _add_transfer_fallback(self, parent: ET.Element, response: VoiceResponse) -> None:
        primary = (response.language or "").split("-")[0].lower()
        if primary in _TRANSFER_FALLBACK:
            text, language, voice = _TRANSFER_FALLBACK[primary], response.language, response.voice
        else:
            # A voice configured for e.g. Marathi may not speak English text; let Twilio pick one.
            text, language, voice = _TRANSFER_FALLBACK["en"], "en-IN", None
        self._add_says(parent, [text], language, voice)


def _twilio_error(response: httpx.Response) -> str:
    """Human-readable error from a Twilio error response ({"code", "message", "more_info", ...})."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and (body.get("code") is not None or body.get("message")):
        return f"HTTP {response.status_code}, Twilio error {body.get('code')}: {body.get('message')}"
    text = response.text.strip()[:200]
    return f"HTTP {response.status_code}: {text or response.reason_phrase}"

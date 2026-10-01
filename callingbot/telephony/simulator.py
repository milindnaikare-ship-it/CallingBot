"""In-process simulator provider: no external effects, used by tests, demos and the admin simulator.

The browser-based simulator (``web/simulator_routes.py``) plays the provider's role: it posts the
same webhook fields Twilio would (``CallSid``, ``SpeechResult``, ``Confidence``, ``AnsweredBy``,
``CallStatus``, ``CallDuration``) and receives the bot's next turn as JSON instead of TwiML.
``CallStatus`` may be given either in Twilio's spelling (``in-progress``, ``no-answer``) or as our
own :class:`~callingbot.models.CallStatus` values (``in_progress``, ``no_answer``, ``voicemail``).
Normalisation otherwise matches :mod:`callingbot.telephony.twilio` (machine + completed =
voicemail; unknown status = failed, logged, raw kept).
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Mapping
from typing import Any

from callingbot.models import CallStatus
from callingbot.telephony.base import (
    CallStatusUpdate,
    PlaceCallResult,
    TelephonyProvider,
    VoiceInput,
    VoiceResponse,
)
from callingbot.telephony.twilio import (
    TWILIO_CALL_STATUS,
    clean_speech,
    normalize_answered_by,
    parse_float_field,
    parse_int_field,
)

log = logging.getLogger(__name__)


def _parse_call_status(raw: Any) -> CallStatus | None:
    value = str(raw or "").strip().lower()
    if value in TWILIO_CALL_STATUS:
        return TWILIO_CALL_STATUS[value]
    try:
        return CallStatus(value)
    except ValueError:
        return None


class SimulatorProvider(TelephonyProvider):
    """Records what would have happened; ``placed`` / ``hung_up`` let tests and the UI inspect it."""

    name = "simulator"

    def __init__(self) -> None:
        self.placed: list[dict[str, Any]] = []
        self.hung_up: list[str] = []

    def place_call(self, *, to_number: str, call_id: int) -> PlaceCallResult:
        # Random suffix keeps ids unique across re-dials of the same Call.id and app restarts
        # (Call.provider_call_id is a unique column).
        provider_call_id = f"SIM-{call_id}-{secrets.token_hex(4)}"
        self.placed.append({"to_number": to_number, "call_id": call_id, "provider_call_id": provider_call_id})
        return PlaceCallResult(provider_call_id=provider_call_id, status=CallStatus.INITIATED)

    def hangup(self, provider_call_id: str) -> None:
        self.hung_up.append(provider_call_id)

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
        status = _parse_call_status(raw_status)
        if status is None:
            log.warning(
                "Unrecognised simulator CallStatus %r for %s; treating it as failed", raw_status, call_sid
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

    def render(self, response: VoiceResponse, *, call_id: int) -> tuple[str, str]:
        payload = {
            "say": list(response.say),
            "language": response.language,
            "voice": response.voice,
            # Resolved here so the browser does not need to know the "defaults to language" rule.
            "stt_language": response.stt_language or response.language,
            "action": response.action,
            "transfer_to": response.transfer_to,
        }
        return json.dumps(payload, ensure_ascii=False), "application/json"

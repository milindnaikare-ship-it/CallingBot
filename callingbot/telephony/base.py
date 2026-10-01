"""Provider-agnostic telephony contract.

The bot is *turn based*: the provider plays our text with its TTS, listens with its speech
recognition, and calls our webhook with the transcript. Every provider adapter translates
between its own webhook/markup format and the neutral types below, so the conversation
engine never sees provider details.

Webhook routes (see ``callingbot.web.telephony_routes``)::

    POST {base}/telephony/{provider}/answer/{call_id}   call connected  -> greeting
    POST {base}/telephony/{provider}/turn/{call_id}     caller spoke / silence -> next reply
    POST {base}/telephony/{provider}/status/{call_id}   lifecycle status callback

``call_id`` is *our* ``Call.id`` so we can find the call before the provider id is known.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from callingbot.models import CallStatus

VoiceAction = Literal["gather", "hangup", "transfer"]


@dataclass
class VoiceInput:
    """Normalised inbound webhook (answer or turn)."""

    provider_call_id: str | None
    speech_text: str | None = None  # None/empty = caller said nothing (timeout)
    confidence: float | None = None
    answered_by: str | None = None  # "human" | "machine" | "unknown" (answering-machine detection)
    raw: dict = field(default_factory=dict)


@dataclass
class VoiceResponse:
    """What to do next on the call, in provider-neutral form."""

    say: list[str]  # utterances to speak, in order
    language: str  # BCP-47 code of the TTS/STT language, e.g. "en-IN"
    voice: str | None = None  # provider TTS voice name
    stt_language: str | None = None  # speech-recognition language (defaults to ``language``)
    action: VoiceAction = "gather"  # gather = listen for the reply after speaking
    transfer_to: str | None = None  # E.164 number when action == "transfer"
    gather_timeout_seconds: int = 6


@dataclass
class CallStatusUpdate:
    """Normalised status callback."""

    provider_call_id: str | None
    status: CallStatus
    duration_seconds: int | None = None
    answered_by: str | None = None
    recording_url: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class PlaceCallResult:
    provider_call_id: str
    status: CallStatus = CallStatus.INITIATED


class TelephonyError(RuntimeError):
    pass


class TelephonyProvider(ABC):
    """Adapter interface. Implementations: twilio, exotel, simulator."""

    name: str

    @abstractmethod
    def place_call(self, *, to_number: str, call_id: int) -> PlaceCallResult:
        """Start an outbound call to ``to_number`` (E.164). Raise TelephonyError on failure."""

    @abstractmethod
    def hangup(self, provider_call_id: str) -> None:
        """Terminate an in-progress call (best effort)."""

    def verify_webhook(self, *, url: str, params: Mapping[str, str], headers: Mapping[str, str]) -> bool:
        """Validate that a webhook really came from the provider. Default: accept."""
        return True

    @abstractmethod
    def parse_voice_input(self, params: Mapping[str, str]) -> VoiceInput:
        """Parse an answer/turn webhook's form parameters."""

    @abstractmethod
    def parse_status(self, params: Mapping[str, str]) -> CallStatusUpdate:
        """Parse a status-callback webhook's form parameters."""

    @abstractmethod
    def render(self, response: VoiceResponse, *, call_id: int) -> tuple[str, str]:
        """Render a VoiceResponse as (body, media_type) for the provider."""

    # Helpers shared by adapters -------------------------------------------------
    @staticmethod
    def webhook_url(base_url: str, provider: str, kind: str, call_id: int) -> str:
        return f"{base_url.rstrip('/')}/telephony/{provider}/{kind}/{call_id}"

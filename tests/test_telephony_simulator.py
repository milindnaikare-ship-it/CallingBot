"""Tests for the simulator provider and the get_provider factory / package re-exports."""

from __future__ import annotations

import json
import re

import pytest

import callingbot.telephony as telephony
from callingbot.models import CallStatus
from callingbot.telephony import SimulatorProvider, VoiceResponse, get_provider
from callingbot.telephony import base as telephony_base


def test_place_call_returns_sim_id_and_records_it():
    sim = SimulatorProvider()

    result = sim.place_call(to_number="+919812345678", call_id=42)

    assert re.fullmatch(r"SIM-42-[0-9a-f]{8}", result.provider_call_id)
    assert result.status == CallStatus.INITIATED
    assert sim.placed == [
        {"to_number": "+919812345678", "call_id": 42, "provider_call_id": result.provider_call_id}
    ]


def test_place_call_ids_are_unique_for_redials():
    sim = SimulatorProvider()
    first = sim.place_call(to_number="+919812345678", call_id=1)
    second = sim.place_call(to_number="+919812345678", call_id=1)
    assert first.provider_call_id != second.provider_call_id
    assert len(sim.placed) == 2


def test_instances_do_not_share_state():
    a, b = SimulatorProvider(), SimulatorProvider()
    a.place_call(to_number="+919812345678", call_id=1)
    a.hangup("SIM-1-deadbeef")
    assert b.placed == [] and b.hung_up == []


def test_hangup_records():
    sim = SimulatorProvider()
    sim.hangup("SIM-1-deadbeef")
    assert sim.hung_up == ["SIM-1-deadbeef"]


def test_verify_webhook_accepts():
    assert SimulatorProvider().verify_webhook(
        url="http://x/telephony/simulator/turn/1", params={}, headers={}
    )


# ---------------------------------------------------------------------------- parse_voice_input
def test_parse_voice_input_twilio_fields():
    params = {
        "CallSid": "SIM-1-abc",
        "SpeechResult": "  send the link  ",
        "Confidence": "0.75",
        "AnsweredBy": "human",
    }
    vi = SimulatorProvider().parse_voice_input(params)
    assert vi.provider_call_id == "SIM-1-abc"
    assert vi.speech_text == "send the link"
    assert vi.confidence == pytest.approx(0.75)
    assert vi.answered_by == "human"
    assert vi.raw == params


@pytest.mark.parametrize(
    "answered_by,expected",
    [("machine", "machine"), ("machine_start", "machine"), ("unknown", "unknown"), (None, None)],
)
def test_parse_voice_input_answered_by(answered_by, expected):
    params = {"CallSid": "S"} if answered_by is None else {"CallSid": "S", "AnsweredBy": answered_by}
    assert SimulatorProvider().parse_voice_input(params).answered_by == expected


def test_parse_voice_input_silence():
    vi = SimulatorProvider().parse_voice_input({"CallSid": "S", "SpeechResult": "   ", "Confidence": "bad"})
    assert vi.speech_text is None
    assert vi.confidence is None


# --------------------------------------------------------------------------------- parse_status
@pytest.mark.parametrize(
    "raw,expected",
    [
        # Twilio spellings
        ("in-progress", CallStatus.IN_PROGRESS),
        ("no-answer", CallStatus.NO_ANSWER),
        ("completed", CallStatus.COMPLETED),
        ("ringing", CallStatus.RINGING),
        # Our own CallStatus values
        ("in_progress", CallStatus.IN_PROGRESS),
        ("no_answer", CallStatus.NO_ANSWER),
        ("voicemail", CallStatus.VOICEMAIL),
        ("canceled", CallStatus.CANCELED),
        ("BUSY", CallStatus.BUSY),
    ],
)
def test_parse_status_accepts_twilio_and_internal_values(raw, expected):
    assert SimulatorProvider().parse_status({"CallSid": "S", "CallStatus": raw}).status == expected


def test_parse_status_fields_and_voicemail():
    sim = SimulatorProvider()
    upd = sim.parse_status(
        {"CallSid": "S", "CallStatus": "completed", "CallDuration": "61", "AnsweredBy": "human"}
    )
    assert upd.status == CallStatus.COMPLETED
    assert upd.duration_seconds == 61
    assert upd.answered_by == "human"

    vm = sim.parse_status({"CallSid": "S", "CallStatus": "completed", "AnsweredBy": "machine"})
    assert vm.status == CallStatus.VOICEMAIL


def test_parse_status_unknown_is_failed():
    upd = SimulatorProvider().parse_status({"CallSid": "S", "CallStatus": "nope"})
    assert upd.status == CallStatus.FAILED
    assert upd.raw == {"CallSid": "S", "CallStatus": "nope"}


# --------------------------------------------------------------------------------------- render
def test_render_json():
    response = VoiceResponse(
        say=["नमस्ते!", "क्या मेरी बात रवि जी से हो रही है?"],
        language="hi-IN",
        voice="Polly.Aditi",
        action="gather",
    )

    body, media_type = SimulatorProvider().render(response, call_id=7)

    assert media_type == "application/json"
    assert "नमस्ते" in body  # ensure_ascii=False keeps Devanagari readable
    assert json.loads(body) == {
        "say": ["नमस्ते!", "क्या मेरी बात रवि जी से हो रही है?"],
        "language": "hi-IN",
        "voice": "Polly.Aditi",
        "stt_language": "hi-IN",  # defaults to language
        "action": "gather",
        "transfer_to": None,
    }


def test_render_transfer_json():
    response = VoiceResponse(
        say=["Connecting you now."],
        language="en-IN",
        stt_language="en-US",
        action="transfer",
        transfer_to="+919800000001",
    )
    payload = json.loads(SimulatorProvider().render(response, call_id=7)[0])
    assert payload["action"] == "transfer"
    assert payload["transfer_to"] == "+919800000001"
    assert payload["stt_language"] == "en-US"
    assert payload["voice"] is None


# --------------------------------------------------------------------------------- get_provider
@pytest.mark.parametrize("name", ["simulator", "Simulator", "  SIMULATOR "])
def test_get_provider_simulator(settings, name):
    provider = get_provider(name, settings)
    assert isinstance(provider, SimulatorProvider)
    assert provider.name == "simulator"


def test_get_provider_simulator_needs_no_credentials(settings):
    bare = settings.model_copy(update={"twilio_account_sid": None, "twilio_auth_token": None})
    assert isinstance(get_provider("simulator", bare), SimulatorProvider)


@pytest.mark.parametrize("name", ["plivo", "", "twilio2"])
def test_get_provider_unknown_name(settings, name):
    with pytest.raises(ValueError) as excinfo:
        get_provider(name, settings)
    assert "twilio" in str(excinfo.value) and "simulator" in str(excinfo.value)


def test_package_reexports_base_types():
    for name in (
        "CallStatusUpdate",
        "PlaceCallResult",
        "TelephonyError",
        "TelephonyProvider",
        "VoiceAction",
        "VoiceInput",
        "VoiceResponse",
    ):
        assert getattr(telephony, name) is getattr(telephony_base, name)
    assert set(telephony.PROVIDER_NAMES) == {"twilio", "exotel", "simulator"}

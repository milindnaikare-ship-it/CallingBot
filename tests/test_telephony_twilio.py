"""Tests for the Twilio adapter: REST requests, signature validation, webhook parsing and TwiML."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl

import httpx
import pytest
from starlette.datastructures import FormData

from callingbot.models import CallStatus
from callingbot.telephony import TelephonyError, TwilioProvider, VoiceResponse, get_provider
from callingbot.telephony.twilio import SPEECH_HINTS

SID = "AC00000000000000000000000000000000"
TOKEN = "test-auth-token"
TURN_URL = "https://bot.example.test/telephony/twilio/turn/42"


class Recorder:
    """httpx.MockTransport handler that records requests and replies via ``responder``."""

    def __init__(self, responder):
        self.responder = responder
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def make_provider(settings, responder=None, **overrides):
    if overrides:
        settings = settings.model_copy(update=overrides)
    recorder = Recorder(
        responder or (lambda request: httpx.Response(201, json={"sid": "CA1", "status": "queued"}))
    )
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    return TwilioProvider(settings, http_client=client), recorder


def form_pairs(request: httpx.Request) -> list[tuple[str, str]]:
    return parse_qsl(request.content.decode("utf-8"), keep_blank_values=True)


def basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# ------------------------------------------------------------------------------------ place_call
def test_place_call_request_shape(settings):
    provider, rec = make_provider(
        settings, lambda r: httpx.Response(201, json={"sid": "CA123", "status": "queued"})
    )

    result = provider.place_call(to_number="+919812345678", call_id=42)

    assert result.provider_call_id == "CA123"
    assert result.status == CallStatus.QUEUED
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "POST"
    assert str(req.url) == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls.json"
    assert req.headers["Authorization"] == basic_auth(SID, TOKEN)
    assert req.headers["Content-Type"] == "application/x-www-form-urlencoded"
    pairs = form_pairs(req)
    form = dict(pairs)
    assert form["To"] == "+919812345678"
    assert form["From"] == "+911400000000"
    assert form["Url"] == "https://bot.example.test/telephony/twilio/answer/42"
    assert form["Method"] == "POST"
    assert form["StatusCallback"] == "https://bot.example.test/telephony/twilio/status/42"
    assert form["StatusCallbackMethod"] == "POST"
    assert form["Timeout"] == "30"
    assert "MachineDetection" not in form  # answering-machine detection is off by default
    assert "Record" not in form  # recording is off by default
    # The key must repeat, once per event, in order.
    assert [v for k, v in pairs if k == "StatusCallbackEvent"] == [
        "initiated",
        "ringing",
        "answered",
        "completed",
    ]


def test_place_call_record_on_and_machine_detection_on(settings):
    provider, rec = make_provider(settings, twilio_record_calls=True, twilio_machine_detection=True)

    provider.place_call(to_number="+919812345678", call_id=1)

    form = dict(form_pairs(rec.requests[0]))
    assert form["Record"] == "true"
    assert form["MachineDetection"] == "Enable"


def test_place_call_unexpected_status_defaults_to_initiated(settings):
    provider, _ = make_provider(
        settings, lambda r: httpx.Response(201, json={"sid": "CA9", "status": "weird"})
    )
    assert provider.place_call(to_number="+919812345678", call_id=1).status == CallStatus.INITIATED


def test_place_call_api_error_includes_twilio_code_and_message_and_masks_number(settings):
    body = {
        "code": 21211,
        "message": "The 'To' number +919812345678 is not a valid phone number.",
        "more_info": "https://www.twilio.com/docs/errors/21211",
        "status": 400,
    }
    provider, _ = make_provider(settings, lambda r: httpx.Response(400, json=body))

    with pytest.raises(TelephonyError) as excinfo:
        provider.place_call(to_number="+919812345678", call_id=1)

    message = str(excinfo.value)
    assert "21211" in message
    assert "not a valid phone number" in message
    assert "400" in message
    assert "+919812345678" not in message  # PII masked
    assert "+91******5678" in message


def test_place_call_non_json_error(settings):
    provider, _ = make_provider(settings, lambda r: httpx.Response(503, text="Service Unavailable"))
    with pytest.raises(TelephonyError, match="503"):
        provider.place_call(to_number="+919812345678", call_id=1)


def test_place_call_network_error(settings):
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    provider, _ = make_provider(settings, boom)
    with pytest.raises(TelephonyError, match="connection refused"):
        provider.place_call(to_number="+919812345678", call_id=1)


def test_place_call_success_without_sid_is_an_error(settings):
    provider, _ = make_provider(settings, lambda r: httpx.Response(201, json={"status": "queued"}))
    with pytest.raises(TelephonyError, match="sid"):
        provider.place_call(to_number="+919812345678", call_id=1)


def test_place_call_without_credentials_raises_before_any_request(settings):
    provider, rec = make_provider(settings, twilio_from_number=None)
    with pytest.raises(TelephonyError, match="twilio_from_number"):
        provider.place_call(to_number="+919812345678", call_id=1)
    assert rec.requests == []


# ---------------------------------------------------------------------------------------- hangup
def test_hangup_request_shape(settings):
    provider, rec = make_provider(settings, lambda r: httpx.Response(200, json={"sid": "CA123"}))

    provider.hangup("CA123")

    req = rec.requests[0]
    assert req.method == "POST"
    assert str(req.url) == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls/CA123.json"
    assert req.headers["Authorization"] == basic_auth(SID, TOKEN)
    assert form_pairs(req) == [("Status", "completed")]


def test_hangup_swallows_api_errors(settings, caplog):
    provider, _ = make_provider(
        settings,
        lambda r: httpx.Response(
            404, json={"code": 20404, "message": "The requested resource was not found"}
        ),
    )
    with caplog.at_level(logging.WARNING, logger="callingbot.telephony.twilio"):
        provider.hangup("CA404")  # must not raise
    assert "20404" in caplog.text


def test_hangup_swallows_network_errors(settings, caplog):
    def boom(request):
        raise httpx.ReadTimeout("timed out", request=request)

    provider, _ = make_provider(settings, boom)
    with caplog.at_level(logging.WARNING, logger="callingbot.telephony.twilio"):
        provider.hangup("CA1")
    assert "timed out" in caplog.text


def test_hangup_without_call_id_makes_no_request(settings):
    provider, rec = make_provider(settings)
    provider.hangup("")
    assert rec.requests == []


# ------------------------------------------------------------------------------ verify_webhook
WEBHOOK_PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "AccountSid": SID,
    "SpeechResult": "Yes, send me the link on WhatsApp",
    "Confidence": "0.92",
    "From": "+911400000000",
}


def documented_signature(token: str, data: str) -> str:
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def known_good_signature() -> str:
    # Built by hand from Twilio's documented algorithm: URL, then name+value for every POST
    # parameter sorted by name - written out explicitly so it is independent of the adapter code.
    data = (
        TURN_URL
        + "AccountSid"
        + SID
        + "CallSid"
        + "CA1234567890ABCDE"
        + "Confidence"
        + "0.92"
        + "From"
        + "+911400000000"
        + "SpeechResult"
        + "Yes, send me the link on WhatsApp"
    )
    return documented_signature(TOKEN, data)


def test_verify_webhook_accepts_known_good_signature(settings):
    provider, _ = make_provider(settings)
    headers = {"X-Twilio-Signature": known_good_signature()}
    assert provider.verify_webhook(url=TURN_URL, params=WEBHOOK_PARAMS, headers=headers)


def test_verify_webhook_header_lookup_is_case_insensitive(settings):
    provider, _ = make_provider(settings)
    headers = {"x-twilio-signature": known_good_signature()}
    assert provider.verify_webhook(url=TURN_URL, params=WEBHOOK_PARAMS, headers=headers)


def test_verify_webhook_accepts_starlette_form_data(settings):
    provider, _ = make_provider(settings)
    form = FormData(list(WEBHOOK_PARAMS.items()))
    assert provider.verify_webhook(
        url=TURN_URL, params=form, headers={"X-Twilio-Signature": known_good_signature()}
    )


def test_verify_webhook_rejects_tampered_param(settings):
    provider, _ = make_provider(settings)
    tampered = {**WEBHOOK_PARAMS, "SpeechResult": "Please add me to the do not call list"}
    headers = {"X-Twilio-Signature": known_good_signature()}
    assert not provider.verify_webhook(url=TURN_URL, params=tampered, headers=headers)


def test_verify_webhook_rejects_added_param(settings):
    provider, _ = make_provider(settings)
    extra = {**WEBHOOK_PARAMS, "Digits": "1"}
    headers = {"X-Twilio-Signature": known_good_signature()}
    assert not provider.verify_webhook(url=TURN_URL, params=extra, headers=headers)


def test_verify_webhook_rejects_tampered_url(settings):
    provider, _ = make_provider(settings)
    headers = {"X-Twilio-Signature": known_good_signature()}
    other_url = "https://bot.example.test/telephony/twilio/turn/43"
    assert not provider.verify_webhook(url=other_url, params=WEBHOOK_PARAMS, headers=headers)


def test_verify_webhook_rejects_signature_from_other_token(settings):
    provider, _ = make_provider(settings)
    data = TURN_URL + "".join(k + v for k, v in sorted(WEBHOOK_PARAMS.items()))
    headers = {"X-Twilio-Signature": documented_signature("someone-elses-token", data)}
    assert not provider.verify_webhook(url=TURN_URL, params=WEBHOOK_PARAMS, headers=headers)


def test_verify_webhook_missing_header_is_rejected(settings):
    provider, _ = make_provider(settings)
    assert not provider.verify_webhook(url=TURN_URL, params=WEBHOOK_PARAMS, headers={})


def test_verify_webhook_missing_auth_token_is_rejected(settings):
    provider, _ = make_provider(settings, twilio_auth_token=None)
    headers = {"X-Twilio-Signature": known_good_signature()}
    assert not provider.verify_webhook(url=TURN_URL, params=WEBHOOK_PARAMS, headers=headers)


def test_verify_webhook_disabled_accepts_anything(settings):
    provider, _ = make_provider(settings, twilio_validate_signature=False)
    assert provider.verify_webhook(url=TURN_URL, params=WEBHOOK_PARAMS, headers={})


def test_verify_webhook_repeated_params_use_all_values_sorted(settings):
    provider, _ = make_provider(settings)
    url = "https://bot.example.test/telephony/twilio/status/42"
    # Repeated name: every value contributes, values sorted ("answered" < "ringing").
    data = url + "CallSid" + "CA1" + "Event" + "answered" + "Event" + "ringing"
    headers = {"X-Twilio-Signature": documented_signature(TOKEN, data)}

    form = FormData([("Event", "ringing"), ("CallSid", "CA1"), ("Event", "answered")])
    assert provider.verify_webhook(url=url, params=form, headers=headers)
    # Same data as a dict of lists (e.g. urllib.parse.parse_qs output).
    assert provider.verify_webhook(
        url=url, params={"Event": ["ringing", "answered"], "CallSid": "CA1"}, headers=headers
    )
    # Dropping one of the repeated values breaks the signature.
    assert not provider.verify_webhook(
        url=url, params={"Event": "ringing", "CallSid": "CA1"}, headers=headers
    )


def test_verify_webhook_tolerates_default_port_difference(settings):
    provider, _ = make_provider(settings)
    signed_with_port = TURN_URL.replace("bot.example.test", "bot.example.test:443")
    data = signed_with_port + "".join(k + v for k, v in sorted(WEBHOOK_PARAMS.items()))
    headers = {"X-Twilio-Signature": documented_signature(TOKEN, data)}
    assert provider.verify_webhook(url=TURN_URL, params=WEBHOOK_PARAMS, headers=headers)
    # ... but not a non-default port.
    data_8443 = TURN_URL.replace("bot.example.test", "bot.example.test:8443") + "".join(
        k + v for k, v in sorted(WEBHOOK_PARAMS.items())
    )
    headers = {"X-Twilio-Signature": documented_signature(TOKEN, data_8443)}
    assert not provider.verify_webhook(url=TURN_URL, params=WEBHOOK_PARAMS, headers=headers)


# ---------------------------------------------------------------------------- parse_voice_input
def test_parse_voice_input_full(settings):
    provider, _ = make_provider(settings)
    params = {
        "CallSid": "CA1",
        "SpeechResult": "  Haan, link bhej dijiye  ",
        "Confidence": "0.87",
        "AnsweredBy": "human",
    }
    vi = provider.parse_voice_input(params)
    assert vi.provider_call_id == "CA1"
    assert vi.speech_text == "Haan, link bhej dijiye"
    assert vi.confidence == pytest.approx(0.87)
    assert vi.answered_by == "human"
    assert vi.raw == params


@pytest.mark.parametrize("speech", ["", "   ", None])
def test_parse_voice_input_silence_is_none(settings, speech):
    provider, _ = make_provider(settings)
    params = {"CallSid": "CA1"} if speech is None else {"CallSid": "CA1", "SpeechResult": speech}
    vi = provider.parse_voice_input(params)
    assert vi.speech_text is None
    assert vi.confidence is None


@pytest.mark.parametrize("confidence", ["abc", "nan", "inf", ""])
def test_parse_voice_input_bad_confidence_is_none(settings, confidence):
    provider, _ = make_provider(settings)
    assert provider.parse_voice_input({"CallSid": "CA1", "Confidence": confidence}).confidence is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("human", "human"),
        ("machine_start", "machine"),
        ("machine_end_beep", "machine"),
        ("machine_end_silence", "machine"),
        ("machine_end_other", "machine"),
        ("fax", "fax"),
        ("unknown", "unknown"),
        ("something_new", "unknown"),
        (None, None),  # AMD disabled / not reported
        ("", None),
    ],
)
def test_parse_voice_input_answered_by_normalised(settings, raw, expected):
    provider, _ = make_provider(settings)
    params = {"CallSid": "CA1"} if raw is None else {"CallSid": "CA1", "AnsweredBy": raw}
    assert provider.parse_voice_input(params).answered_by == expected


# --------------------------------------------------------------------------------- parse_status
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("queued", CallStatus.QUEUED),
        ("initiated", CallStatus.INITIATED),
        ("ringing", CallStatus.RINGING),
        ("in-progress", CallStatus.IN_PROGRESS),
        ("completed", CallStatus.COMPLETED),
        ("busy", CallStatus.BUSY),
        ("failed", CallStatus.FAILED),
        ("no-answer", CallStatus.NO_ANSWER),
        ("canceled", CallStatus.CANCELED),
    ],
)
def test_parse_status_mapping(settings, raw, expected):
    provider, _ = make_provider(settings)
    assert provider.parse_status({"CallSid": "CA1", "CallStatus": raw}).status == expected


def test_parse_status_completed_fields(settings):
    provider, _ = make_provider(settings)
    params = {
        "CallSid": "CA1",
        "CallStatus": "completed",
        "CallDuration": "73",
        "AnsweredBy": "human",
        "RecordingUrl": "https://api.twilio.com/2010-04-01/Accounts/AC0/Recordings/RE1",
    }
    upd = provider.parse_status(params)
    assert upd.provider_call_id == "CA1"
    assert upd.status == CallStatus.COMPLETED
    assert upd.duration_seconds == 73
    assert upd.answered_by == "human"
    assert upd.recording_url == params["RecordingUrl"]
    assert upd.raw == params


@pytest.mark.parametrize("answered_by", ["machine_start", "machine_end_beep", "machine_end_silence"])
def test_parse_status_machine_completed_is_voicemail(settings, answered_by):
    provider, _ = make_provider(settings)
    upd = provider.parse_status({"CallSid": "CA1", "CallStatus": "completed", "AnsweredBy": answered_by})
    assert upd.status == CallStatus.VOICEMAIL
    assert upd.answered_by == "machine"


def test_parse_status_machine_on_non_completed_keeps_status(settings):
    provider, _ = make_provider(settings)
    upd = provider.parse_status(
        {"CallSid": "CA1", "CallStatus": "in-progress", "AnsweredBy": "machine_start"}
    )
    assert upd.status == CallStatus.IN_PROGRESS


def test_parse_status_unknown_maps_to_failed_and_keeps_raw(settings, caplog):
    provider, _ = make_provider(settings)
    with caplog.at_level(logging.WARNING, logger="callingbot.telephony.twilio"):
        upd = provider.parse_status({"CallSid": "CA1", "CallStatus": "teleported"})
    assert upd.status == CallStatus.FAILED
    assert upd.raw["CallStatus"] == "teleported"
    assert "teleported" in caplog.text


@pytest.mark.parametrize("duration", [None, "", "abc", "-5"])
def test_parse_status_bad_duration_is_none(settings, duration):
    provider, _ = make_provider(settings)
    params = {"CallSid": "CA1", "CallStatus": "busy"}
    if duration is not None:
        params["CallDuration"] = duration
    upd = provider.parse_status(params)
    assert upd.duration_seconds is None
    assert upd.recording_url is None


# --------------------------------------------------------------------------------------- render
def parse_twiml(body: str) -> ET.Element:
    assert body.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    return ET.fromstring(body.encode("utf-8"))


def test_render_gather_structure(settings):
    provider, _ = make_provider(settings)
    response = VoiceResponse(
        say=["Hello, this is Asha.", "Am I speaking with Ravi?"],
        language="en-IN",
        voice="Polly.Aditi",
        gather_timeout_seconds=7,
    )

    body, media_type = provider.render(response, call_id=42)

    assert media_type == "application/xml"
    root = parse_twiml(body)
    assert root.tag == "Response"
    assert [child.tag for child in root] == ["Gather", "Redirect"]
    gather, redirect = list(root)
    assert gather.attrib == {
        "input": "speech",
        "action": TURN_URL,
        "method": "POST",
        "language": "en-IN",
        "speechTimeout": "auto",
        "timeout": "7",
        "actionOnEmptyResult": "true",
        "hints": SPEECH_HINTS,
    }
    says = list(gather)
    assert [s.tag for s in says] == ["Say", "Say"]
    assert [s.text for s in says] == ["Hello, this is Asha.", "Am I speaking with Ravi?"]
    assert all(s.attrib == {"voice": "Polly.Aditi", "language": "en-IN"} for s in says)
    assert redirect.attrib == {"method": "POST"}
    assert redirect.text == TURN_URL


def test_render_gather_uses_stt_language_and_omits_missing_voice(settings):
    provider, _ = make_provider(settings)
    response = VoiceResponse(say=["Namaste"], language="hi-IN", voice=None, stt_language="en-IN")

    root = parse_twiml(provider.render(response, call_id=42)[0])

    gather = root.find("Gather")
    assert gather.get("language") == "en-IN"
    say = gather.find("Say")
    assert "voice" not in say.attrib
    assert say.get("language") == "hi-IN"


def test_render_hangup(settings):
    provider, _ = make_provider(settings)
    response = VoiceResponse(say=["Thank you for your time.", "Goodbye."], language="en-IN", action="hangup")

    root = parse_twiml(provider.render(response, call_id=42)[0])

    assert [c.tag for c in root] == ["Say", "Say", "Hangup"]
    assert root.find("Gather") is None


def test_render_transfer(settings):
    provider, _ = make_provider(settings)
    response = VoiceResponse(
        say=["Connecting you to our relationship manager."],
        language="en-IN",
        voice="Polly.Aditi",
        action="transfer",
        transfer_to="+919800000001",
    )

    root = parse_twiml(provider.render(response, call_id=42)[0])

    assert [c.tag for c in root] == ["Say", "Dial", "Say", "Hangup"]
    dial = root.find("Dial")
    assert dial.attrib == {"timeout": "25", "callerId": "+911400000000"}
    assert dial.text == "+919800000001"
    fallback = list(root)[2]
    assert fallback.text and fallback.get("language") == "en-IN"


def test_render_transfer_hindi_fallback(settings):
    provider, _ = make_provider(settings)
    response = VoiceResponse(
        say=["जोड़ रही हूँ"], language="hi-IN", action="transfer", transfer_to="+919800000001"
    )
    root = parse_twiml(provider.render(response, call_id=42)[0])
    fallback = list(root)[2]
    assert fallback.get("language") == "hi-IN"
    assert any("ऀ" <= ch <= "ॿ" for ch in fallback.text)


def test_render_transfer_without_number_still_hangs_up(settings):
    provider, _ = make_provider(settings)
    response = VoiceResponse(say=["One moment."], language="en-IN", action="transfer", transfer_to=None)
    root = parse_twiml(provider.render(response, call_id=42)[0])
    assert root.find("Dial") is None
    assert list(root)[-1].tag == "Hangup"


def test_render_escapes_text_and_attributes(settings):
    provider, _ = make_provider(settings)
    nasty = "Terms & conditions <apply> \"quoted\" 'single'"
    response = VoiceResponse(say=[nasty], language="en-IN", voice='Polly."Aditi"&<x>')

    body, _ = provider.render(response, call_id=42)

    assert "&amp;" in body and "&lt;apply&gt;" in body
    assert "<apply>" not in body
    assert 'voice="Polly.&quot;Aditi&quot;&amp;&lt;x&gt;"' in body
    say = parse_twiml(body).find("Gather/Say")
    assert say.text == nasty
    assert say.get("voice") == 'Polly."Aditi"&<x>'


def test_render_devanagari_survives(settings):
    provider, _ = make_provider(settings)
    text = "नमस्ते! क्या मेरी बात रवि जी से हो रही है?"
    response = VoiceResponse(say=[text], language="hi-IN", voice="Polly.Aditi")

    body, _ = provider.render(response, call_id=42)

    assert text in body  # literal UTF-8, not character references
    assert parse_twiml(body).find("Gather/Say").text == text


def test_render_skips_blank_utterances_and_strips_illegal_xml_chars(settings):
    provider, _ = make_provider(settings)
    response = VoiceResponse(say=["", "   ", "Hello\x00 there\x0b"], language="en-IN")
    root = parse_twiml(provider.render(response, call_id=42)[0])
    says = root.findall("Gather/Say")
    assert [s.text for s in says] == ["Hello there"]


# --------------------------------------------------------------------------------- get_provider
def test_get_provider_twilio(settings):
    provider = get_provider("twilio", settings)
    assert isinstance(provider, TwilioProvider)
    assert provider.name == "twilio"


def test_get_provider_twilio_missing_credentials_names_them(settings):
    incomplete = settings.model_copy(update={"twilio_auth_token": None, "twilio_from_number": "  "})
    with pytest.raises(ValueError) as excinfo:
        get_provider("twilio", incomplete)
    message = str(excinfo.value)
    assert "twilio_auth_token" in message and "TWILIO_AUTH_TOKEN" in message
    assert "twilio_from_number" in message
    assert "twilio_account_sid" not in message


def test_get_provider_warns_about_localhost_base_url(settings, caplog):
    local = settings.model_copy(update={"public_base_url": "http://localhost:8000"})
    with caplog.at_level(logging.WARNING, logger="callingbot.telephony"):
        get_provider("twilio", local)
    assert "PUBLIC_BASE_URL" in caplog.text

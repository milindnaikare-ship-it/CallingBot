"""Tests for the Exotel adapter: connect.json request, errors, status mapping and Phase-3 stubs."""

from __future__ import annotations

import base64
import logging
from urllib.parse import parse_qsl

import httpx
import pytest

from callingbot.models import CallStatus
from callingbot.telephony import ExotelProvider, TelephonyError, VoiceResponse, get_provider

EXOTEL = {
    "exotel_account_sid": "acme1",
    "exotel_api_key": "exo-key",
    "exotel_api_token": "exo-token",
    "exotel_caller_id": "01401234567",
    "exotel_app_id": "123456",
}


@pytest.fixture
def exotel_settings(settings):
    return settings.model_copy(update=EXOTEL)


class Recorder:
    def __init__(self, responder):
        self.responder = responder
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"Call": {"Sid": "b6cfaf0a2f1b4c1e", "Status": "in-progress"}})


def make_provider(settings, responder=ok_response):
    recorder = Recorder(responder)
    return ExotelProvider(
        settings, http_client=httpx.Client(transport=httpx.MockTransport(recorder))
    ), recorder


# ------------------------------------------------------------------------------------ place_call
def test_place_call_request_shape(exotel_settings):
    provider, rec = make_provider(exotel_settings)

    result = provider.place_call(to_number="+919812345678", call_id=42)

    assert result.provider_call_id == "b6cfaf0a2f1b4c1e"
    # Exotel's "in-progress" right after connect means "dialling", not "answered".
    assert result.status == CallStatus.INITIATED
    req = rec.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.exotel.com/v1/Accounts/acme1/Calls/connect.json"
    assert req.headers["Authorization"] == "Basic " + base64.b64encode(b"exo-key:exo-token").decode()
    assert parse_qsl(req.content.decode("utf-8")) == [
        ("From", "+919812345678"),
        ("CallerId", "01401234567"),
        ("Url", "http://my.exotel.com/acme1/exoml/start_voice/123456"),
        ("CallType", "trans"),
        ("StatusCallback", "https://bot.example.test/telephony/exotel/status/42"),
        ("StatusCallbackEvents[0]", "terminal"),
        ("StatusCallbackContentType", "multipart/form-data"),
        ("CustomField", "42"),
    ]


def test_place_call_queued_status_is_kept(exotel_settings):
    provider, _ = make_provider(
        exotel_settings, lambda r: httpx.Response(200, json={"Call": {"Sid": "S1", "Status": "queued"}})
    )
    assert provider.place_call(to_number="+919812345678", call_id=1).status == CallStatus.QUEUED


def test_place_call_subdomain_with_scheme_is_normalised(exotel_settings):
    settings = exotel_settings.model_copy(update={"exotel_subdomain": "https://api.in.exotel.com/"})
    provider, rec = make_provider(settings)
    provider.place_call(to_number="+919812345678", call_id=1)
    assert str(rec.requests[0].url) == "https://api.in.exotel.com/v1/Accounts/acme1/Calls/connect.json"


def test_place_call_rest_exception(exotel_settings):
    body = {
        "RestException": {"Status": 400, "Code": 34001, "Message": "From number +919812345678 is invalid"}
    }
    provider, _ = make_provider(exotel_settings, lambda r: httpx.Response(400, json=body))

    with pytest.raises(TelephonyError) as excinfo:
        provider.place_call(to_number="+919812345678", call_id=1)

    message = str(excinfo.value)
    assert "34001" in message and "is invalid" in message and "400" in message
    assert "+919812345678" not in message


def test_place_call_rest_exception_with_2xx_is_still_an_error(exotel_settings):
    body = {"RestException": {"Status": 403, "Message": "Account suspended"}}
    provider, _ = make_provider(exotel_settings, lambda r: httpx.Response(200, json=body))
    with pytest.raises(TelephonyError, match="Account suspended"):
        provider.place_call(to_number="+919812345678", call_id=1)


def test_place_call_non_json_error(exotel_settings):
    provider, _ = make_provider(exotel_settings, lambda r: httpx.Response(401, text="Unauthorized"))
    with pytest.raises(TelephonyError, match="401"):
        provider.place_call(to_number="+919812345678", call_id=1)


def test_place_call_network_error(exotel_settings):
    def boom(request):
        raise httpx.ConnectTimeout("connect timed out", request=request)

    provider, _ = make_provider(exotel_settings, boom)
    with pytest.raises(TelephonyError, match="connect timed out"):
        provider.place_call(to_number="+919812345678", call_id=1)


def test_place_call_missing_sid(exotel_settings):
    provider, _ = make_provider(exotel_settings, lambda r: httpx.Response(200, json={"Call": {}}))
    with pytest.raises(TelephonyError, match="Sid"):
        provider.place_call(to_number="+919812345678", call_id=1)


def test_place_call_without_credentials_makes_no_request(exotel_settings):
    provider, rec = make_provider(exotel_settings.model_copy(update={"exotel_app_id": None}))
    with pytest.raises(TelephonyError, match="exotel_app_id"):
        provider.place_call(to_number="+919812345678", call_id=1)
    assert rec.requests == []


# ---------------------------------------------------------------------------------------- hangup
def test_hangup_is_a_logged_noop(exotel_settings, caplog):
    def must_not_call(request):
        raise AssertionError("hangup must not call the Exotel API")

    provider, rec = make_provider(exotel_settings, must_not_call)
    with caplog.at_level(logging.INFO, logger="callingbot.telephony.exotel"):
        provider.hangup("b6cfaf0a2f1b4c1e")
    assert rec.requests == []
    assert "not supported" in caplog.text


# --------------------------------------------------------------------------------- parse_status
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("completed", CallStatus.COMPLETED),
        ("failed", CallStatus.FAILED),
        ("busy", CallStatus.BUSY),
        ("no-answer", CallStatus.NO_ANSWER),
        ("canceled", CallStatus.CANCELED),
        ("cancelled", CallStatus.CANCELED),
        ("in-progress", CallStatus.IN_PROGRESS),
        ("ringing", CallStatus.RINGING),
        ("queued", CallStatus.QUEUED),
        ("Completed", CallStatus.COMPLETED),
    ],
)
def test_parse_status_mapping(exotel_settings, raw, expected):
    provider, _ = make_provider(exotel_settings)
    assert provider.parse_status({"CallSid": "S1", "Status": raw}).status == expected


def test_parse_status_fields(exotel_settings):
    provider, _ = make_provider(exotel_settings)
    params = {
        "CallSid": "S1",
        "Status": "completed",
        "EventType": "terminal",
        "ConversationDuration": "95",
        "Duration": "120",
        "RecordingUrl": "https://recordings.exotel.com/exotelrecordings/acme1/S1.mp3",
        "CustomField": "42",
    }
    upd = provider.parse_status(params)
    assert upd.provider_call_id == "S1"
    assert upd.status == CallStatus.COMPLETED
    assert upd.duration_seconds == 95  # ConversationDuration wins over Duration
    assert upd.recording_url == params["RecordingUrl"]
    assert upd.answered_by is None
    assert upd.raw == params


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"Duration": "33"}, 33),
        ({"DialCallDuration": "12"}, 12),
        ({"ConversationDuration": "", "Duration": "7"}, 7),
        ({"ConversationDuration": "x", "DialCallDuration": "4"}, 4),
        ({}, None),
    ],
)
def test_parse_status_duration_fallbacks(exotel_settings, fields, expected):
    provider, _ = make_provider(exotel_settings)
    upd = provider.parse_status({"CallSid": "S1", "Status": "completed", **fields})
    assert upd.duration_seconds == expected


def test_parse_status_unknown_maps_to_failed(exotel_settings, caplog):
    provider, _ = make_provider(exotel_settings)
    with caplog.at_level(logging.WARNING, logger="callingbot.telephony.exotel"):
        upd = provider.parse_status({"CallSid": "S1", "Status": "mystery"})
    assert upd.status == CallStatus.FAILED
    assert upd.raw["Status"] == "mystery"
    assert "mystery" in caplog.text


# ---------------------------------------------------------------------------- parse_voice_input
def test_parse_voice_input_speech(exotel_settings):
    provider, _ = make_provider(exotel_settings)
    vi = provider.parse_voice_input({"CallSid": "S1", "SpeechResult": "  yes please  ", "Confidence": "0.8"})
    assert vi.provider_call_id == "S1"
    assert vi.speech_text == "yes please"
    assert vi.confidence == pytest.approx(0.8)
    assert vi.answered_by is None


def test_parse_voice_input_quoted_digits(exotel_settings):
    provider, _ = make_provider(exotel_settings)
    vi = provider.parse_voice_input({"CallSid": "S1", "digits": '"1"'})
    assert vi.speech_text == "1"


def test_parse_voice_input_nothing(exotel_settings):
    provider, _ = make_provider(exotel_settings)
    vi = provider.parse_voice_input({"CallSid": "S1", "digits": '""'})
    assert vi.speech_text is None
    assert vi.confidence is None


# ------------------------------------------------------------------------- render / verify_webhook
def test_render_not_implemented_points_to_plan(exotel_settings):
    provider, _ = make_provider(exotel_settings)
    with pytest.raises(NotImplementedError, match="DEVELOPMENT_PLAN"):
        provider.render(VoiceResponse(say=["Hello"], language="en-IN"), call_id=1)


def test_verify_webhook_accepts(exotel_settings):
    provider, _ = make_provider(exotel_settings)
    assert provider.verify_webhook(
        url="https://bot.example.test/telephony/exotel/status/1", params={}, headers={}
    )


# --------------------------------------------------------------------------------- get_provider
def test_get_provider_exotel(exotel_settings):
    provider = get_provider("exotel", exotel_settings)
    assert isinstance(provider, ExotelProvider)
    assert provider.name == "exotel"


def test_get_provider_exotel_missing_credentials_names_them(settings):
    partial = settings.model_copy(update={"exotel_account_sid": "acme1", "exotel_api_key": "k"})
    with pytest.raises(ValueError) as excinfo:
        get_provider("exotel", partial)
    message = str(excinfo.value)
    for name in ("exotel_api_token", "exotel_caller_id", "exotel_app_id"):
        assert name in message and name.upper() in message
    assert "exotel_account_sid" not in message
    assert "exotel_api_key" not in message

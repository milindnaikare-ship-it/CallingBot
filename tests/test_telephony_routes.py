"""Tests for callingbot.web.telephony_routes: Twilio webhooks end to end (no network)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import xml.etree.ElementTree as ET

import httpx
import pytest
from conftest import IN_WINDOW_UTC
from fastapi.testclient import TestClient
from sqlalchemy import select

from callingbot import compliance, db
from callingbot.agent.demo_llm import DemoLLM
from callingbot.agent.engine import ConversationEngine, script
from callingbot.agent.llm import ScriptedLLM, text_reply
from callingbot.models import (
    AuditEvent,
    Call,
    Callback,
    CallOutcome,
    CallStatus,
    Campaign,
    CampaignContact,
    CampaignStatus,
    ContactState,
    Distributor,
    EmpanelmentStatus,
    OutboundMessage,
    Turn,
    TurnRole,
)
from callingbot.services import lifecycle
from callingbot.telephony import SimulatorProvider, TwilioProvider
from callingbot.web.app import create_app


def twilio_signature(token: str, url: str, params: dict[str, str]) -> str:
    """Independent implementation of Twilio's algorithm: base64(HMAC-SHA1(url + sorted name+value))."""
    data = url + "".join(name + params[name] for name in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected HTTP request to {request.url}")


@pytest.fixture
def make_client(settings, kb):
    def _make(*, llm=None, provider=None, settings_=None) -> TestClient:
        s = settings_ or settings
        if provider is None:
            provider = TwilioProvider(s, http_client=httpx.Client(transport=httpx.MockTransport(_no_network)))
        app = create_app(s, llm=llm or DemoLLM(kb), provider=provider)
        app.state.clock = lambda: IN_WINDOW_UTC
        return TestClient(app)

    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture
def new_call():
    """Create a distributor + Twilio call (optionally in an active campaign); returns the call id."""

    def _make(*, provider="twilio", sid="CA0001", campaign=False, **distributor_kw) -> int:
        with db.new_session() as s:
            data = {"arn": "ARN-555001", "name": "Ravi Kumar", "phone": "+919812345678", "city": "Pune"}
            data.update(distributor_kw)
            d = Distributor(status=EmpanelmentStatus.NEW, **data)
            s.add(d)
            s.flush()
            contact = None
            if campaign:
                c = Campaign(name="NFO Launch", status=CampaignStatus.ACTIVE)
                s.add(c)
                s.flush()
                contact = CampaignContact(
                    campaign_id=c.id, distributor_id=d.id, state=ContactState.IN_PROGRESS, attempts=1
                )
                s.add(contact)
                s.flush()
            call = lifecycle.create_call(s, distributor=d, provider=provider, contact=contact)
            call.status = CallStatus.INITIATED
            call.provider_call_id = sid
            s.commit()
            return call.id

    return _make


def post(client: TestClient, settings, path: str, params: dict[str, str], *, signature: str | None = None):
    url = settings.base_url + path
    sig = signature if signature is not None else twilio_signature(settings.twilio_auth_token, url, params)
    return client.post(path, data=params, headers={"X-Twilio-Signature": sig})


def answer(client, settings, call_id: int, sid: str = "CA0001", **extra):
    params = {"CallSid": sid, "CallStatus": "in-progress", "AnsweredBy": "human", **extra}
    return post(client, settings, f"/telephony/twilio/answer/{call_id}", params)


def turn(client, settings, call_id: int, speech: str | None, sid: str = "CA0001"):
    params = {"CallSid": sid}
    if speech is not None:
        params.update({"SpeechResult": speech, "Confidence": "0.92"})
    return post(client, settings, f"/telephony/twilio/turn/{call_id}", params)


def twiml(response) -> ET.Element:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/xml")
    return ET.fromstring(response.content)


def says(element: ET.Element) -> list[str]:
    return [s.text or "" for s in element.iter("Say")]


def load_call(call_id: int) -> Call:
    with db.new_session() as s:
        call = s.get(Call, call_id)
        _ = call.distributor  # load while the session is open
        return call


def audits(kind: str) -> list[AuditEvent]:
    with db.new_session() as s:
        return list(s.scalars(select(AuditEvent).where(AuditEvent.kind == kind)))


# ------------------------------------------------------------------------------------- answer / turn


def test_answer_returns_greeting_inside_gather(client, settings, new_call):
    call_id = new_call()
    root = twiml(answer(client, settings, call_id))

    gather = root.find("Gather")
    assert gather is not None and gather.get("input") == "speech"
    assert gather.get("action") == f"{settings.base_url}/telephony/twilio/turn/{call_id}"
    greeting = says(gather)
    assert len(greeting) == 1 and "virtual assistant" in greeting[0] and "Ravi Kumar" in greeting[0]
    assert root.find("Hangup") is None

    call = load_call(call_id)
    assert call.status == CallStatus.IN_PROGRESS and call.answered_by == "human"
    assert len(audits("disclosure_played")) == 1


def test_answer_records_provider_call_id_when_missing(client, settings, new_call):
    call_id = new_call(sid=None)
    twiml(answer(client, settings, call_id, sid="CA-NEW"))
    assert load_call(call_id).provider_call_id == "CA-NEW"


def test_turn_with_speech_returns_next_utterance(client, settings, kb, new_call):
    call_id = new_call()
    twiml(answer(client, settings, call_id))
    root = twiml(turn(client, settings, call_id, "yes speaking"))

    gather = root.find("Gather")
    assert gather is not None
    reply = " ".join(says(gather))
    assert kb.nfo.scheme_name in reply and "Are you already empanelled with us?" in reply

    with db.new_session() as s:
        heard = s.scalars(select(Turn).where(Turn.call_id == call_id, Turn.role == TurnRole.DISTRIBUTOR)).all()
    assert [t.text for t in heard] == ["yes speaking"] and heard[0].meta == {"confidence": 0.92}


def test_turn_silence_reprompts(client, settings, new_call):
    call_id = new_call()
    twiml(answer(client, settings, call_id))
    root = twiml(turn(client, settings, call_id, None))
    assert root.find("Gather") is not None
    assert says(root) == [script("reprompt", "en-IN")]


def test_full_call_then_completed_status_finalises_everything(client, settings, new_call):
    call_id = new_call(campaign=True)
    twiml(answer(client, settings, call_id))
    twiml(turn(client, settings, call_id, "yes speaking"))
    twiml(turn(client, settings, call_id, "no I'm not empanelled"))
    root = twiml(turn(client, settings, call_id, "yes send it on whatsapp"))
    assert root.find("Gather") is None and root.find("Hangup") is not None

    status_params = {"CallSid": "CA0001", "CallStatus": "completed", "CallDuration": "95"}
    response = post(client, settings, f"/telephony/twilio/status/{call_id}", status_params)
    assert response.status_code == 204 and response.content == b""

    with db.new_session() as s:
        call = s.get(Call, call_id)
        contact = s.get(CampaignContact, call.contact_id)
        assert call.status == CallStatus.COMPLETED and call.duration_seconds == 95
        assert call.outcome == CallOutcome.LINK_SENT and call.engine_state["finalized"] is True
        assert call.distributor.status == EmpanelmentStatus.LINK_SENT
        assert contact.state == ContactState.DONE and contact.final_outcome == CallOutcome.LINK_SENT
        assert s.scalars(select(OutboundMessage)).one().call_id == call_id

    # A repeated callback is harmless (idempotent finalisation).
    assert post(client, settings, f"/telephony/twilio/status/{call_id}", status_params).status_code == 204
    assert len(audits("call_finalized")) == 1


def test_unanswered_status_schedules_retry(client, settings, new_call):
    call_id = new_call(campaign=True)
    params = {"CallSid": "CA0001", "CallStatus": "no-answer"}
    assert post(client, settings, f"/telephony/twilio/status/{call_id}", params).status_code == 204
    with db.new_session() as s:
        call = s.get(Call, call_id)
        contact = s.get(CampaignContact, call.contact_id)
        assert call.status == CallStatus.NO_ANSWER and call.outcome == CallOutcome.NO_OUTCOME
        assert contact.state == ContactState.PENDING and contact.next_attempt_at > IN_WINDOW_UTC


def test_intermediate_status_moves_call_forward(client, settings, new_call):
    call_id = new_call()
    params = {"CallSid": "CA0001", "CallStatus": "ringing"}
    assert post(client, settings, f"/telephony/twilio/status/{call_id}", params).status_code == 204
    assert load_call(call_id).status == CallStatus.RINGING


# ------------------------------------------------------------------------------------- authentication


@pytest.mark.parametrize("kind", ["answer", "turn", "status"])
def test_invalid_signature_is_rejected(client, settings, new_call, kind):
    call_id = new_call()
    params = {"CallSid": "CA0001", "CallStatus": "completed", "SpeechResult": "hello"}
    response = post(client, settings, f"/telephony/twilio/{kind}/{call_id}", params, signature="bm9wZQ==")
    assert response.status_code == 403
    call = load_call(call_id)
    assert call.status == CallStatus.INITIATED and not call.llm_messages


def test_missing_or_tampered_signature_is_rejected(client, settings, new_call):
    call_id = new_call()
    path = f"/telephony/twilio/answer/{call_id}"
    params = {"CallSid": "CA0001", "AnsweredBy": "human"}
    assert client.post(path, data=params).status_code == 403
    good = twilio_signature(settings.twilio_auth_token, settings.base_url + path, params)
    tampered = {**params, "AnsweredBy": "machine_start"}
    assert client.post(path, data=tampered, headers={"X-Twilio-Signature": good}).status_code == 403
    # Signed over the internal URL instead of PUBLIC_BASE_URL: rejected too.
    internal = twilio_signature(settings.twilio_auth_token, "http://testserver" + path, params)
    assert client.post(path, data=params, headers={"X-Twilio-Signature": internal}).status_code == 403


def test_signature_covers_the_query_string(client, settings, new_call):
    call_id = new_call()
    path = f"/telephony/twilio/answer/{call_id}?attempt=2"
    params = {"CallSid": "CA0001", "AnsweredBy": "human"}
    sig = twilio_signature(settings.twilio_auth_token, settings.base_url + path, params)
    assert client.post(path, data=params, headers={"X-Twilio-Signature": sig}).status_code == 200
    unsigned_query = twilio_signature(
        settings.twilio_auth_token, f"{settings.base_url}/telephony/twilio/answer/{call_id}", params
    )
    assert client.post(path, data=params, headers={"X-Twilio-Signature": unsigned_query}).status_code == 403


@pytest.mark.parametrize("provider", ["exotel", "simulator", "Twilio", "unknown"])
def test_wrong_provider_name_is_404(client, settings, new_call, provider):
    call_id = new_call()
    response = client.post(f"/telephony/{provider}/answer/{call_id}", data={"CallSid": "CA0001"})
    assert response.status_code == 404


def test_simulator_webhooks_work_outside_prod_but_not_in_prod(make_client, settings, new_call):
    client = make_client(provider=SimulatorProvider())
    call_id = new_call(provider="simulator", sid=None)
    response = client.post(f"/telephony/simulator/answer/{call_id}", data={"AnsweredBy": "human"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("application/json")
    assert response.json()["action"] == "gather" and "virtual assistant" in response.json()["say"][0]

    prod = make_client(provider=SimulatorProvider(), settings_=settings.model_copy(update={"app_env": "prod"}))
    call_id = new_call(provider="simulator", sid=None)
    assert prod.post(f"/telephony/simulator/answer/{call_id}", data={}).status_code == 404
    assert prod.post(f"/telephony/simulator/status/{call_id}", data={"CallStatus": "completed"}).status_code == 404


# ------------------------------------------------------------------------------------- unknown calls


@pytest.mark.parametrize("kind", ["answer", "turn"])
def test_unknown_call_gets_apology_and_hangup(client, settings, kind):
    root = twiml(post(client, settings, f"/telephony/twilio/{kind}/999", {"CallSid": "CA9", "SpeechResult": "hi"}))
    assert root.find("Gather") is None and root.find("Hangup") is not None
    assert says(root) == ["Sorry, this call cannot be continued. Goodbye."]


def test_webhook_for_a_different_call_is_treated_as_unknown(client, settings, new_call):
    # E.g. ids reused after a database reset: never speak one distributor's details to another person.
    call_id = new_call(sid="CA0001")
    root = twiml(answer(client, settings, call_id, sid="CA-OTHER"))
    assert root.find("Hangup") is not None and "Ravi" not in " ".join(says(root))
    assert not load_call(call_id).llm_messages

    simulator_call = new_call(provider="simulator", sid=None, arn="ARN-555002", phone="+919812345679")
    root = twiml(answer(client, settings, simulator_call, sid="CA0002"))
    assert root.find("Hangup") is not None and not load_call(simulator_call).llm_messages


def test_status_for_unknown_call_returns_204(client, settings):
    response = post(client, settings, "/telephony/twilio/status/12345", {"CallSid": "CA9", "CallStatus": "completed"})
    assert response.status_code == 204


# ------------------------------------------------------------------------------------- failure paths


def test_llm_exception_apologises_and_hangs_up(make_client, settings, new_call):
    def boom(messages):
        raise RuntimeError("model endpoint exploded")

    client = make_client(llm=ScriptedLLM([boom]))
    call_id = new_call()
    twiml(answer(client, settings, call_id))
    root = twiml(turn(client, settings, call_id, "yes speaking"))

    assert root.find("Gather") is None and root.find("Hangup") is not None
    assert script("error_apology", "en-IN") in says(root)
    call = load_call(call_id)
    assert "exploded" in call.error and call.pending_action == "hangup"
    with db.new_session() as s:
        assert s.scalars(select(Callback).where(Callback.call_id == call_id)).one().with_rm


def test_unexpected_engine_crash_is_caught(client, settings, kb, new_call, monkeypatch):
    call_id = new_call()
    twiml(answer(client, settings, call_id))
    twiml(turn(client, settings, call_id, "yes speaking"))  # one real turn: the scheme was mentioned

    def crash(self, call, speech_text, *, confidence=None):
        call.summary = "half-written state that must be rolled back"
        raise RuntimeError("database went away")

    monkeypatch.setattr(ConversationEngine, "handle_input", crash)
    root = twiml(turn(client, settings, call_id, "tell me more"))

    assert root.find("Gather") is None and root.find("Hangup") is not None
    spoken = says(root)
    assert spoken[0].startswith("I'm sorry, we are facing a technical issue")
    assert spoken[-1] == kb.nfo.disclaimer("en-IN")  # the scheme was discussed: close with the disclaimer

    call = load_call(call_id)
    assert call.summary is None and call.engine_state["ended"] is True and call.pending_action == "hangup"
    assert "database went away" in call.error
    (event,) = audits("webhook_error")
    assert event.call_id == call_id and event.detail["stage"] == "turn" and "RuntimeError" in event.detail["error"]

    # A retried webhook does not restart the conversation.
    monkeypatch.undo()
    root = twiml(turn(client, settings, call_id, "hello?"))
    assert root.find("Hangup") is not None and says(root) == []


def test_crash_during_opt_out_still_honours_it(client, settings, new_call, monkeypatch):
    call_id = new_call()
    twiml(answer(client, settings, call_id))

    def crash(self, call, speech_text, *, confidence=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(ConversationEngine, "handle_input", crash)
    root = twiml(turn(client, settings, call_id, "please don't call me again"))

    assert says(root) == [script("opt_out_confirm", "en-IN")] and root.find("Hangup") is not None
    call = load_call(call_id)
    assert call.outcome == CallOutcome.OPTED_OUT
    assert call.distributor.do_not_call and call.distributor.status == EmpanelmentStatus.DO_NOT_CALL
    with db.new_session() as s:
        assert compliance.is_dnc(s, "+919812345678")


def test_answer_crash_is_caught(client, settings, new_call, monkeypatch):
    call_id = new_call()

    def crash(self, call, *, answered_by=None):
        raise ValueError("bad template")

    monkeypatch.setattr(ConversationEngine, "start", crash)
    root = twiml(answer(client, settings, call_id))
    assert root.find("Hangup") is not None and len(says(root)) == 1  # nothing discussed: no disclaimer
    assert audits("webhook_error")[0].detail["stage"] == "answer"


def test_status_failure_returns_500_and_is_audited(client, settings, new_call, monkeypatch):
    call_id = new_call()

    def broken(*args, **kwargs):
        raise RuntimeError("lifecycle bug")

    monkeypatch.setattr(lifecycle, "apply_status_update", broken)
    params = {"CallSid": "CA0001", "CallStatus": "completed"}
    assert post(client, settings, f"/telephony/twilio/status/{call_id}", params).status_code == 500
    (event,) = audits("webhook_error")
    assert event.call_id == call_id and event.detail["stage"] == "status"


def test_scripted_llm_reply_is_rendered(make_client, settings, new_call):
    client = make_client(llm=ScriptedLLM([text_reply("Thank you. May I tell you about our NFO?")]))
    call_id = new_call()
    twiml(answer(client, settings, call_id))
    root = twiml(turn(client, settings, call_id, "yes"))
    assert says(root.find("Gather")) == ["Thank you. May I tell you about our NFO?"]

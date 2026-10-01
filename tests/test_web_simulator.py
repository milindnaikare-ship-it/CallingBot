"""Tests for callingbot.web.simulator_routes: the browser call simulator and its JSON API."""

from __future__ import annotations

import httpx
import pytest
from conftest import IN_WINDOW_UTC
from fastapi.testclient import TestClient
from sqlalchemy import select

from callingbot import db
from callingbot.agent.demo_llm import DemoLLM
from callingbot.agent.engine import script
from callingbot.agent.llm import ScriptedLLM, tool_reply
from callingbot.cli import DEMO_ARN, DEMO_PHONE
from callingbot.models import (
    AuditEvent,
    Call,
    CallOutcome,
    CallStatus,
    Distributor,
    EmpanelmentStatus,
    MessageChannel,
    MessageStatus,
    OutboundMessage,
)
from callingbot.telephony import TwilioProvider
from callingbot.web.app import create_app

AUTH = ("admin", "test-password")


@pytest.fixture
def make_client(settings, kb):
    def _make(*, llm=None, provider=None) -> TestClient:
        app = create_app(settings, llm=llm or DemoLLM(kb), provider=provider)
        app.state.clock = lambda: IN_WINDOW_UTC
        c = TestClient(app)
        c.auth = AUTH
        return c

    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def add_distributor(**kw) -> int:
    data = {
        "arn": "ARN-620001",
        "name": "Neha Joshi",
        "phone": "+919866667777",
        "city": "Nashik",
        "status": EmpanelmentStatus.NEW,
    }
    data.update(kw)
    with db.new_session() as s:
        d = Distributor(**data)
        s.add(d)
        s.commit()
        return d.id


def start(client, **body):
    response = client.post("/api/simulator/calls", json={"distributor_id": None, "language": None, **body})
    assert response.status_code == 200, response.text
    return response.json()


def say(client, call_id, text):
    response = client.post(f"/api/simulator/calls/{call_id}/input", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def test_page_renders_with_distributors_and_languages(client):
    add_distributor(name="Listed Person")
    add_distributor(
        arn="ARN-620002",
        name="Opted Out Person",
        phone="+919866667778",
        do_not_call=True,
        status=EmpanelmentStatus.DO_NOT_CALL,
    )
    response = client.get("/simulator")
    assert response.status_code == 200
    page = response.text
    assert "Demo distributor" in page and "Listed Person" in page and "Opted Out Person" not in page
    assert 'value="en-IN"' in page and 'value="hi-IN"' in page
    assert '<script src="/static/simulator.js" defer></script>' in page
    assert "Messaging providers are live" not in page  # outbox only in tests


def test_full_demo_conversation_sends_the_link(client, kb):
    started = start(client)
    call_id = started["call_id"]
    assert started["action"] == "gather" and "virtual assistant" in started["say"][0]

    pitch = say(client, call_id, "yes speaking")
    assert kb.nfo.scheme_name in pitch["say"][0] and pitch["ended"] is False and pitch["outcome"] is None
    assert say(client, call_id, "no I'm not empanelled")["ended"] is False
    final = say(client, call_id, "yes send it on whatsapp")

    assert final["ended"] is True and final["action"] == "hangup"
    assert final["outcome"] == "link_sent" and final["status"] == "completed"
    assert kb.nfo.disclaimer("en-IN") in " ".join(final["say"])
    (message,) = final["messages"]
    assert message["channel"] == "whatsapp" and message["status"] == "queued"
    assert message["destination"] == "+91******0000" and "/r/" in message["link"]

    with db.new_session() as s:
        call = s.get(Call, call_id)
        assert call.provider == "simulator" and call.status == CallStatus.COMPLETED
        assert call.outcome == CallOutcome.LINK_SENT and call.engine_state["finalized"] is True
        demo = call.distributor
        assert (demo.arn, demo.phone, demo.city) == (DEMO_ARN, DEMO_PHONE, "Mumbai")
        assert demo.status == EmpanelmentStatus.LINK_SENT
        outbox = s.scalars(select(OutboundMessage)).one()
        assert outbox.call_id == call_id and outbox.channel == MessageChannel.WHATSAPP
        assert outbox.status == MessageStatus.QUEUED
        assert s.scalars(select(AuditEvent.kind).where(AuditEvent.kind == "call_finalized")).all()

    # The demo distributor is reused, not duplicated.
    start(client)
    with db.new_session() as s:
        assert len(s.scalars(select(Distributor).where(Distributor.arn == DEMO_ARN)).all()) == 1


def test_silence_reprompts_then_hangup_endpoint_finalises(client):
    call_id = start(client)["call_id"]
    silent = say(client, call_id, None)
    assert silent["say"] == [script("reprompt", "en-IN")] and silent["ended"] is False
    assert say(client, call_id, "   ")["ended"] is True  # second silence: polite goodbye

    call_id = start(client)["call_id"]
    response = client.post(f"/api/simulator/calls/{call_id}/hangup", json={})
    assert response.status_code == 200
    assert response.json() == {"status": "completed", "outcome": "no_outcome", "messages": []}
    # Anything after the hang-up gets an empty hang-up; repeating the hang-up is harmless.
    after = say(client, call_id, "hello?")
    assert after["ended"] is True and after["say"] == []
    assert client.post(f"/api/simulator/calls/{call_id}/hangup", json={}).json()["status"] == "completed"


def test_real_distributor_in_another_language_keeps_their_preference(client):
    d = add_distributor(preferred_language="en-IN")
    started = start(client, distributor_id=d, language="hi-IN")
    assert started["language"] == "hi-IN" and "नमस्ते" in started["say"][0]
    with db.new_session() as s:
        call = s.get(Call, started["call_id"])
        assert call.language == "hi-IN" and call.distributor_id == d
        assert s.get(Distributor, d).preferred_language == "en-IN"


def test_bot_ending_the_call_finalises_it(make_client):
    llm = ScriptedLLM([tool_reply(("end_call", {"reason": "done"}), text="Thank you, goodbye.")])
    client = make_client(llm=llm)
    call_id = start(client)["call_id"]
    final = say(client, call_id, "that's all")
    assert final["ended"] is True and final["status"] == "completed"
    with db.new_session() as s:
        assert s.get(Call, call_id).engine_state["finalized"] is True


def test_simulator_calls_ignore_the_configured_provider(make_client, settings):
    client = make_client(
        provider=TwilioProvider(
            settings,
            http_client=httpx.Client(
                transport=httpx.MockTransport(lambda request: pytest.fail("no network"))
            ),
        )
    )
    call_id = start(client)["call_id"]
    with db.new_session() as s:
        assert s.get(Call, call_id).provider == "simulator"


def test_validation_errors(client):
    opted_out = add_distributor(do_not_call=True, status=EmpanelmentStatus.DO_NOT_CALL)
    post = client.post
    assert post("/api/simulator/calls", json={"distributor_id": 999}).status_code == 404
    assert post("/api/simulator/calls", json={"distributor_id": opted_out}).status_code == 409
    assert post("/api/simulator/calls", json={"language": "fr-FR"}).status_code == 422
    assert post("/api/simulator/calls", json={"distributor_id": "abc"}).status_code == 422
    assert post("/api/simulator/calls", json={"unexpected": 1}).status_code == 422
    bad_json = post("/api/simulator/calls", content=b"{nope", headers={"Content-Type": "application/json"})
    assert bad_json.status_code == 422
    assert post("/api/simulator/calls/999/input", json={"text": "hi"}).status_code == 404
    call_id = start(client)["call_id"]
    assert post(f"/api/simulator/calls/{call_id}/input", json={"text": "x" * 2001}).status_code == 422


def test_cannot_drive_a_real_phone_call(client):
    d = add_distributor()
    with db.new_session() as s:
        call = Call(distributor_id=d, provider="twilio", status=CallStatus.IN_PROGRESS)
        s.add(call)
        s.commit()
        call_id = call.id
    assert (
        client.post(f"/api/simulator/calls/{call_id}/input", json={"text": "stop calling"}).status_code == 404
    )
    assert client.post(f"/api/simulator/calls/{call_id}/hangup", json={}).status_code == 404
    with db.new_session() as s:
        assert s.get(Call, call_id).status == CallStatus.IN_PROGRESS


def test_requires_auth_and_same_origin(client, make_client):
    anon = TestClient(client.app)
    assert anon.post("/api/simulator/calls", json={}).status_code == 401
    assert anon.get("/simulator").status_code == 401
    forged = client.post("/api/simulator/calls", json={}, headers={"Origin": "https://evil.example"})
    assert forged.status_code == 403
    # Neither Origin nor a JSON content type: could be a cross-site form, so rejected.
    assert client.post("/api/simulator/calls/1/hangup").status_code == 403
    # Browser fetch from our own page (Origin present) and scripts (JSON, no Origin) are accepted.
    assert (
        client.post("/api/simulator/calls", json={}, headers={"Origin": "http://testserver"}).status_code
        == 200
    )
    with db.new_session() as s:
        assert len(s.scalars(select(Call)).all()) == 1

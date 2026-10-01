"""End-to-end tests: the modules wired together the way production runs them.

Nothing here talks to the network. Twilio is a :class:`TwilioProvider` whose REST calls go to an
``httpx.MockTransport``; its webhooks are posted to the real FastAPI app, signed with an independent
implementation of Twilio's ``X-Twilio-Signature`` algorithm against the exact URLs the provider was
given (``Url``, ``StatusCallback``) or that the bot's TwiML points at (``<Gather action>``). If any
of those URLs drifted from the URL the app verifies against, every webhook below would get a 403.

* Twilio: dialer -> answer -> three turns (incl. a WhatsApp link) -> status -> tracked link click,
  plus the Claude conversation invariants (append-only history, tool_use/tool_result pairing,
  stable system prompt and tools, no phone numbers or emails sent to the model).
* Browser simulator API with the offline demo bot, built purely from settings (LLM_PROVIDER=fake).
* The command line, as in the README quickstart, against the AMC's shipped ``config/``.
* Opt-out through Twilio webhooks, and the dialer never calling that number again.
"""

from __future__ import annotations

import base64
import builtins
import hashlib
import hmac
import itertools
import json
import shutil
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import IN_WINDOW_UTC, ROOT
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from callingbot import cli, compliance, db
from callingbot.agent.demo_llm import DemoLLM
from callingbot.agent.llm import ScriptedLLM, text_reply, tool_reply
from callingbot.models import (
    AuditEvent,
    Call,
    CallOutcome,
    CallStatus,
    Campaign,
    CampaignContact,
    CampaignStatus,
    ContactState,
    Distributor,
    DNCEntry,
    EmpanelmentStatus,
    InterestLevel,
    LinkClick,
    MessageChannel,
    MessageStatus,
    OutboundMessage,
    Turn,
    TurnRole,
)
from callingbot.services import dialer, reporting
from callingbot.settings import get_settings
from callingbot.telephony import TwilioProvider
from callingbot.web.app import create_app

ADMIN = ("admin", "test-password")  # conftest settings
PHONE = "+919812345678"
EMAIL = "ravi.kumar@example.com"


# ---------------------------------------------------------------------------------------------
# A fake Twilio: REST API behind httpx.MockTransport, webhooks posted to the app
# ---------------------------------------------------------------------------------------------


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's algorithm, written independently of the code under test."""
    payload = url + "".join(name + params[name] for name in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


class FakeTwilio:
    """Records Calls.json requests (answering with sids CA123, CA124, ...) and plays Twilio's side."""

    ACCOUNT = {"AccountSid": "AC00000000000000000000000000000000", "ApiVersion": "2010-04-01"}

    def __init__(self, settings):
        self.settings = settings
        self.calls: list[dict[str, list[str]]] = []  # form of every Calls.json request
        self._sids = (f"CA{n}" for n in itertools.count(123))
        transport = httpx.MockTransport(self._handle)
        self.provider = TwilioProvider(settings, http_client=httpx.Client(transport=transport))
        self.client: TestClient | None = None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = f"/2010-04-01/Accounts/{self.settings.twilio_account_sid}/Calls.json"
        if request.method != "POST" or request.url.path != path:
            raise AssertionError(f"unexpected Twilio request {request.method} {request.url}")
        assert request.headers["Authorization"].startswith("Basic ")
        self.calls.append(parse_qs(request.content.decode(), keep_blank_values=True))
        return httpx.Response(201, json={"sid": next(self._sids), "status": "queued"})

    def webhook(self, url: str, params: dict[str, str]):
        """POST a signed webhook to ``url`` - a public URL we were given - as Twilio would."""
        assert self.client is not None
        full = {**self.ACCOUNT, **params}
        signature = twilio_signature(self.settings.twilio_auth_token, url, full)
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == self.settings.base_url  # never an internal URL
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        return self.client.post(path, data=full, headers={"X-Twilio-Signature": signature})


def twiml(response) -> ET.Element:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/xml")
    return ET.fromstring(response.content)


def says(root: ET.Element) -> list[str]:
    return [s.text or "" for s in root.iter("Say")]


def gather_action(root: ET.Element) -> str:
    gather = root.find("Gather")
    assert gather is not None, ET.tostring(root, encoding="unicode")
    assert root.find("Hangup") is None
    return gather.attrib["action"]


@pytest.fixture
def twilio_settings(settings):
    return settings.model_copy(update={"telephony_provider": "twilio"})


@pytest.fixture
def twilio_world(twilio_settings):
    """Factory: a running app wired to a FakeTwilio and the given LLM; one distributor in an ACTIVE
    campaign. Returns (fake twilio, app, ids)."""

    def _make(llm):
        fake = FakeTwilio(twilio_settings)
        app = create_app(twilio_settings, llm=llm, provider=fake.provider)
        app.state.clock = lambda: IN_WINDOW_UTC  # Tuesday 11:00 IST, inside the calling window
        fake.client = TestClient(app)
        with db.new_session() as s:
            d = Distributor(
                arn="ARN-555001",
                name="Ravi Kumar",
                firm_name="Kumar Investments",
                phone=PHONE,
                email=EMAIL,
                city="Pune",
                status=EmpanelmentStatus.NEW,
            )
            campaign = Campaign(name="NFO Launch", status=CampaignStatus.ACTIVE)
            s.add_all([d, campaign])
            s.flush()
            assert dialer.add_distributors_to_campaign(s, campaign) == 1
            s.commit()
            ids = {"distributor": d.id, "campaign": campaign.id}
        return fake, app, ids

    return _make


def dial(app, fake: FakeTwilio, campaign_id: int) -> dialer.DialReport:
    with db.new_session() as s:
        campaign = s.get(Campaign, campaign_id)
        return dialer.dial_due_contacts(
            s,
            campaign=campaign,
            provider=fake.provider,
            kb=app.state.kb,
            settings=app.state.settings,
            now_utc=IN_WINDOW_UTC,
        )


def only(values):
    (value,) = values
    return value


# ---------------------------------------------------------------------------------------------
# a. Twilio, end to end
# ---------------------------------------------------------------------------------------------

PITCH = (
    "Thank you, Ravi ji. I'm calling about our upcoming NFO, the Sample Flexi Cap Fund. "
    "Are you already empanelled with us?"
)
SENT = "Done, I've sent the empanelment link to you on WhatsApp. Is there anything else I can help with?"
GOODBYE = "Thank you, have a great day!"


def test_twilio_call_end_to_end(twilio_world, kb):
    record = {
        "outcome": "link_sent",
        "interest_level": "warm",
        "summary_for_rm": "Not yet empanelled. Asked for the empanelment link on WhatsApp; it was sent.",
        "objections": [],
    }
    llm = ScriptedLLM(
        [
            text_reply(PITCH),
            tool_reply(("send_empanelment_link", {"channel": "whatsapp", "email": None})),
            text_reply(SENT),
            tool_reply(("record_outcome", record), ("end_call", {"reason": "done"}), text=GOODBYE),
        ]
    )
    fake, app, ids = twilio_world(llm)

    # --- the dialer places the call through the Twilio REST API
    report = dial(app, fake, ids["campaign"])
    assert (report.placed, report.skipped, report.failed) == (1, 0, 0)
    form = only(fake.calls)
    with db.new_session() as s:
        call = s.scalars(select(Call)).one()
        call_id = call.id
        assert (call.provider, call.provider_call_id, call.status) == ("twilio", "CA123", CallStatus.QUEUED)
        contact = s.scalars(select(CampaignContact)).one()
        assert (contact.state, contact.attempts) == (ContactState.IN_PROGRESS, 1)
    base = app.state.settings.base_url
    assert form["To"] == [PHONE] and form["From"] == [app.state.settings.twilio_from_number]
    answer_url, status_url = form["Url"][0], form["StatusCallback"][0]
    assert answer_url == f"{base}/telephony/twilio/answer/{call_id}"
    assert status_url == f"{base}/telephony/twilio/status/{call_id}"
    assert form["StatusCallbackEvent"] == ["initiated", "ringing", "answered", "completed"]

    # The signature is checked against the public URL, not the one the request reached internally.
    internal = fake.client.post(
        f"/telephony/twilio/answer/{call_id}",
        data={"CallSid": "CA123"},
        headers={
            "X-Twilio-Signature": twilio_signature(
                app.state.settings.twilio_auth_token,
                f"http://testserver/telephony/twilio/answer/{call_id}",
                {"CallSid": "CA123"},
            )
        },
    )
    assert internal.status_code == 403

    assert fake.webhook(status_url, {"CallSid": "CA123", "CallStatus": "ringing"}).status_code == 204

    # --- answered: the pre-approved greeting, no LLM involved
    root = twiml(fake.webhook(answer_url, {"CallSid": "CA123", "CallStatus": "in-progress"}))
    greeting = only(says(root))
    assert "virtual assistant" in greeting and kb.amc.name in greeting and "Ravi Kumar" in greeting
    turn_url = gather_action(root)
    assert turn_url == f"{base}/telephony/twilio/turn/{call_id}"
    assert root.find("Redirect").text == turn_url
    assert llm.requests == []
    assert fake.webhook(status_url, {"CallSid": "CA123", "CallStatus": "in-progress"}).status_code == 204

    def turn(url: str, speech: str) -> ET.Element:
        params = {
            "CallSid": "CA123",
            "CallStatus": "in-progress",
            "SpeechResult": speech,
            "Confidence": "0.93",
        }
        return twiml(fake.webhook(url, params))

    # --- turn 1: scripted pitch
    root = turn(turn_url, "Yes, speaking")
    assert says(root) == [PITCH]
    turn_url = gather_action(root)

    # --- turn 2: the model sends the link on WhatsApp, then confirms
    root = turn(turn_url, "Please send the empanelment link on WhatsApp")
    assert says(root) == [SENT]
    turn_url = gather_action(root)

    # --- turn 3: outcome + goodbye; the engine adds the mandatory SEBI disclaimer and hangs up
    root = turn(turn_url, "No, that's all, thanks")
    assert root.find("Gather") is None and root.find("Hangup") is not None
    assert says(root) == [GOODBYE, kb.nfo.mandatory_disclaimer]

    # --- Twilio reports the end of the call
    status = {"CallSid": "CA123", "CallStatus": "completed", "CallDuration": "95"}
    assert fake.webhook(status_url, status).status_code == 204
    assert fake.webhook(status_url, status).status_code == 204  # retried callback: harmless

    with db.new_session() as s:
        call = s.get(Call, call_id)
        assert call.status == CallStatus.COMPLETED and call.duration_seconds == 95
        assert call.engine_state["finalized"] is True and call.engine_state["disclaimer_spoken"] is True
        assert call.outcome == CallOutcome.LINK_SENT and call.interest_level == InterestLevel.WARM
        assert "WhatsApp" in call.summary
        assert call.turn_count == 3 and call.answered_at == IN_WINDOW_UTC and call.ended_at == IN_WINDOW_UTC
        contact = s.scalars(select(CampaignContact)).one()
        assert (contact.state, contact.final_outcome) == (ContactState.DONE, CallOutcome.LINK_SENT)
        distributor = s.get(Distributor, ids["distributor"])
        assert distributor.status == EmpanelmentStatus.LINK_SENT and not distributor.do_not_call

        message = s.scalars(select(OutboundMessage)).one()
        assert message.channel == MessageChannel.WHATSAPP and message.status == MessageStatus.QUEUED
        assert (message.destination, message.call_id, message.distributor_id) == (
            PHONE,
            call_id,
            distributor.id,
        )
        assert message.link.startswith(f"{base}/r/") and message.link in message.body

        transcript = [(t.role, t.text) for t in s.scalars(select(Turn).where(Turn.call_id == call_id))]
        assert transcript == [
            (TurnRole.BOT, greeting),
            (TurnRole.DISTRIBUTOR, "Yes, speaking"),
            (TurnRole.BOT, PITCH),
            (TurnRole.DISTRIBUTOR, "Please send the empanelment link on WhatsApp"),
            (TurnRole.BOT, SENT),
            (TurnRole.DISTRIBUTOR, "No, that's all, thanks"),
            (TurnRole.BOT, GOODBYE),
            (TurnRole.BOT, kb.nfo.mandatory_disclaimer),
        ]
        kinds = [e.kind for e in s.scalars(select(AuditEvent).order_by(AuditEvent.id))]
        assert kinds == ["disclosure_played", "link_sent", "call_finalized"]
        stored_history = list(call.llm_messages)
        link = message.link

    # --- the distributor opens the tracked link
    response = fake.client.get(urlsplit(link).path, follow_redirects=False, headers={"User-Agent": "Mobile"})
    assert response.status_code == 302
    target = response.headers["location"]
    assert target == f"https://partners.sample-mf.example/empanel?arn=ARN-555001&ref=call{call_id}"
    with db.new_session() as s:
        click = s.scalars(select(LinkClick)).one()
        assert (click.distributor_id, click.call_id) == (ids["distributor"], call_id)
        stats = reporting.campaign_stats(s, ids["campaign"])
    assert stats["calls_total"] == 1 and stats["connected"] == 1
    assert stats["links_sent"] == 1 and stats["link_clicks"] == 1
    assert stats["calls_by_outcome"]["link_sent"] == 1 and stats["by_status"]["link_sent"] == 1
    assert stats["contacts_by_state"]["done"] == 1

    # The admin transcript page shows the call.
    page = fake.client.get(f"/calls/{call_id}", auth=ADMIN)
    assert page.status_code == 200 and "Please send the empanelment link on WhatsApp" in page.text

    # --- what Claude saw
    assert_conversation_invariants(llm.requests, stored_history, greeting)
    assert len(llm.requests) == 4
    seen_by_model = json.dumps(llm.requests, ensure_ascii=False)
    assert PHONE not in seen_by_model and PHONE[3:] not in seen_by_model and EMAIL not in seen_by_model


def assert_conversation_invariants(requests: list[dict], stored: list[dict], greeting: str) -> None:
    """The engine's Messages API contract (docs/ARCHITECTURE.md section 5)."""
    assert requests, "the LLM was never called"
    # Byte-identical system prompt and tools on every request (prompt caching).
    assert all(r["system"] == requests[0]["system"] for r in requests)
    assert all(r["tools"] == requests[0]["tools"] for r in requests)

    # Append-only: each request strictly extends the previous one, and the stored history extends
    # the last request (it also holds the final response and its tool results).
    histories = [r["messages"] for r in requests] + [stored]
    for before, after in itertools.pairwise(histories):
        assert len(after) > len(before)
        assert after[: len(before)] == before

    # Message 0 is the call context, message 1 the greeting exactly as spoken.
    assert stored[0]["role"] == "user" and "[Call context" in stored[0]["content"]
    assert stored[1] == {"role": "assistant", "content": [{"type": "text", "text": greeting}]}
    # Every request ends with a user turn, and turns alternate.
    assert all(r["messages"][-1]["role"] == "user" for r in requests)
    roles = [m["role"] for m in stored]
    assert all(a != b for a, b in itertools.pairwise(roles)), roles

    # Every tool_use is answered, in order, by a tool_result in the very next (user) message, and no
    # tool_result appears anywhere else.
    answered: set[str] = set()
    for i, message in enumerate(stored):
        blocks = message["content"] if isinstance(message["content"], list) else []
        uses = [b["id"] for b in blocks if b.get("type") == "tool_use"]
        results = [b["tool_use_id"] for b in blocks if b.get("type") == "tool_result"]
        if uses:
            assert message["role"] == "assistant"
            assert i + 1 < len(stored), f"tool_use {uses} has no tool_result after it"
            following = stored[i + 1]
            assert following["role"] == "user"
            assert [b["tool_use_id"] for b in following["content"] if b.get("type") == "tool_result"] == uses
        if results:
            previous = stored[i - 1]["content"]
            assert results == [b["id"] for b in previous if b.get("type") == "tool_use"]
            answered.update(results)
    all_uses = {
        b["id"]
        for m in stored
        if isinstance(m["content"], list)
        for b in m["content"]
        if b.get("type") == "tool_use"
    }
    assert answered == all_uses


# ---------------------------------------------------------------------------------------------
# b. Browser simulator API with the offline demo bot (LLM_PROVIDER=fake)
# ---------------------------------------------------------------------------------------------


def test_simulator_api_happy_path_with_demo_llm(settings, kb):
    assert settings.llm_provider == "fake" and settings.telephony_provider == "simulator"
    app = create_app(settings)  # LLM, messenger and provider all built from settings
    assert isinstance(app.state.llm, DemoLLM) and app.state.provider.name == "simulator"
    app.state.clock = lambda: IN_WINDOW_UTC
    client = TestClient(app)
    with db.new_session() as s:
        d = Distributor(arn="ARN-555002", name="Meena Shah", phone="+919812300002", city="Surat")
        s.add(d)
        s.commit()
        distributor_id = d.id

    assert client.get("/simulator", auth=ADMIN).status_code == 200
    assert client.post("/api/simulator/calls", json={"distributor_id": distributor_id}).status_code == 401

    started = client.post("/api/simulator/calls", json={"distributor_id": distributor_id}, auth=ADMIN)
    assert started.status_code == 200, started.text
    body = started.json()
    call_id = body["call_id"]
    assert (
        body["action"] == "gather"
        and "virtual assistant" in body["say"][0]
        and "Meena Shah" in body["say"][0]
    )

    def say(text):
        r = client.post(f"/api/simulator/calls/{call_id}/input", json={"text": text}, auth=ADMIN)
        assert r.status_code == 200, r.text
        return r.json()

    pitch = say("Yes, speaking")
    assert pitch["action"] == "gather" and not pitch["ended"]
    assert kb.nfo.scheme_name in " ".join(pitch["say"]) and "Are you already empanelled" in " ".join(
        pitch["say"]
    )
    offer = say("No, not yet")
    assert offer["action"] == "gather" and "empanelment link" in " ".join(offer["say"])
    done = say("Yes please, send it on WhatsApp")
    assert done["action"] == "hangup" and done["ended"] is True
    assert done["outcome"] == "link_sent" and done["status"] == "completed"
    assert kb.nfo.mandatory_disclaimer in " ".join(done["say"])
    (message,) = done["messages"]
    assert message["channel"] == "whatsapp" and message["status"] == "queued"
    assert (
        message["link"].startswith(f"{settings.base_url}/r/")
        and "+919812300002" not in message["destination"]
    )

    # The call is over: further input gets a silent hang-up and changes nothing.
    after = say("Hello?")
    assert after["action"] == "hangup" and after["say"] == []

    with db.new_session() as s:
        call = s.get(Call, call_id)
        assert call.provider == "simulator" and call.engine_state["finalized"] is True
        assert call.turn_count == 3 and call.outcome == CallOutcome.LINK_SENT
        assert s.get(Distributor, distributor_id).status == EmpanelmentStatus.LINK_SENT
        kinds = [e.kind for e in s.scalars(select(AuditEvent).order_by(AuditEvent.id))]
        assert kinds == ["disclosure_played", "link_sent", "call_finalized"]
    page = client.get(f"/calls/{call_id}", auth=ADMIN)
    assert page.status_code == 200 and "Yes please, send it on WhatsApp" in page.text


# ---------------------------------------------------------------------------------------------
# c. The command line, as in the README quickstart, with the AMC's shipped config/
# ---------------------------------------------------------------------------------------------

_ENV_TO_CLEAR = (
    "ANTHROPIC_API_KEY",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "SMS_PROVIDER",
    "WHATSAPP_PROVIDER",
    "EMAIL_PROVIDER",
    "ADMIN_PASSWORD",
    "SECRET_KEY",
    "APP_ENV",
)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # relative paths as in the README; no stray .env
    for var in _ENV_TO_CLEAR:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'callingbot.db'}")
    monkeypatch.setenv("CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("TELEPHONY_PROVIDER", "simulator")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setattr(cli, "utcnow", lambda: IN_WINDOW_UTC)
    (tmp_path / "data").mkdir()
    shutil.copy(ROOT / "data" / "sample_distributors.csv", tmp_path / "data")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    db.get_engine().dispose()
    db.configure_engine("sqlite://")


def test_cli_quickstart_end_to_end(cli_env, capsys, monkeypatch):
    def run(*argv: str) -> str:
        code = cli.main(list(argv))
        out, err = capsys.readouterr()
        assert code == 0, f"callingbot {' '.join(argv)} -> {code}\n{out}\n{err}"
        return out

    assert "Database ready" in run("init-db")
    assert (cli_env / "callingbot.db").exists()
    out = run("check-config")
    assert "Carnelian" in out and "Traceback" not in out

    out = run("import-distributors", "data/sample_distributors.csv", "--campaign", "NFO Launch")
    assert "created" in out and "Created campaign 'NFO Launch' (draft)." in out
    assert "error(s)" in out  # the sample deliberately contains bad rows
    with db.new_session() as s:
        imported = s.scalar(select(func.count(Distributor.id)))
        contacts = s.scalar(select(func.count(CampaignContact.id)))
    assert imported >= 5 and 0 < contacts <= imported

    out = run("campaign", "start", "NFO Launch")
    assert f"is active ({contacts} pending contact(s))" in out
    out = run("run-dialer", "--campaign", "NFO Launch", "--once")
    assert "round 1: placed 3" in out  # max_concurrent_calls in config/campaign.yaml

    out = run("stats", "--campaign", "NFO Launch")
    assert f"Distributors: {contacts}" in out and "Calls: 3" in out and "Funnel:" in out

    lines = iter(["Yes speaking", "Please send the empanelment link on WhatsApp"])
    prompts: list[str] = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(lines, "/quit")

    monkeypatch.setattr(builtins, "input", fake_input)
    out = run("simulate")
    assert prompts == ["YOU: ", "YOU: "]  # the bot hung up after the link, no third prompt
    assert "a virtual assistant calling from Carnelian Mutual Fund" in out
    assert "(The bot hung up.)" in out and "Outcome: link_sent" in out
    # PUBLIC_BASE_URL is the quickstart default here, so links point at the local app.
    assert "whatsapp to +91******0000 [queued] link: http://localhost:8000/r/" in out

    out = run("stats")
    assert "Links sent: 1" in out and "Calls: 4" in out
    out = run("export-leads", "leads.csv")
    assert "Exported 1 lead(s)" in out
    assert "Demo Distributor" in (cli_env / "leads.csv").read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------------------------------------
# d. Opt-out through the Twilio webhooks
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("model_calls_opt_out", [True, False], ids=["model-opt-out", "engine-safety-net"])
def test_opt_out_end_to_end(twilio_world, kb, model_calls_opt_out):
    if model_calls_opt_out:
        reply = tool_reply(
            ("opt_out", {"reason": "Asked not to be called again"}),
            text="I'm sorry for the disturbance. We won't call you again.",
        )
        spoken = "I'm sorry for the disturbance. We won't call you again."
    else:
        # The model misses the request; the engine's deterministic detector must still honour it.
        reply = text_reply("I understand. Before you go, may I share one more detail about the NFO?")
        spoken = "Understood, we won't call you again. Have a good day."
    llm = ScriptedLLM([text_reply(PITCH), reply])
    fake, app, ids = twilio_world(llm)

    assert dial(app, fake, ids["campaign"]).placed == 1
    form = only(fake.calls)
    answer_url, status_url = form["Url"][0], form["StatusCallback"][0]
    turn_url = gather_action(
        twiml(fake.webhook(answer_url, {"CallSid": "CA123", "CallStatus": "in-progress"}))
    )
    turn_url = gather_action(
        twiml(fake.webhook(turn_url, {"CallSid": "CA123", "SpeechResult": "Yes, speaking"}))
    )

    root = twiml(fake.webhook(turn_url, {"CallSid": "CA123", "SpeechResult": "Please don't call me again"}))
    assert root.find("Gather") is None and root.find("Hangup") is not None
    # No pitch-closing disclaimer for someone who opted out; just the confirmation.
    assert says(root) == [spoken]
    assert (
        fake.webhook(
            status_url, {"CallSid": "CA123", "CallStatus": "completed", "CallDuration": "31"}
        ).status_code
        == 204
    )

    with db.new_session() as s:
        call = s.scalars(select(Call)).one()
        assert call.outcome == CallOutcome.OPTED_OUT and call.status == CallStatus.COMPLETED
        assert call.engine_state["finalized"] is True
        entry = s.scalars(select(DNCEntry)).one()
        assert entry.phone == PHONE and entry.source == "call_opt_out"
        assert compliance.is_dnc(s, PHONE) and compliance.is_dnc(s, "98123 45678")
        distributor = s.get(Distributor, ids["distributor"])
        assert distributor.status == EmpanelmentStatus.DO_NOT_CALL and distributor.do_not_call
        contact = s.scalars(select(CampaignContact)).one()
        assert (contact.state, contact.final_outcome) == (ContactState.DONE, CallOutcome.OPTED_OUT)
        kinds = {e.kind for e in s.scalars(select(AuditEvent))}
        assert {"opt_out", "dnc_added", "call_finalized"} <= kinds
        assert not s.scalars(select(OutboundMessage)).all()

    # The next dialling round of the same campaign does not call them...
    report = dial(app, fake, ids["campaign"])
    assert (report.placed, report.failed) == (0, 0)
    assert len(fake.calls) == 1

    # ...nor can a new campaign add them, and a contact that was queued elsewhere before the opt-out
    # is skipped at dial time.
    with db.new_session() as s:
        second = Campaign(name="NFO Follow-up", status=CampaignStatus.ACTIVE)
        s.add(second)
        s.flush()
        assert dialer.add_distributors_to_campaign(s, second) == 0
        s.add(
            CampaignContact(
                campaign_id=second.id, distributor_id=ids["distributor"], state=ContactState.PENDING
            )
        )
        s.commit()
        second_id = second.id
    report = dial(app, fake, second_id)
    assert (report.placed, report.skipped) == (0, 1)
    assert len(fake.calls) == 1
    with db.new_session() as s:
        skipped = s.scalars(select(AuditEvent).where(AuditEvent.kind == "dial_skipped")).one()
        assert skipped.detail["reason"] == "dnc"
        assert s.scalar(select(func.count(Call.id))) == 1

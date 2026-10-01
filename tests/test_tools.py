"""Tests for callingbot.agent.tools: strict schemas, validation and every tool handler."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from conftest import IN_WINDOW_UTC
from sqlalchemy import select

from callingbot import links
from callingbot.agent import tools as tools_module
from callingbot.agent.tools import (
    TOOL_DEFINITIONS,
    ToolContext,
    ToolOutcome,
    build_tool_definitions,
    execute_tool,
    mask_email,
    normalize_arn_input,
    normalize_email,
)
from callingbot.compliance import is_dnc
from callingbot.messaging import MessageSender, Messenger, OutgoingMessage, SendResult, build_messenger
from callingbot.models import (
    AuditEvent,
    Call,
    Callback,
    CallOutcome,
    CallStatus,
    DNCEntry,
    EmpanelmentStatus,
    FollowUpKind,
    InterestLevel,
    MessageChannel,
    MessageStatus,
    OutboundMessage,
)

EXPECTED_TOOLS = {
    "verify_arn",
    "update_distributor_details",
    "send_empanelment_link",
    "schedule_callback",
    "set_language",
    "transfer_to_human",
    "log_request",
    "opt_out",
    "record_outcome",
    "end_call",
}
FORBIDDEN_KEYWORDS = {
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "uniqueItems",
    "default",
}


class RecordingSender(MessageSender):
    def __init__(self, channel: MessageChannel, *, ok: bool = True):
        self.channel = channel
        self.ok = ok
        self.sent: list[OutgoingMessage] = []

    def send(self, message: OutgoingMessage) -> SendResult:
        self.sent.append(message)
        if self.ok:
            return SendResult(ok=True, provider_message_id=f"pm-{len(self.sent)}")
        return SendResult(ok=False, error="provider down")


@pytest.fixture
def make_ctx(session, kb, settings, make_distributor):
    def _make(
        *, distributor=None, now_utc=IN_WINDOW_UTC, messenger=None, settings_=None, **call_kw
    ) -> ToolContext:
        d = distributor or make_distributor()
        call = Call(distributor_id=d.id, provider="simulator", status=CallStatus.IN_PROGRESS, **call_kw)
        session.add(call)
        session.flush()
        return ToolContext(
            session=session,
            call=call,
            distributor=d,
            kb=kb,
            settings=settings_ or settings,
            messenger=messenger or build_messenger(settings),
            now_utc=now_utc,
        )

    return _make


def payload(outcome: ToolOutcome) -> dict:
    return json.loads(outcome.content)


def audits(session, kind: str) -> list[AuditEvent]:
    return list(session.scalars(select(AuditEvent).where(AuditEvent.kind == kind)))


# --- definitions -------------------------------------------------------------------------------


def _check_schema(schema, path: str) -> None:
    """Recursive checker for the strict-mode subset of JSON Schema."""
    if isinstance(schema, list):
        for i, item in enumerate(schema):
            _check_schema(item, f"{path}[{i}]")
        return
    if not isinstance(schema, dict):
        return
    bad = FORBIDDEN_KEYWORDS & set(schema)
    assert not bad, f"{path}: unsupported keywords {bad}"
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, f"{path}: additionalProperties must be False"
        props = schema.get("properties")
        assert isinstance(props, dict) and props, f"{path}: properties missing"
        assert schema.get("required") == list(props), f"{path}: required must list every property"
    if schema.get("type") == "array":
        assert "items" in schema, f"{path}: arrays need items"
    if "anyOf" in schema:
        assert {"type": "null"} in schema["anyOf"] or all("type" in s for s in schema["anyOf"])
    for key, value in schema.items():
        if key in ("properties",):
            for name, sub in value.items():
                _check_schema(sub, f"{path}.{name}")
        elif isinstance(value, (dict, list)):
            _check_schema(value, f"{path}.{key}")


@pytest.mark.parametrize("variant", ["generic", "kb"])
def test_tool_definitions_are_strict_valid(kb, variant):
    definitions = TOOL_DEFINITIONS if variant == "generic" else build_tool_definitions(kb)
    assert {t["name"] for t in definitions} == EXPECTED_TOOLS
    for tool in definitions:
        assert set(tool) == {"name", "description", "strict", "input_schema"}
        assert tool["strict"] is True
        assert len(tool["description"]) > 80, tool["name"]
        assert tool["input_schema"]["type"] == "object"
        _check_schema(tool["input_schema"], tool["name"])


def test_tool_definitions_are_deterministic_and_kb_aware(kb):
    assert json.dumps(build_tool_definitions(kb)) == json.dumps(build_tool_definitions(kb))
    assert json.dumps(TOOL_DEFINITIONS) == json.dumps(build_tool_definitions())
    by_name = {t["name"]: t for t in build_tool_definitions(kb)}
    lang = by_name["set_language"]["input_schema"]["properties"]["language"]
    assert lang["enum"] == ["en-IN", "hi-IN"]
    preferred = by_name["update_distributor_details"]["input_schema"]["properties"]["preferred_language"]
    assert preferred["anyOf"][0]["enum"] == ["en-IN", "hi-IN"] and preferred["anyOf"][1] == {"type": "null"}
    outcome = by_name["record_outcome"]["input_schema"]["properties"]["outcome"]
    assert "opted_out" not in outcome["enum"] and "transferred" not in outcome["enum"]


# --- dispatch and validation -------------------------------------------------------------------


def test_unknown_tool(make_ctx):
    out = execute_tool(make_ctx(), "delete_database", {})
    assert out.is_error and "Unknown tool" in payload(out)["error"] and "verify_arn" in payload(out)["error"]


@pytest.mark.parametrize(
    "name,tool_input,fragment",
    [
        ("verify_arn", {}, "missing required field(s): arn"),
        ("verify_arn", {"arn": 12345}, "arn must be a string"),
        ("verify_arn", {"arn": "1", "extra": True}, "unexpected field(s): extra"),
        (
            "send_empanelment_link",
            {"channel": "fax", "email": None},
            "channel must be one of: sms, whatsapp, email",
        ),
        (
            "schedule_callback",
            {"when_local": "2026-10-14 11:30", "with_rm": "yes", "notes": ""},
            "true or false",
        ),
        (
            "record_outcome",
            {"outcome": "interested", "interest_level": None, "summary_for_rm": "x", "objections": "none"},
            "array",
        ),
        (
            "record_outcome",
            {"outcome": "opted_out", "interest_level": None, "summary_for_rm": "x", "objections": []},
            "outcome must be one of",
        ),
        ("verify_arn", ["ARN-1"], "input must be an object"),
    ],
)
def test_invalid_input_is_reported_not_raised(make_ctx, name, tool_input, fragment):
    out = execute_tool(make_ctx(), name, tool_input)
    assert out.is_error and not out.end_call
    assert fragment in payload(out)["error"]


def test_missing_nullable_fields_are_read_as_null(make_ctx):
    out = execute_tool(make_ctx(), "send_empanelment_link", {"channel": "sms"})
    assert not out.is_error and payload(out)["sent"] is True


def test_handler_crash_becomes_error(make_ctx, monkeypatch):
    def boom(ctx, inp):
        raise RuntimeError("bug")

    monkeypatch.setitem(tools_module._HANDLERS, "verify_arn", boom)
    out = execute_tool(make_ctx(), "verify_arn", {"arn": "ARN-1"})
    assert out.is_error and "internal problem" in payload(out)["error"]


def test_tool_content_is_compact_json(make_ctx):
    out = execute_tool(make_ctx(), "end_call", {"reason": "done"})
    assert out.content == '{"ending":true}' and out.end_call and not out.is_error


# --- verify_arn --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,arn,valid",
    [
        ("ARN 123456", "ARN-123456", True),
        ("123456", "ARN-123456", True),
        ("arn-0123456", "ARN-0123456", True),
        ("A R N 98 76 5", "ARN-98765", True),
        ("ARN-12345678", "ARN-12345678", False),
        ("ARN 0000", "ARN-0000", False),
        ("my arn", None, False),
    ],
)
def test_normalize_arn_input(raw, arn, valid):
    assert normalize_arn_input(raw) == (arn, valid)


@pytest.mark.parametrize("said", ["ARN 123456", "123456", "arn-0123456"])
def test_verify_arn_matches_record(make_ctx, make_distributor, said):
    ctx = make_ctx(distributor=make_distributor(arn="ARN-123456", notes=None))
    out = execute_tool(ctx, "verify_arn", {"arn": said})
    data = payload(out)
    assert not out.is_error
    assert data["valid_format"] is True and data["matches_record"] is True
    assert ctx.distributor.notes is None


def test_verify_arn_mismatch_notes_the_stated_arn(make_ctx, make_distributor):
    make_distributor(arn="ARN-777777", name="Someone Else")
    ctx = make_ctx(distributor=make_distributor(arn="ARN-123456", notes="Existing note"))
    data = payload(execute_tool(ctx, "verify_arn", {"arn": "ARN 777777"}))
    assert data["matches_record"] is False and data["valid_format"] is True
    assert "Someone Else" not in json.dumps(data)  # never reveals who owns the stated ARN
    assert ctx.distributor.notes == f"Existing note\nDistributor stated ARN ARN-777777 on call {ctx.call.id}"
    execute_tool(ctx, "verify_arn", {"arn": "777777"})  # same statement again: no duplicate note
    assert ctx.distributor.notes.count("ARN-777777") == 1


def test_verify_arn_invalid_format(make_ctx):
    ctx = make_ctx()
    data = payload(execute_tool(ctx, "verify_arn", {"arn": "ARN 12345678"}))
    assert data["valid_format"] is False and data["matches_record"] is False and "repeat" in data["hint"]
    assert ctx.distributor.notes is None
    assert execute_tool(ctx, "verify_arn", {"arn": "I don't know"}).is_error


# --- update_distributor_details ----------------------------------------------------------------


def test_update_details_happy_path(make_ctx):
    ctx = make_ctx()
    out = execute_tool(
        ctx,
        "update_distributor_details",
        {
            "email": "Ravi.K@Gmail.com",
            "preferred_language": "hi-IN",
            "alt_phone": "98765 43210",
            "notes": "Prefers mornings",
        },
    )
    data = payload(out)
    assert not out.is_error
    assert data["updated"] == ["email", "preferred_language", "alt_phone", "notes"] and data["rejected"] == {}
    d = ctx.distributor
    assert (
        d.email == "ravi.k@gmail.com" and d.preferred_language == "hi-IN" and d.alt_phone == "+919876543210"
    )
    assert f"Call {ctx.call.id}: Prefers mornings" in d.notes
    assert data["email_on_file"] == "r***@gmail.com" and data["alt_phone_on_file"] == "+91******3210"


def test_update_details_rejections(make_ctx):
    ctx = make_ctx()
    before = (ctx.distributor.email, ctx.distributor.alt_phone)
    out = execute_tool(
        ctx,
        "update_distributor_details",
        {"email": "not-an-email", "preferred_language": "ta-IN", "alt_phone": "12345", "notes": None},
    )
    data = payload(out)
    assert out.is_error and data["updated"] == []
    assert set(data["rejected"]) == {"email", "preferred_language", "alt_phone"}
    assert "en-IN, hi-IN" in data["rejected"]["preferred_language"]
    assert (ctx.distributor.email, ctx.distributor.alt_phone) == before


def test_update_details_partial_and_empty(make_ctx):
    ctx = make_ctx()
    out = execute_tool(
        ctx,
        "update_distributor_details",
        {
            "email": "ravi at gmail dot com",
            "preferred_language": None,
            "alt_phone": ctx.distributor.phone,
            "notes": None,
        },
    )
    assert not out.is_error and ctx.distributor.email == "ravi@gmail.com"
    assert payload(out)["rejected"] == {"alt_phone": "same as the number on this call"}
    empty = {"email": None, "preferred_language": None, "alt_phone": None, "notes": None}
    assert execute_tool(ctx, "update_distributor_details", empty).is_error


def test_email_helpers():
    assert normalize_email(" Ravi.K@Example.co.in ") == "ravi.k@example.co.in"
    assert normalize_email("ravi dot k at example dot com") == "ravi.k@example.com"
    assert normalize_email("ravi@") is None and normalize_email("") is None and normalize_email(None) is None
    assert mask_email("ravi@example.com") == "r***@example.com" and mask_email(None) == "***"


# --- send_empanelment_link ---------------------------------------------------------------------


def test_send_link_by_sms(session, make_ctx, settings, kb):
    ctx = make_ctx()
    out = execute_tool(ctx, "send_empanelment_link", {"channel": "sms", "email": None})
    data = payload(out)
    assert not out.is_error and not out.end_call
    assert data == {"sent": True, "channel": "sms", "destination": "+91******" + ctx.distributor.phone[-4:]}

    row = session.scalars(select(OutboundMessage)).one()
    assert row.channel == MessageChannel.SMS and row.status == MessageStatus.QUEUED
    assert row.destination == ctx.distributor.phone
    assert row.call_id == ctx.call.id and row.distributor_id == ctx.distributor.id
    assert row.link.startswith(f"{settings.base_url}/r/")
    token = row.link.rsplit("/", 1)[1]
    assert links.parse_link_token(settings.secret_key, token) == (ctx.distributor.id, ctx.call.id)
    assert row.body == (
        f"Dear {ctx.distributor.name}, thank you for speaking with Asha from Sample MF. "
        f"Complete your empanelment here: {row.link} . For help call {kb.amc.distributor_helpline}."
    )
    assert ctx.distributor.status == EmpanelmentStatus.LINK_SENT
    assert ctx.call.outcome == CallOutcome.LINK_SENT
    (event,) = audits(session, "link_sent")
    assert event.call_id == ctx.call.id and event.detail["channel"] == "sms"
    assert ctx.distributor.phone not in json.dumps(event.detail)  # masked in the audit trail too


def test_send_link_by_whatsapp_uses_template_vars(make_ctx, settings):
    recorder = RecordingSender(MessageChannel.WHATSAPP)
    ctx = make_ctx(messenger=Messenger({MessageChannel.WHATSAPP: recorder}))
    out = execute_tool(ctx, "send_empanelment_link", {"channel": "whatsapp", "email": None})
    assert not out.is_error
    (msg,) = recorder.sent
    assert msg.to == ctx.distributor.phone and msg.subject is None
    assert msg.template_vars == {"name": ctx.distributor.name, "link": msg.link}
    assert ctx.session.scalars(select(OutboundMessage)).one().status == MessageStatus.SENT


def test_send_link_by_email(make_ctx, make_distributor):
    recorder = RecordingSender(MessageChannel.EMAIL)
    ctx = make_ctx(
        distributor=make_distributor(email=None), messenger=Messenger({MessageChannel.EMAIL: recorder})
    )
    no_email = execute_tool(ctx, "send_empanelment_link", {"channel": "email", "email": None})
    assert no_email.is_error and "No email address on file" in payload(no_email)["error"]
    bad = execute_tool(ctx, "send_empanelment_link", {"channel": "email", "email": "ravi@@x"})
    assert bad.is_error and "not valid" in payload(bad)["error"]
    assert recorder.sent == []

    out = execute_tool(ctx, "send_empanelment_link", {"channel": "email", "email": "Ravi@Example.com"})
    assert payload(out) == {"sent": True, "channel": "email", "destination": "r***@example.com"}
    assert ctx.distributor.email == "ravi@example.com"
    (msg,) = recorder.sent
    assert msg.to == "ravi@example.com" and msg.subject == "Empanelment with Sample Mutual Fund"
    assert msg.template_vars == {}


def test_send_link_uses_email_on_file(make_ctx, make_distributor):
    ctx = make_ctx(distributor=make_distributor(email="onfile@example.com"))
    assert payload(execute_tool(ctx, "send_empanelment_link", {"channel": "email", "email": None}))["sent"]
    assert ctx.session.scalars(select(OutboundMessage)).one().destination == "onfile@example.com"


def test_send_link_failure_suggests_another_channel(session, make_ctx):
    ctx = make_ctx(messenger=Messenger({MessageChannel.SMS: RecordingSender(MessageChannel.SMS, ok=False)}))
    out = execute_tool(ctx, "send_empanelment_link", {"channel": "sms", "email": None})
    data = payload(out)
    assert out.is_error and data["sent"] is False and "whatsapp or email" in data["error"]
    assert session.scalars(select(OutboundMessage)).one().status == MessageStatus.FAILED
    assert ctx.distributor.status == EmpanelmentStatus.NEW
    assert ctx.call.outcome is None and audits(session, "link_sent") == []


def test_fourth_send_is_rejected(session, make_ctx):
    ctx = make_ctx()
    for channel in ("sms", "whatsapp", "email"):
        assert not execute_tool(ctx, "send_empanelment_link", {"channel": channel, "email": None}).is_error
    out = execute_tool(ctx, "send_empanelment_link", {"channel": "sms", "email": None})
    assert out.is_error and "already been sent 3 times" in payload(out)["error"]
    assert len(session.scalars(select(OutboundMessage)).all()) == 3
    assert ctx.call.engine_state["link_sends"] == 3


def test_no_link_after_opt_out(make_ctx):
    ctx = make_ctx(outcome=CallOutcome.OPTED_OUT)
    assert execute_tool(ctx, "send_empanelment_link", {"channel": "sms", "email": None}).is_error


# --- schedule_callback -------------------------------------------------------------------------


def test_schedule_callback_happy_path(session, make_ctx):
    ctx = make_ctx()
    out = execute_tool(
        ctx,
        "schedule_callback",
        {"when_local": "2026-10-14 11:30", "with_rm": True, "notes": "Wants SIP details"},
    )
    assert payload(out) == {
        "scheduled": True,
        "when_spoken": "Wednesday 14th October at 11:30 AM",
        "with_rm": True,
    }
    cb = session.scalars(select(Callback)).one()
    assert cb.scheduled_for == datetime(2026, 10, 14, 6, 0)  # naive UTC
    assert cb.with_rm and cb.notes == "Wants SIP details" and cb.call_id == ctx.call.id
    assert cb.distributor_id == ctx.distributor.id
    assert ctx.distributor.status == EmpanelmentStatus.CALLBACK_SCHEDULED
    assert ctx.call.outcome == CallOutcome.CALLBACK_REQUESTED
    assert audits(session, "callback_scheduled")[0].detail["with_rm"] is True


def test_schedule_callback_rejects_past_time_with_next_slot(session, make_ctx):
    out = execute_tool(
        make_ctx(), "schedule_callback", {"when_local": "2026-10-13 10:30", "with_rm": False, "notes": ""}
    )
    data = payload(out)
    assert out.is_error and "already passed" in data["error"]
    # now is 11:00 IST; the suggestion is one hour later, on a half-hour boundary.
    assert data["next_available_spoken"] == "Tuesday 13th October at 12 PM"
    assert data["next_available_local"] == "2026-10-13 12:00"
    assert session.scalars(select(Callback)).all() == []


@pytest.mark.parametrize(
    "when,spoken,reason",
    [
        ("2026-10-13 20:00", "Wednesday 14th October at 10 AM", "after_window"),
        ("2026-10-14 08:15", "Wednesday 14th October at 10 AM", "before_window"),
        ("2026-10-18 11:00", "Monday 19th October at 10 AM", "non_calling_day"),  # Sunday
        ("2026-10-15 11:00", "Friday 16th October at 10 AM", "holiday"),
    ],
)
def test_schedule_callback_outside_window_suggests_next_slot(session, kb, make_ctx, when, spoken, reason):
    kb.campaign.holidays.append(date(2026, 10, 15))
    out = execute_tool(make_ctx(), "schedule_callback", {"when_local": when, "with_rm": True, "notes": ""})
    data = payload(out)
    assert out.is_error and reason in data["error"]
    assert data["next_available_spoken"] == spoken
    assert session.scalars(select(Callback)).all() == []


def test_schedule_callback_rounds_suggestion_to_half_hour(make_ctx):
    # 18:50 IST is inside the window; one hour later is not, so the next morning is proposed.
    ctx = make_ctx(now_utc=datetime(2026, 10, 13, 13, 20))
    data = payload(
        execute_tool(
            ctx, "schedule_callback", {"when_local": "2026-10-13 18:00", "with_rm": True, "notes": ""}
        )
    )
    assert data["next_available_local"] == "2026-10-14 10:00"
    ctx2 = make_ctx(now_utc=datetime(2026, 10, 13, 5, 37))  # 11:07 IST -> 12:07 -> 12:30
    data2 = payload(
        execute_tool(
            ctx2, "schedule_callback", {"when_local": "2026-10-13 09:00", "with_rm": True, "notes": ""}
        )
    )
    assert data2["next_available_local"] == "2026-10-13 12:30"


@pytest.mark.parametrize("when", ["tomorrow 11am", "14/10/2026 11:30", "2026-13-01 10:00"])
def test_schedule_callback_bad_format(make_ctx, when):
    out = execute_tool(make_ctx(), "schedule_callback", {"when_local": when, "with_rm": True, "notes": ""})
    assert out.is_error and "YYYY-MM-DD HH:MM" in payload(out)["error"]


def test_schedule_callback_too_far_ahead(make_ctx):
    out = execute_tool(
        make_ctx(), "schedule_callback", {"when_local": "2027-10-14 11:00", "with_rm": True, "notes": ""}
    )
    assert out.is_error and "days away" in payload(out)["error"]


def test_schedule_callback_twice_reschedules(session, make_ctx):
    ctx = make_ctx()
    execute_tool(ctx, "schedule_callback", {"when_local": "2026-10-14 11:30", "with_rm": True, "notes": "a"})
    execute_tool(ctx, "schedule_callback", {"when_local": "2026-10-15 15:00", "with_rm": False, "notes": "b"})
    cb = session.scalars(select(Callback)).one()
    assert cb.scheduled_for == datetime(2026, 10, 15, 9, 30) and cb.with_rm is False and cb.notes == "b"


def test_schedule_callback_keeps_stronger_outcome(make_ctx):
    ctx = make_ctx(outcome=CallOutcome.LINK_SENT)
    assert not execute_tool(
        ctx, "schedule_callback", {"when_local": "2026-10-14 11:30", "with_rm": True, "notes": ""}
    ).is_error
    assert ctx.call.outcome == CallOutcome.LINK_SENT


# --- set_language / transfer / opt_out / end_call ----------------------------------------------


def test_set_language(make_ctx):
    ctx = make_ctx(language="en-IN")
    assert payload(execute_tool(ctx, "set_language", {"language": "hi-IN"})) == {
        "language": "hi-IN",
        "name": "Hindi",
    }
    assert ctx.call.language == "hi-IN"
    out = execute_tool(ctx, "set_language", {"language": "ta-IN"})
    assert out.is_error and ctx.call.language == "hi-IN"


def test_transfer_without_rm_number(session, make_ctx):
    ctx = make_ctx()
    out = execute_tool(ctx, "transfer_to_human", {"reason": "wants a person"})
    assert payload(out) == {"available": False, "suggestion": "offer a callback with schedule_callback"}
    assert out.transfer_to is None and not out.is_error and ctx.call.outcome is None


def test_transfer_with_rm_number(session, make_ctx, settings):
    rm_settings = settings.model_copy(update={"rm_transfer_number": "+919000000001"})
    ctx = make_ctx(settings_=rm_settings)
    out = execute_tool(ctx, "transfer_to_human", {"reason": "wants a person"})
    assert out.transfer_to == "+919000000001" and not out.end_call
    assert ctx.call.outcome == CallOutcome.TRANSFERRED
    assert audits(session, "transfer")[0].detail["reason"] == "wants a person"


def test_transfer_outside_calling_window(make_ctx, settings):
    rm_settings = settings.model_copy(update={"rm_transfer_number": "+919000000001"})
    ctx = make_ctx(settings_=rm_settings, now_utc=datetime(2026, 10, 13, 15, 0))  # 20:30 IST
    out = execute_tool(ctx, "transfer_to_human", {"reason": "wants a person"})
    assert out.transfer_to is None and payload(out)["available"] is False


def test_opt_out(session, make_ctx, make_distributor):
    ctx = make_ctx(distributor=make_distributor(alt_phone="+919811112222"))
    out = execute_tool(ctx, "opt_out", {"reason": "Asked to stop calling"})
    assert out.end_call and not out.is_error and payload(out)["opted_out"] is True
    d = ctx.distributor
    assert d.do_not_call and d.status == EmpanelmentStatus.DO_NOT_CALL
    assert ctx.call.outcome == CallOutcome.OPTED_OUT
    entries = {e.phone: e for e in session.scalars(select(DNCEntry))}
    assert set(entries) == {d.phone, d.alt_phone}
    assert entries[d.phone].source == "call_opt_out" and entries[d.phone].reason == "Asked to stop calling"
    assert is_dnc(session, d.phone) and is_dnc(session, d.alt_phone)
    assert audits(session, "opt_out")[0].call_id == ctx.call.id
    # Idempotent: a second opt-out does not fail.
    assert execute_tool(ctx, "opt_out", {"reason": "again"}).end_call


def test_end_call(make_ctx):
    out = execute_tool(make_ctx(), "end_call", {"reason": "goodbye said"})
    assert out.end_call and out.transfer_to is None


# --- record_outcome ----------------------------------------------------------------------------


def _record(ctx, outcome, *, level="warm", summary="Discussed the NFO.", objections=()):
    return execute_tool(
        ctx,
        "record_outcome",
        {
            "outcome": outcome,
            "interest_level": level,
            "summary_for_rm": summary,
            "objections": list(objections),
        },
    )


def test_record_outcome_sets_fields(make_ctx):
    ctx = make_ctx()
    out = _record(
        ctx,
        "not_interested",
        level="cold",
        summary="Works with two AMCs only.",
        objections=["too many AMCs", " "],
    )
    assert payload(out) == {"recorded": True, "outcome": "not_interested"}
    assert ctx.call.outcome == CallOutcome.NOT_INTERESTED and ctx.call.interest_level == InterestLevel.COLD
    assert ctx.call.summary == "Works with two AMCs only.\nObjections: too many AMCs"
    _record(ctx, "no_outcome", level=None, summary="")
    assert ctx.call.interest_level is None and ctx.call.summary is None


@pytest.mark.parametrize("locked", [CallOutcome.OPTED_OUT, CallOutcome.TRANSFERRED])
def test_record_outcome_never_overwrites_locked_outcomes(make_ctx, locked):
    ctx = make_ctx(outcome=locked)
    data = payload(_record(ctx, "interested"))
    assert ctx.call.outcome == locked and data["outcome"] == locked.value and "note" in data


def test_record_outcome_does_not_downgrade_link_sent(make_ctx):
    ctx = make_ctx()
    execute_tool(ctx, "send_empanelment_link", {"channel": "sms", "email": None})
    data = payload(_record(ctx, "interested", level="hot"))
    assert ctx.call.outcome == CallOutcome.LINK_SENT and data["outcome"] == "link_sent"
    assert ctx.call.interest_level == InterestLevel.HOT
    _record(ctx, "link_sent")
    assert ctx.call.outcome == CallOutcome.LINK_SENT


def test_record_outcome_link_sent_requires_a_sent_link(make_ctx):
    ctx = make_ctx()
    data = payload(_record(ctx, "link_sent"))
    assert ctx.call.outcome == CallOutcome.INTERESTED and "No link was sent" in data["note"]


def test_record_outcome_dispositions_replace_progress(make_ctx):
    ctx = make_ctx(outcome=CallOutcome.INTERESTED)
    _record(ctx, "already_empanelled")
    assert ctx.call.outcome == CallOutcome.ALREADY_EMPANELLED
    _record(ctx, "wrong_person", level=None)
    assert ctx.call.outcome == CallOutcome.WRONG_PERSON


# --- log_request -------------------------------------------------------------------------------


def test_log_request_creates_follow_up_for_the_team(session, make_ctx):
    ctx = make_ctx()
    out = execute_tool(ctx, "log_request", {"kind": "rm_request", "details": "Wants an RM for Pune"})
    assert payload(out)["logged"] is True and not out.is_error and not out.end_call
    req = session.scalars(select(Callback)).one()
    assert req.kind == FollowUpKind.RM_REQUEST and req.with_rm
    assert req.scheduled_for == IN_WINDOW_UTC and req.notes == "Wants an RM for Pune"
    assert req.call_id == ctx.call.id and req.distributor_id == ctx.distributor.id
    assert ctx.call.outcome == CallOutcome.INTERESTED
    assert ctx.distributor.status == EmpanelmentStatus.INTERESTED
    assert audits(session, "request_logged")[0].detail["request_kind"] == "rm_request"


def test_log_request_same_kind_refreshes_note(session, make_ctx):
    ctx = make_ctx()
    execute_tool(ctx, "log_request", {"kind": "commission_query", "details": "first"})
    execute_tool(ctx, "log_request", {"kind": "commission_query", "details": "second"})
    execute_tool(ctx, "log_request", {"kind": "collateral_request", "details": "single pager"})
    rows = {r.kind: r.notes for r in session.scalars(select(Callback))}
    assert rows == {FollowUpKind.COMMISSION_QUERY: "second", FollowUpKind.COLLATERAL_REQUEST: "single pager"}


def test_log_request_keeps_stronger_outcome(make_ctx):
    ctx = make_ctx(outcome=CallOutcome.ALREADY_EMPANELLED)
    execute_tool(ctx, "log_request", {"kind": "collateral_request", "details": "deck"})
    assert ctx.call.outcome == CallOutcome.ALREADY_EMPANELLED


def test_log_request_rejects_unknown_kind(make_ctx):
    out = execute_tool(make_ctx(), "log_request", {"kind": "pizza", "details": "x"})
    assert out.is_error


def test_schedule_callback_does_not_overwrite_logged_request(session, make_ctx):
    ctx = make_ctx()
    execute_tool(ctx, "log_request", {"kind": "email_issue", "details": "No email received"})
    execute_tool(ctx, "schedule_callback", {"when_local": "2026-10-14 11:30", "with_rm": True, "notes": "cb"})
    kinds = sorted(r.kind.value for r in session.scalars(select(Callback)))
    assert kinds == ["callback", "email_issue"]

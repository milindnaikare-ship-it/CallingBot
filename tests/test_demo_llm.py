"""Tests for callingbot.agent.demo_llm.DemoLLM, mostly through the real ConversationEngine."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from conftest import IN_WINDOW_UTC
from sqlalchemy import select

from callingbot.agent.demo_llm import DemoLLM
from callingbot.agent.engine import ConversationEngine
from callingbot.agent.llm import build_llm
from callingbot.agent.prompts import build_call_context, build_system_prompt
from callingbot.agent.tools import build_tool_definitions
from callingbot.compliance import is_dnc, screen_bot_utterance
from callingbot.messaging import build_messenger
from callingbot.models import (
    Call,
    Callback,
    CallOutcome,
    CallStatus,
    EmpanelmentStatus,
    MessageChannel,
    OutboundMessage,
    Turn,
    TurnRole,
)
from callingbot.timeutil import to_local


@pytest.fixture
def run_call(session, kb, settings, make_distributor):
    """``run_call(["yes", ...], now=..., **distributor_kw) -> (call, responses)``."""

    def _run(utterances, *, now=IN_WINDOW_UTC, **distributor_kw):
        d = make_distributor(**distributor_kw)
        call = Call(distributor_id=d.id, provider="simulator", status=CallStatus.RINGING)
        session.add(call)
        session.flush()
        engine = ConversationEngine(
            session=session,
            kb=kb,
            settings=settings,
            llm=DemoLLM(kb),
            messenger=build_messenger(settings),
            now=lambda: now,
        )
        responses = [engine.start(call, answered_by="human")]
        for text in utterances:
            responses.append(engine.handle_input(call, text))
        return call, responses

    return _run


def bot_lines(session, call) -> list[str]:
    turns = session.scalars(
        select(Turn).where(Turn.call_id == call.id, Turn.role == TurnRole.BOT).order_by(Turn.id)
    )
    return [t.text for t in turns]


def assert_clean(session, call) -> None:
    for line in bot_lines(session, call):
        assert screen_bot_utterance(line).ok, line
    assert not session.scalars(select(Turn).where(Turn.call_id == call.id, Turn.flagged.is_(True))).all()


def test_demo_llm_basics(kb):
    llm = DemoLLM(kb)
    assert llm.model == "demo" and llm.supports_system_messages is False


def test_build_llm_fake_returns_demo(settings):
    assert isinstance(build_llm(settings), DemoLLM)


def test_full_whatsapp_empanelment_conversation(session, kb, run_call):
    call, responses = run_call(["yes speaking", "no I'm not empanelled", "yes send it on whatsapp"])
    greeting, pitch, offer, close = responses

    assert "virtual assistant" in greeting.say[0]
    assert kb.nfo.scheme_name in pitch.say[0] and pitch.say[0].endswith("Are you already empanelled with us?")
    assert "20th October" in pitch.say[0] and pitch.action == "gather"
    assert offer.say[0].endswith("Shall I send you the empanelment link by SMS?")
    assert close.action == "hangup"
    assert close.say[0].startswith("Done, I've sent you the empanelment link by WhatsApp.")
    assert kb.nfo.disclaimer("en-IN") in close.say[0] and len(close.say) == 1  # disclaimer spoken once

    assert call.outcome == CallOutcome.LINK_SENT and call.interest_level.value == "warm"
    assert "WhatsApp" in call.summary
    message = session.scalars(select(OutboundMessage)).one()
    assert message.channel == MessageChannel.WHATSAPP and message.destination == call.distributor.phone
    assert message.call_id == call.id and message.link
    assert call.distributor.status == EmpanelmentStatus.LINK_SENT
    assert call.engine_state["ended"] is True and call.pending_action == "hangup"
    assert_clean(session, call)


def test_plain_yes_to_the_offer_sends_sms(session, run_call):
    call, responses = run_call(["yes", "not yet", "yes please"])
    assert responses[-1].action == "hangup"
    assert session.scalars(select(OutboundMessage)).one().channel == MessageChannel.SMS
    assert call.outcome == CallOutcome.LINK_SENT


def test_email_with_spoken_address(session, run_call):
    call, _ = run_call(["yes", "no", "please email it to Ravi.K@Example.com"], email=None)
    message = session.scalars(select(OutboundMessage)).one()
    assert message.channel == MessageChannel.EMAIL and message.destination == "ravi.k@example.com"
    assert call.distributor.email == "ravi.k@example.com"


def test_email_failure_offers_another_channel(session, run_call):
    call, responses = run_call(["yes", "no", "send it by email"], email=None)
    assert responses[-1].action == "gather"
    assert "Shall I send it by SMS instead?" in responses[-1].say[0]
    assert session.scalars(select(OutboundMessage)).all() == []
    assert_clean(session, call)


def test_not_interested(session, kb, run_call):
    call, responses = run_call(["yes speaking", "I'm not interested"])
    final = responses[-1]
    assert final.action == "hangup" and final.say[0].startswith("No problem at all")
    assert final.say[-1] == kb.nfo.disclaimer("en-IN")  # engine closes with the disclaimer
    assert call.outcome == CallOutcome.NOT_INTERESTED
    assert_clean(session, call)


def test_opt_out(session, run_call):
    call, responses = run_call(["yes", "please stop calling me"])
    final = responses[-1]
    assert final.action == "hangup" and "won't call you again" in final.say[0] and len(final.say) == 1
    assert call.outcome == CallOutcome.OPTED_OUT and is_dnc(session, call.distributor.phone)
    assert call.distributor.status == EmpanelmentStatus.DO_NOT_CALL


def test_wrong_number(run_call):
    call, responses = run_call(["sorry, wrong number"])
    assert responses[-1].action == "hangup" and len(responses[-1].say) == 1  # no disclaimer
    assert call.outcome == CallOutcome.WRONG_PERSON


def test_already_empanelled(session, kb, run_call):
    call, responses = run_call(["yes", "yes, already empanelled"])
    final = responses[-1]
    assert final.action == "hangup" and "marketing kit" in final.say[0]
    assert final.say == [final.say[0]] and kb.nfo.disclaimer("en-IN") in final.say[0]
    assert call.outcome == CallOutcome.ALREADY_EMPANELLED


def test_busy_schedules_callback_tomorrow(session, run_call):
    call, responses = run_call(["I'm busy, call me tomorrow"])
    final = responses[-1]
    assert final.action == "hangup" and "Wednesday 14th October at 11 AM" in final.say[0]
    cb = session.scalars(select(Callback)).one()
    assert cb.scheduled_for == datetime(2026, 10, 14, 5, 30) and cb.with_rm  # 11:00 IST
    assert call.outcome == CallOutcome.CALLBACK_REQUESTED
    assert call.distributor.status == EmpanelmentStatus.CALLBACK_SCHEDULED


def test_busy_late_in_the_day_moves_to_next_window(session, run_call):
    late = datetime(2026, 10, 13, 13, 0)  # 18:30 IST: two hours later is after the window
    call, responses = run_call(["yes", "I'm driving, call me later"], now=late)
    assert "Wednesday 14th October at 10 AM" in responses[-1].say[0]
    assert session.scalars(select(Callback)).one().scheduled_for == datetime(2026, 10, 14, 4, 30)


@pytest.mark.parametrize(
    "question,expected",
    [
        ("What is the commission?", "no upfront commission"),
        ("Which documents do I need?", "PAN card copy"),
        ("Who is the fund manager?", "Fund Manager Name 1 and Fund Manager Name 2"),
        ("What is the exit load?", "1% if redeemed within 1 year"),
        ("What is the minimum investment?", "Rs 5,000"),
        ("When does it close?", "closes on 3rd November"),
        ("What is the benchmark?", "Nifty 500 TRI"),
        ("Is it risky?", "Very High"),
        ("Tell me about the weather", "I can share details about the NFO"),
    ],
)
def test_questions_are_answered_from_the_knowledge_base(session, kb, run_call, question, expected):
    call, responses = run_call(["yes", question])
    reply = " ".join(responses[-1].say)
    assert expected in reply and responses[-1].action == "gather"
    if expected not in ("no upfront commission", "PAN card copy", "I can share details about the NFO"):
        assert kb.nfo.disclaimer("en-IN") in reply  # scheme features come with the disclaimer
    assert_clean(session, call)


def test_switch_to_hindi(session, kb, run_call):
    call, responses = run_call(["Can we talk in Hindi?", "नहीं", "हाँ, व्हाट्सएप पर भेज दीजिए"])
    pitch = responses[1]
    assert pitch.language == "hi-IN" and pitch.stt_language == "hi-IN"
    assert "क्या आप पहले से हमारे साथ empanelled हैं?" in pitch.say[0]
    assert re_devanagari(pitch.say[0])
    assert "SMS से भेज दूँ" in responses[2].say[0]
    final = responses[3]
    assert final.action == "hangup" and kb.nfo.disclaimer("hi-IN") in final.say[0]
    assert session.scalars(select(OutboundMessage)).one().channel == MessageChannel.WHATSAPP
    assert call.language == "hi-IN" and call.outcome == CallOutcome.LINK_SENT
    assert_clean(session, call)


def re_devanagari(text: str) -> bool:
    return any("ऀ" <= ch <= "ॿ" for ch in text)


def test_declining_link_and_callback(run_call):
    call, responses = run_call(["yes", "no", "no thanks", "no"])
    assert "call back from our relationship manager" in responses[3].say[0]
    assert responses[-1].action == "hangup" and call.outcome == CallOutcome.NOT_INTERESTED


def test_accepting_callback_after_declining_link(session, run_call):
    call, responses = run_call(["yes", "no", "no", "yes please"])
    assert responses[-1].action == "hangup" and call.outcome == CallOutcome.CALLBACK_REQUESTED
    assert session.scalars(select(Callback)).one().with_rm


# --- direct protocol checks -------------------------------------------------------------------------


def _context_messages(kb, session, make_distributor) -> list[dict]:
    d = make_distributor()
    call = Call(distributor_id=d.id, provider="simulator", language="en-IN")
    session.add(call)
    session.flush()
    context = build_call_context(kb, d, call, to_local(IN_WINDOW_UTC, "Asia/Kolkata"))
    return [
        {"role": "user", "content": context},
        {"role": "assistant", "content": [{"type": "text", "text": "Hello! Am I speaking with you?"}]},
    ]


def _complete(llm, kb, messages):
    return llm.complete(system=build_system_prompt(kb), tools=build_tool_definitions(kb), messages=messages)


def test_operator_notes_are_ignored(kb, session, make_distributor):
    messages = _context_messages(kb, session, make_distributor)
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "yes speaking"},
                {"type": "text", "text": "[Operator note] Wrong number, not interested, stop calling"},
            ],
        }
    )
    result = _complete(DemoLLM(kb), kb, messages)
    assert result.stop_reason == "end_turn" and result.tool_calls == []
    assert "Are you already empanelled with us?" in result.text


def test_callback_error_retries_the_suggested_slot(kb, session, make_distributor):
    messages = _context_messages(kb, session, make_distributor)
    use = {"type": "tool_use", "id": "toolu_x", "name": "schedule_callback"}
    use["input"] = {"when_local": "2026-10-13 20:00", "with_rm": True, "notes": ""}
    error = {"error": "outside hours", "next_available_local": "2026-10-14 10:00"}
    messages += [
        {"role": "user", "content": "busy"},
        {"role": "assistant", "content": [use]},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_x",
                    "content": json.dumps(error),
                    "is_error": True,
                }
            ],
        },
    ]
    result = _complete(DemoLLM(kb), kb, messages)
    (call,) = result.tool_calls
    assert call.name == "schedule_callback" and call.input["when_local"] == "2026-10-14 10:00"
    assert call.id != "toolu_x" and result.content[-1]["id"] == call.id

    # The same suggestion failing again falls back to an RM callback without a slot.
    use2 = {"type": "tool_use", "id": "toolu_y", "name": "schedule_callback", "input": call.input}
    messages += [
        {"role": "assistant", "content": [use2]},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_y",
                    "content": json.dumps(error),
                    "is_error": True,
                }
            ],
        },
    ]
    final = _complete(DemoLLM(kb), kb, messages)
    assert [c.name for c in final.tool_calls] == ["record_outcome", "end_call"]
    assert "relationship manager" in final.text


def test_tool_ids_are_unique_within_a_call(session, run_call):
    call, _ = run_call(["yes", "no", "yes send it on whatsapp"])
    ids = [
        b["id"]
        for m in call.llm_messages
        if m["role"] == "assistant"
        for b in m["content"]
        if b.get("type") == "tool_use"
    ]
    assert len(ids) == 3 and len(set(ids)) == 3  # send, record_outcome, end_call
    assert all(i.startswith("toolu_demo_") for i in ids)

"""Tests for callingbot.agent.prompts: system prompt, call context and spoken-date helpers."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import FIXTURE_CONFIG, IN_WINDOW_UTC

from callingbot.agent.prompts import (
    CALL_CONTEXT_HEADER,
    build_call_context,
    build_system_prompt,
    nfo_phase,
    ordinal,
    render_greeting,
    spoken_date,
    spoken_datetime,
    spoken_time,
)
from callingbot.knowledge import load_knowledge
from callingbot.models import Call, CallOutcome, CallStatus, EmpanelmentStatus
from callingbot.timeutil import to_local

IST = ZoneInfo("Asia/Kolkata")


def _squash(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def call_for(session):
    def _make(distributor, **kw) -> Call:
        call = Call(distributor_id=distributor.id, provider="simulator", **kw)
        session.add(call)
        session.flush()
        return call

    return _make


# --- system prompt -----------------------------------------------------------------------------


def test_system_prompt_is_deterministic(kb):
    first = build_system_prompt(kb)
    assert first == build_system_prompt(kb)
    # A freshly loaded knowledge base (new objects, same files) gives byte-identical text.
    assert first == build_system_prompt(load_knowledge(FIXTURE_CONFIG))


def test_system_prompt_contains_approved_knowledge(kb):
    prompt = build_system_prompt(kb)
    squashed = _squash(prompt)
    assert kb.amc.name in prompt
    assert kb.amc.bot_name in prompt
    assert kb.nfo.scheme_name in prompt
    assert kb.nfo.category in prompt
    assert kb.nfo.benchmark in prompt
    assert kb.nfo.exit_load in prompt
    assert kb.nfo.riskometer in prompt
    assert all(manager in prompt for manager in kb.nfo.fund_managers)
    assert _squash(kb.nfo.commission_response) in squashed
    for lang in kb.amc.languages:
        assert _squash(kb.nfo.disclaimer(lang.code)) in squashed
        assert f"{lang.name} ({lang.code})" in prompt
    for faq in kb.faqs:
        assert _squash(faq.question) in squashed
        assert _squash(faq.answer) in squashed
    for item in [*kb.amc.distributor_value_props, *kb.amc.empanelment_documents, *kb.amc.empanelment_steps]:
        assert _squash(item) in squashed
    assert "Tuesday 20th October 2026" in prompt  # NFO dates rendered for speech


def test_system_prompt_covers_key_rules(kb):
    prompt = build_system_prompt(kb).lower()
    for phrase in (
        "virtual assistant",
        "opt_out",
        "record_outcome",
        "end_call",
        "guaranteed",
        "no performance history",
        "pan, bank details, aadhaar, otps",
        "devanagari",
        "[operator note]",
        "speech-to-text transcript",
        "wrong_person",
        "investment adviser",
    ):
        assert phrase in prompt, phrase


def test_system_prompt_has_no_per_call_data(kb, make_distributor):
    d = make_distributor(name="Ravi Kumar", phone="+919812345678", email="ravi.kumar@example.com")
    prompt = build_system_prompt(kb)
    assert d.name not in prompt and d.phone not in prompt and d.email not in prompt
    assert "+91" not in prompt
    # The only email address in the prompt is the AMC's own partner desk.
    assert set(re.findall(r"[\w.+-]+@[\w.-]+", prompt)) == {kb.amc.distributor_email}
    assert not re.search(r"\b20\d\d-\d\d-\d\d \d\d:\d\d\b", prompt)  # no timestamps


def test_system_prompt_follows_the_knowledge_base(kb):
    changed = kb.model_copy(deep=True)
    changed.nfo.scheme_name = "Another Scheme"
    changed.amc.languages = [lang for lang in changed.amc.languages if lang.code == "en-IN"]
    prompt = build_system_prompt(changed)
    assert "Another Scheme" in prompt and kb.nfo.scheme_name not in prompt
    assert "Devanagari" not in prompt  # Hindi guidance only when Hindi is configured
    assert "Supported languages: English (en-IN)." in prompt


# --- call context ------------------------------------------------------------------------------


def test_call_context_contents(kb, make_distributor, call_for):
    d = make_distributor(
        name="Ravi Kumar",
        firm_name="Kumar Wealth",
        city="Nagpur",
        phone="+919812345678",
        email="ravi@example.com",
    )
    call = call_for(d, language="en-IN")
    text = build_call_context(kb, d, call, to_local(IN_WINDOW_UTC, "Asia/Kolkata"))
    assert text.startswith(CALL_CONTEXT_HEADER)
    assert "Tuesday 13th October 2026, 11 AM IST (2026-10-13 11:00)" in text
    assert "The NFO opens in 7 days" in text
    assert "Ravi Kumar of Kumar Wealth, Nagpur" in text
    assert d.arn in text
    assert "Funnel status: new" in text
    assert "Previous calls: none." in text
    assert "Call language: English (en-IN)." in text
    assert render_greeting(kb, "en-IN", d) in text
    assert "next user message is the distributor's reply" in text
    # PII minimisation: never the phone number or the email address itself.
    assert d.phone not in text and "9812345678" not in text and d.email not in text
    assert "email address - yes" in text


def test_call_context_without_email_and_in_hindi(kb, make_distributor, call_for):
    d = make_distributor(email=None, status=EmpanelmentStatus.LINK_SENT)
    call = call_for(d, language="hi-IN")
    text = build_call_context(kb, d, call, datetime(2026, 10, 13, 11, 0))  # naive = local wall time
    assert "email address - no" in text
    assert "Call language: Hindi (hi-IN)." in text
    assert "link_sent - the empanelment link was sent earlier" in text
    assert "(2026-10-13 11:00)" in text
    assert "नमस्ते" in text  # the Hindi greeting is the one quoted


def test_call_context_lists_last_three_prior_calls(kb, make_distributor, call_for):
    d = make_distributor()
    base = datetime(2026, 10, 1, 6, 0)
    for i in range(4):
        call_for(
            d,
            created_at=base + timedelta(days=i),
            status=CallStatus.COMPLETED if i % 2 else CallStatus.NO_ANSWER,
            answered_at=base + timedelta(days=i) if i % 2 else None,
            outcome=CallOutcome.CALLBACK_REQUESTED if i == 3 else None,
            summary=f"Summary number {i}" if i % 2 else None,
        )
    current = call_for(d, created_at=base + timedelta(days=10))
    text = build_call_context(kb, d, current, to_local(IN_WINDOW_UTC, "Asia/Kolkata"))
    assert "Previous calls: 4 (the last 3, most recent first):" in text
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(lines) == 3
    assert lines[0].startswith(
        "- 4th October 2026: answered; outcome callback_requested; summary: Summary number 3"
    )
    assert lines[1] == "- 3rd October 2026: not answered (no_answer)"
    assert "Summary number 1" in lines[2]
    assert "1st October" not in text  # oldest call dropped


@pytest.mark.parametrize(
    "today,expected",
    [
        (date(2026, 10, 13), "The NFO opens in 7 days, on Tuesday 20th October 2026"),
        (date(2026, 10, 19), "The NFO opens tomorrow"),
        (date(2026, 10, 20), "The NFO is open now and closes on Tuesday 3rd November 2026."),
        (date(2026, 10, 25), "The NFO is open now and closes on Tuesday 3rd November 2026."),
        (date(2026, 11, 3), "The NFO is open now and closes today."),
        (date(2026, 11, 4), "The NFO period has ended"),
    ],
)
def test_nfo_phase(kb, today, expected):
    assert expected in nfo_phase(kb, today)


def test_call_context_after_nfo_focuses_on_empanelment(kb, make_distributor, call_for):
    d = make_distributor()
    text = build_call_context(kb, d, call_for(d), datetime(2026, 11, 20, 12, 0, tzinfo=IST))
    assert "focus on empanelment for future schemes" in text


# --- greeting and spoken helpers ---------------------------------------------------------------


def test_render_greeting(kb, make_distributor):
    d = make_distributor(name="Ravi Kumar")
    en = render_greeting(kb, "en-IN", d)
    assert en.startswith("Hello! This is Asha, a virtual assistant calling on behalf of Sample Mutual Fund.")
    assert en.endswith("Am I speaking with Ravi Kumar?")
    assert "{" not in en and "\n" not in en
    assert "Ravi Kumar जी" in render_greeting(kb, "hi-IN", d)
    assert render_greeting(kb, "xx-XX", d) == en  # unknown language -> default


def test_render_greeting_falls_back_when_no_script(kb, make_distributor):
    changed = kb.model_copy(deep=True)
    changed.amc.languages[0].greeting = None
    text = render_greeting(changed, "en-IN", make_distributor(name="Meera"))
    assert "virtual assistant" in text and "recorded" in text and text.endswith("Meera?")


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "1st"),
        (2, "2nd"),
        (3, "3rd"),
        (4, "4th"),
        (11, "11th"),
        (12, "12th"),
        (13, "13th"),
        (21, "21st"),
        (22, "22nd"),
        (23, "23rd"),
        (31, "31st"),
        (111, "111th"),
    ],
)
def test_ordinal(n, expected):
    assert ordinal(n) == expected


def test_spoken_date_and_time():
    assert spoken_date(date(2026, 10, 20)) == "20th October"
    assert spoken_date(date(2026, 10, 20), weekday=True, year=True) == "Tuesday 20th October 2026"
    assert spoken_time(time(11, 0)) == "11 AM"
    assert spoken_time(time(11, 30)) == "11:30 AM"
    assert spoken_time(time(0, 5)) == "12:05 AM"
    assert spoken_time(time(12, 0)) == "12 PM"
    assert spoken_time(time(16, 5)) == "4:05 PM"
    assert spoken_datetime(datetime(2026, 10, 14, 11, 30)) == "Wednesday 14th October at 11:30 AM"

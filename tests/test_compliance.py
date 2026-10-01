"""Tests for callingbot.compliance: calling window, DNC list, utterance screen, opt-out detection."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from conftest import IN_WINDOW_UTC
from sqlalchemy import func, select

from callingbot.compliance import (
    RULE_IDS,
    ScreenResult,
    WindowDecision,
    add_to_dnc,
    check_calling_window,
    detect_opt_out,
    is_dnc,
    next_window_start,
    safe_reply,
    screen_bot_utterance,
)
from callingbot.knowledge import CampaignPolicy
from callingbot.models import AuditEvent, DNCEntry, EmpanelmentStatus

# --------------------------------------------------------------------------------------------
# Calling window (default policy: Mon-Sat 10:00-19:00 IST; IST = UTC+05:30)
# --------------------------------------------------------------------------------------------


@pytest.fixture
def policy(kb) -> CampaignPolicy:
    # The real campaign.yaml: Mon-Sat, 10:00-19:00, holidays 2026-10-02 and 2026-12-25.
    return kb.campaign


def test_inside_window_allowed(policy):
    assert check_calling_window(policy, IN_WINDOW_UTC) == WindowDecision(True, None, None)


def test_exactly_at_window_start_is_allowed(policy):
    # Tuesday 10:00 IST == 04:30 UTC
    assert check_calling_window(policy, datetime(2026, 10, 13, 4, 30)).allowed


def test_exactly_at_window_end_is_not_allowed(policy):
    # Tuesday 19:00 IST == 13:30 UTC; the window is half-open.
    decision = check_calling_window(policy, datetime(2026, 10, 13, 13, 30))
    assert not decision.allowed
    assert decision.reason == "after_window"
    assert decision.next_allowed_utc == datetime(2026, 10, 14, 4, 30)  # Wednesday 10:00 IST


def test_one_minute_before_window_end_is_allowed(policy):
    assert check_calling_window(policy, datetime(2026, 10, 13, 13, 29)).allowed


def test_before_window_same_day(policy):
    # Tuesday 09:00 IST == 03:30 UTC -> opens at 10:00 IST the same day.
    decision = check_calling_window(policy, datetime(2026, 10, 13, 3, 30))
    assert decision == WindowDecision(False, "before_window", datetime(2026, 10, 13, 4, 30))


def test_after_window_moves_to_next_day(policy):
    # Tuesday 19:30 IST == 14:00 UTC
    decision = check_calling_window(policy, datetime(2026, 10, 13, 14, 0))
    assert decision == WindowDecision(False, "after_window", datetime(2026, 10, 14, 4, 30))


def test_sunday_is_non_calling_day(policy):
    # Sunday 2026-10-18 12:00 IST == 06:30 UTC
    decision = check_calling_window(policy, datetime(2026, 10, 18, 6, 30))
    assert decision == WindowDecision(False, "non_calling_day", datetime(2026, 10, 19, 4, 30))


def test_saturday_evening_rolls_to_monday_morning(policy):
    # Saturday 2026-10-17 20:00 IST == 14:30 UTC -> Monday 2026-10-19 10:00 IST == 04:30 UTC
    decision = check_calling_window(policy, datetime(2026, 10, 17, 14, 30))
    assert decision.reason == "after_window"
    assert decision.next_allowed_utc == datetime(2026, 10, 19, 4, 30)


def test_holiday_blocks_whole_day(policy):
    # Friday 2026-10-02 (Gandhi Jayanti) 11:00 IST -> Saturday 2026-10-03 10:00 IST
    decision = check_calling_window(policy, datetime(2026, 10, 2, 5, 30))
    assert decision == WindowDecision(False, "holiday", datetime(2026, 10, 3, 4, 30))


def test_ist_offset_utc_monday_night_is_ist_tuesday_early_morning(policy):
    # Monday 20:00 UTC == Tuesday 01:30 IST: the IST date/weekday must be used, not the UTC one.
    decision = check_calling_window(policy, datetime(2026, 10, 12, 20, 0))
    assert decision == WindowDecision(False, "before_window", datetime(2026, 10, 13, 4, 30))


def test_ist_offset_utc_saturday_evening_is_ist_sunday(policy):
    # Saturday 19:00 UTC == Sunday 00:30 IST -> non-calling day (not "after_window" Saturday).
    decision = check_calling_window(policy, datetime(2026, 10, 17, 19, 0))
    assert decision.reason == "non_calling_day"
    assert decision.next_allowed_utc == datetime(2026, 10, 19, 4, 30)


def test_aware_datetime_is_accepted(policy):
    aware = IN_WINDOW_UTC.replace(tzinfo=UTC)
    assert check_calling_window(policy, aware).allowed
    assert next_window_start(policy, aware) == IN_WINDOW_UTC


def test_next_window_start_inside_window_returns_same_instant(policy):
    assert next_window_start(policy, IN_WINDOW_UTC) == IN_WINDOW_UTC


def test_next_window_start_is_naive_utc(policy):
    result = next_window_start(policy, datetime(2026, 10, 18, 6, 30))
    assert result.tzinfo is None
    assert result == datetime(2026, 10, 19, 4, 30)


def test_next_window_start_custom_window_and_timezone():
    policy = CampaignPolicy(calling_days=[0, 1, 2, 3, 4], window_start=time(9, 30), window_end=time(18, 0))
    # Friday 18:00 IST -> Monday 09:30 IST == 04:00 UTC
    assert next_window_start(policy, datetime(2026, 10, 16, 12, 30)) == datetime(2026, 10, 19, 4, 0)
    # Same policy evaluated in UTC
    assert next_window_start(policy, datetime(2026, 10, 16, 12, 30), tz="UTC") == datetime(
        2026, 10, 16, 12, 30
    )


def test_next_window_start_raises_when_nothing_found():
    sundays = [date(2026, 10, 18) + timedelta(weeks=w) for w in range(12)]
    policy = CampaignPolicy(calling_days=[6], holidays=sundays)
    with pytest.raises(ValueError):
        next_window_start(policy, datetime(2026, 10, 13, 5, 30))
    # check_calling_window still refuses the call instead of crashing the dialer.
    decision = check_calling_window(policy, datetime(2026, 10, 13, 5, 30))
    assert decision == WindowDecision(False, "non_calling_day", None)


# --------------------------------------------------------------------------------------------
# DNC
# --------------------------------------------------------------------------------------------


def test_add_to_dnc_normalises_and_is_idempotent(session):
    entry = add_to_dnc(session, "98765 43210", reason="asked on call", source="call_opt_out")
    assert entry.phone == "+919876543210"
    again = add_to_dnc(session, "+91 98765-43210", reason="second time", source="manual")
    assert again.id == entry.id
    assert again.reason == "asked on call"  # original evidence kept
    assert session.scalar(select(func.count()).select_from(DNCEntry)) == 1
    assert is_dnc(session, "09876543210")
    assert is_dnc(session, "+919876543210")


def test_add_to_dnc_marks_distributors_and_audits(session, make_distributor):
    primary = make_distributor(phone="+919811111111", status=EmpanelmentStatus.LINK_SENT)
    alt = make_distributor(phone="+919822222222", alt_phone="+919811111111")
    other = make_distributor(phone="+919833333333")

    add_to_dnc(session, "9811111111", reason="opted out", source="call_opt_out")

    for d in (primary, alt):
        assert d.do_not_call
        assert d.status == EmpanelmentStatus.DO_NOT_CALL
        assert d.dnc_reason == "opted out"
    assert not other.do_not_call and other.status == EmpanelmentStatus.NEW

    events = session.scalars(select(AuditEvent).where(AuditEvent.kind == "dnc_added")).all()
    assert {e.distributor_id for e in events} == {primary.id, alt.id}
    assert all(e.detail["source"] == "call_opt_out" for e in events)
    assert all("9811111111" not in e.detail["phone"] for e in events)  # masked


def test_add_to_dnc_without_distributor_still_audits(session):
    add_to_dnc(session, "+919844444444", reason="manual", source="manual")
    events = session.scalars(select(AuditEvent).where(AuditEvent.kind == "dnc_added")).all()
    assert len(events) == 1 and events[0].distributor_id is None
    assert events[0].detail["new_entry"] is True


def test_add_to_dnc_flushes_but_does_not_commit(session):
    add_to_dnc(session, "+919855555555", reason="x", source="manual")
    assert session.scalar(select(DNCEntry.id)) is not None  # visible inside the transaction
    session.rollback()
    assert session.scalar(select(DNCEntry.id)) is None


def test_add_to_dnc_keeps_unnormalisable_number(session):
    entry = add_to_dnc(session, "  022-2345 6789 ", reason="landline", source="manual")
    assert entry.phone == "022-2345 6789"
    assert is_dnc(session, "022-2345 6789")


def test_add_to_dnc_rejects_empty_phone(session):
    with pytest.raises(ValueError):
        add_to_dnc(session, "   ", reason="x", source="manual")


def test_is_dnc_from_distributor_flag(session, make_distributor):
    make_distributor(phone="+919866666666", do_not_call=True)
    make_distributor(phone="+919877777777", alt_phone="+919888888888", status=EmpanelmentStatus.DO_NOT_CALL)
    assert is_dnc(session, "9866666666")
    assert is_dnc(session, "+919888888888")


def test_is_dnc_false_for_clean_numbers(session, make_distributor):
    make_distributor(phone="+919899999999")
    assert not is_dnc(session, "+919899999999")
    assert not is_dnc(session, "")
    assert not is_dnc(session, "not a number")


# --------------------------------------------------------------------------------------------
# Utterance screen
# --------------------------------------------------------------------------------------------

VIOLATIONS = [
    # guaranteed_returns
    ("This fund offers guaranteed returns.", "guaranteed_returns"),
    ("You will get assured returns from this scheme.", "guaranteed_returns"),
    ("It's a sure-shot return product.", "guaranteed_returns"),
    ("Fixed returns every year, sir.", "guaranteed_returns"),
    ("We guarantee your capital.", "guaranteed_returns"),
    ("Returns are guaranteed by the AMC.", "guaranteed_returns"),
    ("It's 100% guaranteed.", "guaranteed_returns"),
    ("Don't worry, returns are guaranteed.", "guaranteed_returns"),
    ("There is no lock-in but returns are guaranteed.", "guaranteed_returns"),
    ("Returns are guaranteed? No.", "guaranteed_returns"),
    ("isme return ki guarantee hai", "guaranteed_returns"),
    ("pakka return milega", "guaranteed_returns"),
    ("इसमें पक्का रिटर्न मिलेगा।", "guaranteed_returns"),
    ("रिटर्न की गारंटी है।", "guaranteed_returns"),
    # return_projection
    ("You can expect 12% returns.", "return_projection"),
    ("It can give returns of 15 percent a year.", "return_projection"),
    ("Your investment will grow 20% this year.", "return_projection"),
    ("This will double your money in five years.", "return_projection"),
    ("Expect twelve percent annual returns.", "return_projection"),
    ("A CAGR of 18% is likely.", "return_projection"),
    ("It will give you good returns.", "return_projection"),
    ("isme 15 percent return milega", "return_projection"),
    ("पैसा डबल हो जाएगा।", "return_projection"),
    ("इसमें 12 प्रतिशत रिटर्न मिलेगा।", "return_projection"),
    # past_performance
    ("Our flagship fund has delivered 18% returns.", "past_performance"),
    ("The manager has a track record of 20% annualised returns.", "past_performance"),
    ("Past returns were excellent.", "past_performance"),
    ("It has consistently beaten the market.", "past_performance"),
    ("The fund manager has a proven track record.", "past_performance"),
    ("पिछले साल 20% रिटर्न दिया।", "past_performance"),
    # risk_free
    ("It is a risk-free investment.", "risk_free"),
    ("There is no risk at all.", "risk_free"),
    ("No, there is no risk.", "risk_free"),
    ("This fund has zero risk.", "risk_free"),
    ("Your money is 100% safe.", "risk_free"),
    ("It's completely safe.", "risk_free"),
    ("The scheme offers capital protection.", "risk_free"),
    ("isme koi risk nahi hai", "risk_free"),
    ("इसमें कोई जोखिम नहीं है।", "risk_free"),
    # advice
    ("You should invest in this NFO.", "advice"),
    ("I recommend investing before the NFO closes.", "advice"),
    ("I strongly recommend this fund.", "advice"),
    ("This is the best fund in the market.", "advice"),
    ("It will outperform the Nifty.", "advice"),
    ("This fund can beat the market.", "advice"),
    ("You can't lose with this one.", "advice"),
    ("aapko invest karna chahiye", "advice"),
    ("यह सबसे अच्छा फंड है।", "advice"),
    # commission_figure
    ("You get a 1.5% trail on this scheme.", "commission_figure"),
    ("The commission of 2 percent is paid monthly.", "commission_figure"),
    ("Brokerage will be around 0.8 to 1 percent.", "commission_figure"),
    ("Payout is Rs 500 per application.", "commission_figure"),
    ("trail 75 bps hai", "commission_figure"),
    ("कमीशन 1% है", "commission_figure"),
    # inducement
    ("We will give you a gift voucher for every application.", "inducement"),
    ("Top partners win a foreign trip.", "inducement"),
    ("You'll get cashback on the first ten applications.", "inducement"),
    ("हम आपको बोनस देंगे।", "inducement"),
]


@pytest.mark.parametrize("text,rule", VIOLATIONS)
def test_screen_blocks(text, rule):
    result = screen_bot_utterance(text)
    assert not result.ok
    assert rule in result.violations
    assert set(result.violations) <= set(RULE_IDS)


ALLOWED = [
    # negated guarantee / risk claims (required)
    "Returns are not guaranteed.",
    "There is no guarantee of returns.",
    "No assured returns.",
    "रिटर्न की गारंटी नहीं",
    "रिटर्न की कोई गारंटी नहीं है।",
    "returns ki koi guarantee nahi hai",
    "I cannot guarantee returns.",
    "No one can guarantee returns in equity.",
    "There are no assured or guaranteed returns in this scheme.",
    "Returns in equity funds are not guaranteed.",
    "Nothing is 100% guaranteed in markets.",
    "The scheme is not risk-free.",
    "No investment is 100% safe.",
    "No investment has zero risk.",
    "There is no capital protection in this scheme.",
    # factual / neutral statements that look superficially similar
    "Since this is a new fund, it has no track record or past performance.",
    "Past performance may or may not be sustained in future.",
    "I'm not able to advise whether you should invest.",
    "I can't say which is the best fund for your clients.",
    "I can't say whether it will give good returns.",
    "We will ensure the best support for the NFO.",
    "The exit load is 1% if redeemed within 1 year.",
    "Growth and IDCW options are available, with a 1% exit load.",
    "SIP is available from Rs 500 per month.",
    "The minimum investment is Rs 5,000.",
    "We do not offer any gifts or incentives.",
    "As per SEBI rules, there is no upfront commission.",
    "Brokerage payouts are made within 30 days.",
    "Brokerage is paid on the 10th of every month.",
    "The fund manager has 20 years of experience.",
    "The NFO opens on 20 October and closes on 3 November.",
    "The NFO is expected to open on 20 October.",
    "Please rest assured your documents are handled securely.",
    "There's no fee, so you won't lose anything by registering.",
    "Investing in equity carries risk, and you could lose money.",
    "Shall I schedule a call back at 4 pm tomorrow?",
    "न्यूनतम निवेश 5,000 रुपये है।",
    "रिटर्न बाज़ार पर निर्भर करते हैं और इनकी कोई गारंटी नहीं है।",
    "मुझे खेद है, मैं निवेश सलाह नहीं दे सकती।",
    "Returns guaranteed nahi hain, yeh market linked scheme hai.",
    "",
]


@pytest.mark.parametrize("text", ALLOWED)
def test_screen_allows(text):
    assert screen_bot_utterance(text) == ScreenResult(ok=True, violations=[])


def test_screen_reports_multiple_rules_in_rule_order():
    result = screen_bot_utterance("We guarantee 12% returns and it is completely safe, plus a 1% trail.")
    assert result.violations == ["guaranteed_returns", "return_projection", "risk_free", "commission_figure"]


def test_screen_is_case_insensitive_and_handles_nukta_and_curly_quotes():
    assert "guaranteed_returns" in screen_bot_utterance("GUARANTEED RETURNS").violations
    assert "advice" in screen_bot_utterance("You can\u2019t lose.").violations
    # "फ़ायदा" (with nukta) and "फायदा" (without) are the same word.
    assert not screen_bot_utterance("अच्छा फ़ायदा मिलेगा").ok
    assert not screen_bot_utterance("अच्छा फायदा मिलेगा").ok


def _approved_texts(kb) -> list[str]:
    nfo, amc = kb.nfo, kb.amc
    texts = [nfo.mandatory_disclaimer, nfo.disclaimer("hi-IN"), nfo.commission_response, *nfo.key_highlights]
    texts += [faq.answer for faq in kb.faqs]
    texts += [
        lang.greeting.format(bot_name=amc.bot_name, amc_name=amc.name, name="Ravi Kumar")
        for lang in amc.languages
        if lang.greeting
    ]
    # Other approved talking points the bot reads out verbatim.
    texts += [nfo.scheme_type, nfo.investment_objective, nfo.exit_load, nfo.min_investment]
    texts += [nfo.sip_details or "", nfo.allotment_or_reopen_note or "", *nfo.plans_and_options]
    texts += [*nfo.distributor_support, *amc.distributor_value_props, *amc.empanelment_steps]
    texts += [*amc.empanelment_documents]
    return texts


def test_all_approved_config_text_passes_the_screen(kb):
    texts = _approved_texts(kb)
    assert len(kb.faqs) >= 5 and len(texts) > 20
    blocked = {t: screen_bot_utterance(t).violations for t in texts if not screen_bot_utterance(t).ok}
    assert blocked == {}


@pytest.mark.parametrize("language", ["en-IN", "hi-IN"])
def test_safe_reply_passes_the_screen(language):
    assert screen_bot_utterance(safe_reply(language)).ok


def test_safe_reply_languages():
    english = (
        "I'm sorry, I can only share information from the official scheme documents. "
        "Our relationship manager can help you with that. Shall I arrange a call back?"
    )
    assert safe_reply("en-IN") == english
    assert safe_reply("ta-IN") == english  # unknown -> English
    assert safe_reply("") == english
    hindi = safe_reply("hi-IN")
    assert hindi != english and any("\u0900" <= ch <= "\u097f" for ch in hindi)
    assert safe_reply("hi") == hindi


# --------------------------------------------------------------------------------------------
# Opt-out detection
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "don't call me again",
        "Do not call me.",
        "stop calling me",
        "please remove my number",
        "take me off your list",
        "unsubscribe",
        "never call me",
        "put me on DND",
        "no more calls",
        "mujhe dobara call mat karna",
        "call mat karo",
        "phone mat karna",
        "mera number hata do",
        "कॉल मत करो",
        "दोबारा फ़ोन मत करना",
        "दोबारा फोन मत करना",
        "मेरा नंबर हटा दो",
        "I'm not interested, please don't call me.",
        "Don\u2019t call me again, I'm busy.",
        "Do not call me from now on",
        "remove me from your list",
        "delete my data",
        "I don't want any calls",
        "kabhi call mat karna",
        "मुझे दोबारा कॉल मत करना",
    ],
)
def test_detect_opt_out_positive(text):
    assert detect_opt_out(text)


@pytest.mark.parametrize(
    "text",
    [
        "don't call me now, call me tomorrow",
        "can you call me later",
        "I'm busy, call after 5",
        "don't worry",
        "call me on WhatsApp",
        "I will call you back",
        "abhi call mat karo, kal karna",
        "अभी कॉल मत करो, शाम को करना",
        "please remove my old number and use this one",
        "I am not interested",
        "Yes, please call me",
        "",
    ],
)
def test_detect_opt_out_negative(text):
    assert not detect_opt_out(text)

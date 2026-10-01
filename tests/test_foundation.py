"""Tests for foundation modules: phone, funnel, knowledge, models."""

from datetime import date

import pytest

from callingbot.funnel import advance_status, outcome_to_status
from callingbot.knowledge import CampaignPolicy
from callingbot.models import Call, CallOutcome, CallStatus, EmpanelmentStatus
from callingbot.phone import mask_phone, normalize_indian_mobile


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("98765 43210", "+919876543210"),
        ("098765-43210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("0091 9876543210", "+919876543210"),
        ("5876543210", None),  # Indian mobiles start 6-9
        ("12345", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_indian_mobile(raw, expected):
    assert normalize_indian_mobile(raw) == expected


def test_mask_phone():
    assert mask_phone("+919876543210") == "+91******3210"


def test_knowledge_loads(kb):
    assert kb.amc.default_language == "en-IN"
    assert "{arn}" in kb.amc.empanelment_url_template
    assert kb.nfo.nfo_open_date <= kb.nfo.nfo_close_date
    assert kb.faqs and all("#" not in f.answer for f in kb.faqs)
    assert kb.amc.language("xx-XX").code == "en-IN"


def test_campaign_policy_validation():
    with pytest.raises(ValueError):
        CampaignPolicy(window_start="19:00", window_end="10:00")
    with pytest.raises(ValueError):
        CampaignPolicy(calling_days=[7])


def test_advance_status_only_moves_up(make_distributor):
    d = make_distributor()
    assert advance_status(d, EmpanelmentStatus.LINK_SENT)
    assert not advance_status(d, EmpanelmentStatus.CONTACTED)
    assert d.status == EmpanelmentStatus.LINK_SENT
    assert advance_status(d, EmpanelmentStatus.DO_NOT_CALL)
    assert d.do_not_call
    assert not advance_status(d, EmpanelmentStatus.EMPANELLED)
    assert d.status == EmpanelmentStatus.DO_NOT_CALL


def test_outcome_mapping():
    assert outcome_to_status(CallOutcome.OPTED_OUT) == EmpanelmentStatus.DO_NOT_CALL
    assert outcome_to_status(CallOutcome.NO_OUTCOME) is None
    assert outcome_to_status(None) is None


def test_call_defaults_persist(session, make_distributor):
    d = make_distributor(arn_valid_till=date(2027, 3, 31))
    call = Call(distributor_id=d.id, provider="simulator")
    session.add(call)
    session.commit()
    session.refresh(call)
    assert call.status == CallStatus.QUEUED
    assert call.llm_messages == [] and call.engine_state == {}
    assert call.language == "en-IN"
    assert d.calls == [call]

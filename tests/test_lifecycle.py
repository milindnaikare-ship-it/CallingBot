"""Tests for callingbot.services.lifecycle: call creation, status callbacks, finalisation and retries."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from conftest import IN_WINDOW_UTC
from sqlalchemy import func, select

from callingbot.compliance import add_to_dnc
from callingbot.models import (
    AuditEvent,
    Callback,
    CallOutcome,
    CallStatus,
    Campaign,
    CampaignContact,
    CampaignStatus,
    ContactState,
    EmpanelmentStatus,
)
from callingbot.services.lifecycle import apply_status_update, create_call, finalize_call
from callingbot.telephony.base import CallStatusUpdate

# IST = UTC+05:30. Tuesday 2026-10-13.
TUE_1830_IST = datetime(2026, 10, 13, 13, 0)
WED_1000_IST = datetime(2026, 10, 14, 4, 30)


@pytest.fixture
def dial(session, make_distributor):
    """Factory: a campaign contact that has just been dialled (attempts already counted)."""

    def _dial(*, attempts: int = 1, distributor=None, language: str | None = None):
        d = distributor or make_distributor()
        campaign = session.scalar(select(Campaign).where(Campaign.name == "Lifecycle"))
        if campaign is None:
            campaign = Campaign(name="Lifecycle", status=CampaignStatus.ACTIVE)
            session.add(campaign)
            session.flush()
        contact = CampaignContact(
            campaign_id=campaign.id, distributor_id=d.id, state=ContactState.IN_PROGRESS, attempts=attempts
        )
        session.add(contact)
        session.flush()
        call = create_call(
            session, distributor=d, provider="simulator", campaign=campaign, contact=contact, language=language
        )
        return d, contact, call

    return _dial


def _status(status: CallStatus, **kw) -> CallStatusUpdate:
    return CallStatusUpdate(provider_call_id=kw.pop("provider_call_id", "SIM-1"), status=status, **kw)


def _audits(session, kind: str) -> list[AuditEvent]:
    return list(session.scalars(select(AuditEvent).where(AuditEvent.kind == kind)))


def _connect(session, call, kb, *, turns: int, at: datetime = IN_WINDOW_UTC) -> None:
    apply_status_update(session, call, _status(CallStatus.IN_PROGRESS, answered_by="human"), kb=kb, now_utc=at)
    call.turn_count = turns


# --------------------------------------------------------------------------------------------
# create_call
# --------------------------------------------------------------------------------------------


def test_create_call_defaults(session, make_distributor):
    d = make_distributor(preferred_language="hi-IN")
    call = create_call(session, distributor=d, provider="twilio")
    assert call.id is not None
    assert call.status == CallStatus.QUEUED
    assert call.language == "hi-IN"
    assert call.distributor_id == d.id and call.campaign_id is None and call.contact_id is None
    assert call.engine_state == {} and call.llm_messages == []


def test_create_call_language_precedence(session, make_distributor):
    assert create_call(session, distributor=make_distributor(), provider="simulator").language == "en-IN"
    d = make_distributor(preferred_language="hi-IN")
    assert create_call(session, distributor=d, provider="simulator", language="en-IN").language == "en-IN"


def test_create_call_links_campaign_and_contact(dial):
    d, contact, call = dial()
    assert call.contact_id == contact.id
    assert call.campaign_id == contact.campaign_id


# --------------------------------------------------------------------------------------------
# apply_status_update
# --------------------------------------------------------------------------------------------


def test_status_moves_forward_only(session, kb, dial):
    _, _, call = dial()
    apply_status_update(session, call, _status(CallStatus.RINGING), kb=kb, now_utc=IN_WINDOW_UTC)
    assert call.status == CallStatus.RINGING
    assert call.provider_call_id == "SIM-1"
    apply_status_update(session, call, _status(CallStatus.INITIATED), kb=kb, now_utc=IN_WINDOW_UTC)
    assert call.status == CallStatus.RINGING  # late "initiated" does not move it back

    answered = IN_WINDOW_UTC + timedelta(seconds=5)
    apply_status_update(
        session, call, _status(CallStatus.IN_PROGRESS, answered_by="human"), kb=kb, now_utc=answered
    )
    assert call.status == CallStatus.IN_PROGRESS
    assert call.answered_at == answered and call.answered_by == "human"
    apply_status_update(
        session, call, _status(CallStatus.IN_PROGRESS), kb=kb, now_utc=answered + timedelta(seconds=30)
    )
    assert call.answered_at == answered  # first IN_PROGRESS wins
    apply_status_update(session, call, _status(CallStatus.RINGING), kb=kb, now_utc=answered)
    assert call.status == CallStatus.IN_PROGRESS


def test_provider_call_id_is_not_overwritten(session, kb, dial):
    _, _, call = dial()
    call.provider_call_id = "CA-original"
    apply_status_update(
        session, call, _status(CallStatus.RINGING, provider_call_id="CA-other"), kb=kb, now_utc=IN_WINDOW_UTC
    )
    assert call.provider_call_id == "CA-original"


def test_completed_records_end_and_duration_from_update(session, kb, dial):
    d, contact, call = dial()
    _connect(session, call, kb, turns=3)
    call.outcome = CallOutcome.INTERESTED
    end = IN_WINDOW_UTC + timedelta(minutes=2)
    apply_status_update(
        session,
        call,
        _status(CallStatus.COMPLETED, duration_seconds=118, recording_url="https://rec/1"),
        kb=kb,
        now_utc=end,
    )
    assert call.status == CallStatus.COMPLETED
    assert call.ended_at == end
    assert call.duration_seconds == 118
    assert call.recording_url == "https://rec/1"
    assert call.engine_state["finalized"] is True
    assert contact.state == ContactState.DONE
    assert d.status == EmpanelmentStatus.INTERESTED


def test_duration_computed_from_answered_at(session, kb, dial):
    _, _, call = dial()
    _connect(session, call, kb, turns=1)
    apply_status_update(
        session, call, _status(CallStatus.COMPLETED), kb=kb, now_utc=IN_WINDOW_UTC + timedelta(seconds=75)
    )
    assert call.duration_seconds == 75


def test_unanswered_call_has_no_computed_duration(session, kb, dial):
    _, _, call = dial()
    apply_status_update(session, call, _status(CallStatus.NO_ANSWER), kb=kb, now_utc=IN_WINDOW_UTC)
    assert call.duration_seconds is None


def test_terminal_status_is_sticky_and_idempotent(session, kb, dial):
    _, contact, call = dial()
    apply_status_update(session, call, _status(CallStatus.NO_ANSWER), kb=kb, now_utc=IN_WINDOW_UTC)
    first_next = contact.next_attempt_at
    later = IN_WINDOW_UTC + timedelta(minutes=1)
    for status in (CallStatus.IN_PROGRESS, CallStatus.RINGING, CallStatus.COMPLETED, CallStatus.NO_ANSWER):
        apply_status_update(session, call, _status(status), kb=kb, now_utc=later)
    assert call.status == CallStatus.NO_ANSWER
    assert call.ended_at == IN_WINDOW_UTC
    assert call.answered_at is None
    assert contact.next_attempt_at == first_next
    assert len(_audits(session, "call_finalized")) == 1


def test_late_terminal_callback_fills_missing_duration_and_recording(session, kb, dial):
    _, _, call = dial()
    apply_status_update(session, call, _status(CallStatus.BUSY), kb=kb, now_utc=IN_WINDOW_UTC)
    apply_status_update(
        session,
        call,
        _status(CallStatus.COMPLETED, duration_seconds=0, recording_url="https://rec/2"),
        kb=kb,
        now_utc=IN_WINDOW_UTC,
    )
    assert call.status == CallStatus.BUSY
    assert call.duration_seconds == 0
    assert call.recording_url == "https://rec/2"
    apply_status_update(
        session, call, _status(CallStatus.BUSY, duration_seconds=99, recording_url="x"), kb=kb, now_utc=IN_WINDOW_UTC
    )
    assert call.duration_seconds == 0 and call.recording_url == "https://rec/2"


def test_connected_call_reported_as_voicemail_stays_completed(session, kb, dial):
    d, _, call = dial()
    _connect(session, call, kb, turns=2)
    apply_status_update(session, call, _status(CallStatus.VOICEMAIL), kb=kb, now_utc=IN_WINDOW_UTC)
    assert call.status == CallStatus.COMPLETED
    assert call.outcome == CallOutcome.NO_OUTCOME
    assert d.status == EmpanelmentStatus.CONTACTED


def test_terminal_without_finalize_is_finalized_by_next_terminal_update(session, kb, dial):
    _, contact, call = dial()
    call.status = CallStatus.COMPLETED  # e.g. set by another component that crashed before finalising
    apply_status_update(session, call, _status(CallStatus.COMPLETED), kb=kb, now_utc=IN_WINDOW_UTC)
    assert call.engine_state.get("finalized") is True
    assert contact.state == ContactState.PENDING


# --------------------------------------------------------------------------------------------
# finalize_call: retries and contact state
# --------------------------------------------------------------------------------------------


def test_unanswered_retry_inside_same_window(session, kb, dial):
    d, contact, call = dial(attempts=1)
    apply_status_update(session, call, _status(CallStatus.NO_ANSWER), kb=kb, now_utc=IN_WINDOW_UTC)
    assert call.outcome == CallOutcome.NO_OUTCOME
    assert contact.state == ContactState.PENDING
    assert contact.next_attempt_at == IN_WINDOW_UTC + timedelta(minutes=180)  # 14:00 IST same day
    assert contact.final_outcome is None
    assert d.status == EmpanelmentStatus.NEW


def test_failed_evening_attempt_moves_to_next_day_window_start(session, kb, dial):
    _, contact, call = dial(attempts=1)
    apply_status_update(session, call, _status(CallStatus.FAILED), kb=kb, now_utc=TUE_1830_IST)
    # 18:30 + 180 min = 21:30 IST, after the 19:00 close -> Wednesday 10:00 IST.
    assert contact.state == ContactState.PENDING
    assert contact.next_attempt_at == WED_1000_IST


def test_second_attempt_uses_second_backoff(session, kb, dial):
    _, contact, call = dial(attempts=2)
    apply_status_update(session, call, _status(CallStatus.BUSY), kb=kb, now_utc=IN_WINDOW_UTC)
    assert contact.next_attempt_at == IN_WINDOW_UTC + timedelta(minutes=1440)  # Wednesday 11:00 IST


def test_retry_skips_sunday_and_holiday(session, kb, dial):
    # Saturday 2026-10-17 18:00 IST + 3h -> Sunday is not a calling day -> Monday 10:00 IST.
    _, contact, call = dial(attempts=1)
    apply_status_update(session, call, _status(CallStatus.NO_ANSWER), kb=kb, now_utc=datetime(2026, 10, 17, 12, 30))
    assert contact.next_attempt_at == datetime(2026, 10, 19, 4, 30)
    # Thursday 2026-10-01 18:00 IST + 3h -> Friday 2 Oct is Gandhi Jayanti -> Saturday 10:00 IST.
    _, contact2, call2 = dial(attempts=1)
    apply_status_update(
        session, call2, _status(CallStatus.NO_ANSWER, provider_call_id="SIM-2"), kb=kb, now_utc=datetime(2026, 10, 1, 12, 30)
    )
    assert contact2.next_attempt_at == datetime(2026, 10, 3, 4, 30)


def test_max_attempts_closes_contact(session, kb, dial):
    _, contact, call = dial(attempts=kb.campaign.max_attempts)
    apply_status_update(session, call, _status(CallStatus.NO_ANSWER), kb=kb, now_utc=IN_WINDOW_UTC)
    assert contact.state == ContactState.DONE
    assert contact.final_outcome == CallOutcome.NO_OUTCOME
    assert contact.next_attempt_at is None


def test_final_outcome_closes_contact_and_advances_funnel(session, kb, dial):
    d, contact, call = dial()
    _connect(session, call, kb, turns=4)
    call.outcome = CallOutcome.LINK_SENT
    apply_status_update(session, call, _status(CallStatus.COMPLETED), kb=kb, now_utc=IN_WINDOW_UTC)
    assert contact.state == ContactState.DONE
    assert contact.final_outcome == CallOutcome.LINK_SENT
    assert d.status == EmpanelmentStatus.LINK_SENT


def test_opt_out_outcome_closes_contact_and_marks_dnc(session, kb, dial):
    d, contact, call = dial()
    _connect(session, call, kb, turns=1)
    call.outcome = CallOutcome.OPTED_OUT
    apply_status_update(session, call, _status(CallStatus.COMPLETED), kb=kb, now_utc=IN_WINDOW_UTC)
    assert contact.state == ContactState.DONE
    assert d.status == EmpanelmentStatus.DO_NOT_CALL and d.do_not_call


def test_connected_no_outcome_is_done_not_retried(session, kb, dial):
    d, contact, call = dial(attempts=1)
    _connect(session, call, kb, turns=2)
    apply_status_update(session, call, _status(CallStatus.COMPLETED), kb=kb, now_utc=IN_WINDOW_UTC)
    assert call.outcome == CallOutcome.NO_OUTCOME
    assert contact.state == ContactState.DONE
    assert contact.final_outcome == CallOutcome.NO_OUTCOME
    assert d.status == EmpanelmentStatus.CONTACTED


def test_single_turn_hangup_is_retried_but_counts_as_contacted(session, kb, dial):
    d, contact, call = dial(attempts=1)
    _connect(session, call, kb, turns=1)
    apply_status_update(session, call, _status(CallStatus.COMPLETED), kb=kb, now_utc=IN_WINDOW_UTC)
    assert contact.state == ContactState.PENDING
    assert contact.next_attempt_at is not None
    assert d.status == EmpanelmentStatus.CONTACTED


def test_voicemail_is_retried_and_not_contacted(session, kb, dial):
    d, contact, call = dial(attempts=1)
    apply_status_update(
        session, call, _status(CallStatus.IN_PROGRESS, answered_by="machine"), kb=kb, now_utc=IN_WINDOW_UTC
    )
    apply_status_update(session, call, _status(CallStatus.VOICEMAIL), kb=kb, now_utc=IN_WINDOW_UTC)
    assert call.status == CallStatus.VOICEMAIL
    assert call.outcome == CallOutcome.VOICEMAIL
    assert contact.state == ContactState.PENDING
    assert contact.next_attempt_at == IN_WINDOW_UTC + timedelta(minutes=180)
    assert d.status == EmpanelmentStatus.NEW


def test_distributor_opted_out_mid_call_without_outcome_is_done(session, kb, dial):
    d, contact, call = dial(attempts=1)
    add_to_dnc(session, d.phone, reason="asked", source="call_opt_out")
    apply_status_update(session, call, _status(CallStatus.NO_ANSWER), kb=kb, now_utc=IN_WINDOW_UTC)
    assert contact.state == ContactState.DONE
    assert contact.next_attempt_at is None


def test_bot_callback_requeues_contact_at_callback_time(session, kb, dial):
    d, contact, call = dial(attempts=1)
    _connect(session, call, kb, turns=3)
    when = datetime(2026, 10, 15, 9, 0)  # Thursday 14:30 IST
    session.add(Callback(distributor_id=d.id, call_id=call.id, scheduled_for=when, with_rm=False))
    call.outcome = CallOutcome.CALLBACK_REQUESTED
    apply_status_update(session, call, _status(CallStatus.COMPLETED), kb=kb, now_utc=IN_WINDOW_UTC)
    assert contact.state == ContactState.PENDING
    assert contact.next_attempt_at == when
    assert contact.final_outcome is None
    assert d.status == EmpanelmentStatus.CALLBACK_SCHEDULED


def test_bot_callback_ignored_when_attempts_exhausted(session, kb, dial):
    d, contact, call = dial(attempts=kb.campaign.max_attempts)
    _connect(session, call, kb, turns=3)
    session.add(Callback(distributor_id=d.id, call_id=call.id, scheduled_for=WED_1000_IST, with_rm=False))
    call.outcome = CallOutcome.CALLBACK_REQUESTED
    apply_status_update(session, call, _status(CallStatus.COMPLETED), kb=kb, now_utc=IN_WINDOW_UTC)
    assert contact.state == ContactState.DONE
    assert contact.final_outcome == CallOutcome.CALLBACK_REQUESTED


def test_rm_callback_closes_contact(session, kb, dial):
    d, contact, call = dial(attempts=1)
    _connect(session, call, kb, turns=3)
    session.add(Callback(distributor_id=d.id, call_id=call.id, scheduled_for=WED_1000_IST, with_rm=True))
    call.outcome = CallOutcome.CALLBACK_REQUESTED
    apply_status_update(session, call, _status(CallStatus.COMPLETED), kb=kb, now_utc=IN_WINDOW_UTC)
    assert contact.state == ContactState.DONE
    assert contact.final_outcome == CallOutcome.CALLBACK_REQUESTED


def test_bot_callback_ignored_after_opt_out(session, kb, dial):
    d, contact, call = dial(attempts=1)
    _connect(session, call, kb, turns=3)
    session.add(Callback(distributor_id=d.id, call_id=call.id, scheduled_for=WED_1000_IST, with_rm=False))
    call.outcome = CallOutcome.OPTED_OUT
    apply_status_update(session, call, _status(CallStatus.COMPLETED), kb=kb, now_utc=IN_WINDOW_UTC)
    assert contact.state == ContactState.DONE


# --------------------------------------------------------------------------------------------
# finalize_call: direct use
# --------------------------------------------------------------------------------------------


def test_finalize_is_idempotent_and_audited_once(session, kb, dial):
    _, contact, call = dial(attempts=1)
    call.status = CallStatus.NO_ANSWER
    finalize_call(session, call, kb=kb, now_utc=IN_WINDOW_UTC)
    first = (contact.state, contact.next_attempt_at)
    finalize_call(session, call, kb=kb, now_utc=IN_WINDOW_UTC + timedelta(hours=5))
    assert (contact.state, contact.next_attempt_at) == first
    events = _audits(session, "call_finalized")
    assert len(events) == 1
    assert events[0].call_id == call.id
    assert events[0].detail["outcome"] == "no_outcome"
    assert events[0].detail["status"] == "no_answer"
    assert events[0].detail["contact_state"] == "pending"


def test_finalize_closes_non_terminal_call_defensively(session, kb, dial):
    _, _, call = dial()
    call.status = CallStatus.IN_PROGRESS
    finalize_call(session, call, kb=kb, now_utc=IN_WINDOW_UTC)
    assert call.status == CallStatus.COMPLETED
    assert call.ended_at == IN_WINDOW_UTC


def test_finalize_without_contact(session, kb, make_distributor):
    d = make_distributor()
    call = create_call(session, distributor=d, provider="simulator")
    call.status = CallStatus.COMPLETED
    call.turn_count = 2
    call.outcome = CallOutcome.NOT_INTERESTED
    finalize_call(session, call, kb=kb, now_utc=IN_WINDOW_UTC)
    assert d.status == EmpanelmentStatus.NOT_INTERESTED
    assert session.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.kind == "call_finalized")) == 1


def test_finalize_never_downgrades_funnel(session, kb, dial, make_distributor):
    d = make_distributor(status=EmpanelmentStatus.LINK_SENT)
    _, contact, call = dial(distributor=d)
    _connect(session, call, kb, turns=3)
    call.outcome = CallOutcome.NOT_INTERESTED
    apply_status_update(session, call, _status(CallStatus.COMPLETED), kb=kb, now_utc=IN_WINDOW_UTC)
    assert d.status == EmpanelmentStatus.LINK_SENT
    assert contact.state == ContactState.DONE

"""Tests for callingbot.services.dialer: campaign membership, due selection, dialling and reaping."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from conftest import IN_WINDOW_UTC
from sqlalchemy import select

from callingbot.models import (
    AuditEvent,
    Call,
    Callback,
    CallbackStatus,
    CallOutcome,
    CallStatus,
    Campaign,
    CampaignContact,
    CampaignStatus,
    ContactState,
    DNCEntry,
    EmpanelmentStatus,
)
from callingbot.services.dialer import (
    DialReport,
    add_distributors_to_campaign,
    count_active_calls,
    dial_due_contacts,
    reap_stale_calls,
    select_due_contacts,
)
from callingbot.telephony import SimulatorProvider, TelephonyError
from callingbot.telephony.base import PlaceCallResult


class FailingProvider(SimulatorProvider):
    name = "failing"

    def place_call(self, *, to_number: str, call_id: int) -> PlaceCallResult:
        raise TelephonyError("carrier rejected the call (simulated)")


class BuggyProvider(SimulatorProvider):
    name = "buggy"

    def place_call(self, *, to_number: str, call_id: int) -> PlaceCallResult:
        raise KeyError("sid")


@pytest.fixture
def campaign(session) -> Campaign:
    c = Campaign(name="NFO Launch", status=CampaignStatus.ACTIVE)
    session.add(c)
    session.flush()
    return c


@pytest.fixture
def provider() -> SimulatorProvider:
    return SimulatorProvider()


def _policy(kb, **changes):
    return kb.model_copy(update={"campaign": kb.campaign.model_copy(update=changes)})


def _member(session, campaign, distributor, **kw) -> CampaignContact:
    contact = CampaignContact(campaign_id=campaign.id, distributor_id=distributor.id, **kw)
    session.add(contact)
    session.flush()
    return contact


def _dial(session, campaign, provider, kb, settings, now=IN_WINDOW_UTC, **kw) -> DialReport:
    return dial_due_contacts(
        session, campaign=campaign, provider=provider, kb=kb, settings=settings, now_utc=now, **kw
    )


def _skip_reasons(session) -> list[str]:
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.kind == "dial_skipped").order_by(AuditEvent.id)
    )
    return [e.detail["reason"] for e in events]


# --------------------------------------------------------------------------------------------
# add_distributors_to_campaign / select_due_contacts
# --------------------------------------------------------------------------------------------


def test_add_all_dialable_distributors(session, campaign, make_distributor):
    ok = [make_distributor(), make_distributor(status=EmpanelmentStatus.INTERESTED)]
    make_distributor(do_not_call=True)
    make_distributor(status=EmpanelmentStatus.EMPANELLED)
    make_distributor(status=EmpanelmentStatus.ALREADY_EMPANELLED)
    make_distributor(status=EmpanelmentStatus.WRONG_NUMBER)
    on_dnc_list = make_distributor()
    session.add(DNCEntry(phone=on_dnc_list.phone, reason="manual", source="manual"))
    session.flush()

    assert add_distributors_to_campaign(session, campaign) == 2
    contacts = session.scalars(
        select(CampaignContact).where(CampaignContact.campaign_id == campaign.id)
    ).all()
    assert {c.distributor_id for c in contacts} == {d.id for d in ok}
    assert all(c.state == ContactState.PENDING and c.next_attempt_at is None for c in contacts)
    assert add_distributors_to_campaign(session, campaign) == 0  # existing members are not duplicated


def test_add_given_ids_still_filters_ineligible(session, campaign, make_distributor):
    a, b = make_distributor(), make_distributor()
    opted_out = make_distributor(do_not_call=True)
    make_distributor()  # not requested
    assert add_distributors_to_campaign(session, campaign, [a.id, opted_out.id, a.id]) == 1
    assert add_distributors_to_campaign(session, campaign, [a.id, b.id]) == 1
    assert add_distributors_to_campaign(session, campaign, []) == 0


def test_select_due_contacts_order_and_filters(session, campaign, make_distributor):
    now = IN_WINDOW_UTC
    later = _member(session, campaign, make_distributor(), next_attempt_at=now - timedelta(minutes=1))
    earlier = _member(session, campaign, make_distributor(), next_attempt_at=now - timedelta(hours=2))
    asap = _member(session, campaign, make_distributor(), next_attempt_at=None)
    exactly_now = _member(session, campaign, make_distributor(), next_attempt_at=now)
    _member(session, campaign, make_distributor(), next_attempt_at=now + timedelta(minutes=1))
    _member(session, campaign, make_distributor(), state=ContactState.DONE)
    _member(session, campaign, make_distributor(), state=ContactState.IN_PROGRESS)

    due = select_due_contacts(session, campaign, now_utc=now, limit=10)
    assert [c.id for c in due] == [asap.id, earlier.id, later.id, exactly_now.id]
    assert [c.id for c in select_due_contacts(session, campaign, now_utc=now, limit=2)] == [
        asap.id,
        earlier.id,
    ]
    assert select_due_contacts(session, campaign, now_utc=now, limit=0) == []


# --------------------------------------------------------------------------------------------
# dial_due_contacts: gates
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [CampaignStatus.DRAFT, CampaignStatus.PAUSED, CampaignStatus.COMPLETED])
def test_inactive_campaign_places_nothing(
    session, campaign, provider, kb, settings, make_distributor, status
):
    _member(session, campaign, make_distributor())
    campaign.status = status
    report = _dial(session, campaign, provider, kb, settings)
    assert report.placed == 0 and report.window is None
    assert "not active" in report.messages[0]
    assert provider.placed == []


def test_outside_calling_window_places_nothing(session, campaign, provider, kb, settings, make_distributor):
    _member(session, campaign, make_distributor())
    evening = datetime(2026, 10, 13, 14, 0)  # Tuesday 19:30 IST
    report = _dial(session, campaign, provider, kb, settings, now=evening)
    assert report.placed == 0
    assert report.window is not None and not report.window.allowed
    assert report.window.reason == "after_window"
    assert report.window.next_allowed_utc == datetime(2026, 10, 14, 4, 30)
    assert "Outside the calling window" in report.messages[0]
    assert provider.placed == []


def test_holiday_places_nothing(session, campaign, provider, kb, settings, make_distributor):
    _member(session, campaign, make_distributor())
    report = _dial(
        session, campaign, provider, kb, settings, now=datetime(2026, 10, 2, 5, 30)
    )  # Gandhi Jayanti
    assert report.window.reason == "holiday" and report.placed == 0


def test_capacity_limited_by_max_concurrent_calls(
    session, campaign, provider, kb, settings, make_distributor
):
    for _ in range(5):
        _member(session, campaign, make_distributor())
    report = _dial(session, campaign, provider, kb, settings)  # max_concurrent_calls = 3
    assert report.placed == 3
    assert report.window is not None and report.window.allowed
    # Those three calls are still active, so a second round has no capacity.
    report2 = _dial(session, campaign, provider, kb, settings, now=IN_WINDOW_UTC + timedelta(seconds=30))
    assert report2.placed == 0
    assert "No capacity" in report2.messages[0]


def test_capacity_limited_by_calls_per_minute_and_max_new_calls(
    session, campaign, provider, kb, settings, make_distributor
):
    for _ in range(6):
        _member(session, campaign, make_distributor())
    fast = _policy(kb, calls_per_minute=2, max_concurrent_calls=10)
    assert _dial(session, campaign, provider, fast, settings).placed == 2
    # calls_per_minute is a rolling minute: a round 30 s later (the default loop interval) waits.
    half_minute = _dial(
        session, campaign, provider, fast, settings, now=IN_WINDOW_UTC + timedelta(seconds=30)
    )
    assert half_minute.placed == 0
    assert "2 placed in the last minute" in half_minute.messages[0]
    later = IN_WINDOW_UTC + timedelta(seconds=61)
    assert _dial(session, campaign, provider, fast, settings, now=later, max_new_calls=1).placed == 1
    assert _dial(session, campaign, provider, fast, settings, now=later).placed == 1  # 2 per minute in total


def test_new_calls_are_stamped_with_the_injected_clock(
    session, campaign, provider, kb, settings, make_distributor
):
    contact = _member(session, campaign, make_distributor())
    _dial(session, campaign, provider, kb, settings)
    assert session.scalar(select(Call.created_at).where(Call.contact_id == contact.id)) == IN_WINDOW_UTC


def test_active_calls_from_other_campaigns_count(session, campaign, provider, kb, settings, make_distributor):
    other = make_distributor()
    for status in (CallStatus.RINGING, CallStatus.IN_PROGRESS):
        session.add(Call(distributor_id=other.id, provider="twilio", status=status, created_at=IN_WINDOW_UTC))
    # A QUEUED call that never got placed does not hold a line.
    session.add(
        Call(
            distributor_id=other.id,
            provider="twilio",
            status=CallStatus.QUEUED,
            created_at=IN_WINDOW_UTC - timedelta(minutes=10),
        )
    )
    session.add(Call(distributor_id=other.id, provider="twilio", status=CallStatus.COMPLETED))
    session.flush()
    assert count_active_calls(session, now_utc=IN_WINDOW_UTC) == 2
    for _ in range(3):
        _member(session, campaign, make_distributor())
    assert _dial(session, campaign, provider, kb, settings).placed == 1  # 3 concurrent - 2 active


# --------------------------------------------------------------------------------------------
# dial_due_contacts: eligibility re-check
# --------------------------------------------------------------------------------------------


def test_ineligible_contacts_are_skipped_with_audit(
    session, campaign, provider, kb, settings, make_distributor
):
    on_list = make_distributor()
    session.add(DNCEntry(phone=on_list.phone, reason="ncpr", source="ncpr_scrub"))
    flagged = make_distributor(do_not_call=True)
    expired = make_distributor(arn_valid_till=date(2026, 10, 12))  # day before IN_WINDOW_UTC (IST)
    bad_phone = make_distributor(phone="+911234")
    empanelled = make_distributor(status=EmpanelmentStatus.EMPANELLED)
    contacts = [_member(session, campaign, d) for d in (on_list, flagged, expired, bad_phone, empanelled)]

    report = _dial(session, campaign, provider, kb, settings)
    assert (report.placed, report.skipped, report.failed) == (0, 5, 0)
    assert provider.placed == []
    assert all(c.state == ContactState.SKIPPED for c in contacts)
    assert all(c.attempts == 0 for c in contacts)
    assert _skip_reasons(session) == ["dnc", "dnc", "arn_expired", "invalid_phone", "status_empanelled"]
    event = session.scalar(select(AuditEvent).where(AuditEvent.kind == "dial_skipped").limit(1))
    assert event.distributor_id == on_list.id
    assert event.detail["campaign_id"] == campaign.id and event.detail["contact_id"] == contacts[0].id


def test_arn_valid_today_is_dialled(session, campaign, provider, kb, settings, make_distributor):
    _member(session, campaign, make_distributor(arn_valid_till=date(2026, 10, 13)))
    assert _dial(session, campaign, provider, kb, settings).placed == 1


def test_skipped_contacts_do_not_consume_capacity(
    session, campaign, provider, kb, settings, make_distributor
):
    for _ in range(4):
        _member(session, campaign, make_distributor(do_not_call=True))
    good = [_member(session, campaign, make_distributor()) for _ in range(3)]
    report = _dial(session, campaign, provider, kb, settings)
    assert (report.placed, report.skipped) == (3, 4)
    assert all(c.state == ContactState.IN_PROGRESS for c in good)


def test_exhausted_attempts_are_closed(session, campaign, provider, kb, settings, make_distributor):
    contact = _member(session, campaign, make_distributor(), attempts=kb.campaign.max_attempts)
    report = _dial(session, campaign, provider, kb, settings)
    assert report.placed == 0 and report.skipped == 1
    assert contact.state == ContactState.DONE
    assert contact.final_outcome == CallOutcome.NO_OUTCOME


# --------------------------------------------------------------------------------------------
# dial_due_contacts: placing calls
# --------------------------------------------------------------------------------------------


def test_success_path_with_simulator(session, campaign, provider, kb, settings, make_distributor):
    d = make_distributor(preferred_language="hi-IN")
    contact = _member(session, campaign, d)
    report = _dial(session, campaign, provider, kb, settings)
    assert (report.placed, report.skipped, report.failed) == (1, 0, 0)
    assert report.messages == []

    call = session.scalar(select(Call).where(Call.contact_id == contact.id))
    assert provider.placed == [
        {"to_number": d.phone, "call_id": call.id, "provider_call_id": call.provider_call_id}
    ]
    assert call.status == CallStatus.INITIATED
    assert call.provider == "simulator"
    assert call.campaign_id == campaign.id
    assert call.language == "hi-IN"
    assert contact.state == ContactState.IN_PROGRESS
    assert contact.attempts == 1
    assert contact.last_attempt_at == IN_WINDOW_UTC
    # Committed: visible from a brand-new session (as a webhook in another process would see it).
    from callingbot import db

    other = db.new_session()
    try:
        assert other.get(Call, call.id).provider_call_id == call.provider_call_id
    finally:
        other.close()


def test_status_already_advanced_by_webhook_is_not_overwritten(
    session, campaign, kb, settings, make_distributor
):
    class RacingProvider(SimulatorProvider):
        # Simulates a "ringing" webhook processed (and committed) before place_call returns.
        def place_call(self, *, to_number: str, call_id: int) -> PlaceCallResult:
            from callingbot import db

            other = db.new_session()
            try:
                other.get(Call, call_id).status = CallStatus.RINGING
                other.commit()
            finally:
                other.close()
            return super().place_call(to_number=to_number, call_id=call_id)

    contact = _member(session, campaign, make_distributor())
    assert _dial(session, campaign, RacingProvider(), kb, settings).placed == 1
    call = session.scalar(select(Call).where(Call.contact_id == contact.id))
    assert call.status == CallStatus.RINGING
    assert call.provider_call_id.startswith("SIM-")


def test_telephony_error_marks_failed_and_schedules_retry(session, campaign, kb, settings, make_distributor):
    contact = _member(session, campaign, make_distributor())
    report = _dial(session, campaign, FailingProvider(), kb, settings)
    assert (report.placed, report.failed) == (0, 1)
    call = session.scalar(select(Call).where(Call.contact_id == contact.id))
    assert call.status == CallStatus.FAILED
    assert "carrier rejected" in call.error
    assert call.ended_at == IN_WINDOW_UTC
    assert call.engine_state["finalized"] is True
    assert contact.attempts == 1
    assert contact.state == ContactState.PENDING
    assert contact.next_attempt_at == IN_WINDOW_UTC + timedelta(minutes=180)
    failed = session.scalar(select(AuditEvent).where(AuditEvent.kind == "dial_failed"))
    assert failed.call_id == call.id and "carrier rejected" in failed.detail["error"]


def test_unexpected_provider_exception_is_handled_like_a_failure(
    session, campaign, kb, settings, make_distributor
):
    contact = _member(session, campaign, make_distributor())
    report = _dial(session, campaign, BuggyProvider(), kb, settings)
    assert report.failed == 1
    assert contact.state == ContactState.PENDING


def test_zero_backoff_failure_is_not_redialled_in_the_same_round(
    session, campaign, kb, settings, make_distributor
):
    contacts = [_member(session, campaign, make_distributor()) for _ in range(2)]
    eager = _policy(kb, retry_backoff_minutes=[0], max_concurrent_calls=10, calls_per_minute=10)
    report = _dial(session, campaign, FailingProvider(), eager, settings)
    assert report.failed == 2
    assert all(c.attempts == 1 for c in contacts)


def test_due_bot_callback_is_closed_when_redialled(
    session, campaign, provider, kb, settings, make_distributor
):
    d = make_distributor()
    _member(session, campaign, d, next_attempt_at=IN_WINDOW_UTC - timedelta(minutes=5))
    due = Callback(distributor_id=d.id, scheduled_for=IN_WINDOW_UTC - timedelta(minutes=5), with_rm=False)
    rm = Callback(distributor_id=d.id, scheduled_for=IN_WINDOW_UTC - timedelta(minutes=5), with_rm=True)
    session.add_all([due, rm])
    session.flush()
    assert _dial(session, campaign, provider, kb, settings).placed == 1
    assert due.status == CallbackStatus.DONE
    assert rm.status == CallbackStatus.PENDING


# --------------------------------------------------------------------------------------------
# reap_stale_calls
# --------------------------------------------------------------------------------------------


def test_reap_stale_calls(session, campaign, provider, kb, settings, make_distributor):
    contact = _member(session, campaign, make_distributor())
    _dial(session, campaign, provider, kb, settings)
    stale_call = session.scalar(select(Call).where(Call.contact_id == contact.id))
    stale_call.created_at = IN_WINDOW_UTC
    fresh = make_distributor()
    fresh_call = Call(distributor_id=fresh.id, provider="simulator", status=CallStatus.RINGING)
    done_call = Call(distributor_id=fresh.id, provider="simulator", status=CallStatus.COMPLETED)
    session.add_all([fresh_call, done_call])
    session.flush()

    threshold = settings.max_call_seconds * 2 + 120
    now = IN_WINDOW_UTC + timedelta(seconds=threshold)
    fresh_call.created_at = now - timedelta(seconds=10)
    done_call.created_at = IN_WINDOW_UTC - timedelta(days=1)
    assert reap_stale_calls(session, kb=kb, settings=settings, now_utc=now) == 0  # exactly at threshold

    now += timedelta(seconds=1)
    assert reap_stale_calls(session, kb=kb, settings=settings, now_utc=now) == 1
    assert stale_call.status == CallStatus.FAILED
    assert stale_call.error == "stale: no terminal status received"
    assert stale_call.ended_at == now
    assert stale_call.engine_state["finalized"] is True
    assert contact.state == ContactState.PENDING and contact.next_attempt_at is not None
    assert fresh_call.status == CallStatus.RINGING
    assert done_call.status == CallStatus.COMPLETED
    assert reap_stale_calls(session, kb=kb, settings=settings, now_utc=now) == 0

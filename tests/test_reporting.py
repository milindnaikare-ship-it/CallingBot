"""Tests for callingbot.services.reporting.campaign_stats on a seeded database."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import IN_WINDOW_UTC

from callingbot.models import (
    Call,
    Callback,
    CallbackStatus,
    CallOutcome,
    CallStatus,
    Campaign,
    CampaignContact,
    CampaignStatus,
    ContactState,
    EmpanelmentStatus,
    LinkClick,
    MessageChannel,
    MessageStatus,
    OutboundMessage,
)
from callingbot.services.reporting import campaign_stats

S = EmpanelmentStatus


@pytest.fixture
def seeded(session, make_distributor):
    """Campaign "A" with four members and calls, plus activity outside the campaign."""
    campaign = Campaign(name="A", status=CampaignStatus.ACTIVE)
    other_campaign = Campaign(name="B", status=CampaignStatus.ACTIVE)
    session.add_all([campaign, other_campaign])
    session.flush()

    d1 = make_distributor(status=S.INTERESTED)
    d2 = make_distributor(status=S.LINK_SENT)
    d3 = make_distributor(status=S.NEW)
    d4 = make_distributor(status=S.DO_NOT_CALL, do_not_call=True)  # opted out on the call
    d5 = make_distributor(status=S.EMPANELLED)  # not a member of A
    make_distributor(status=S.DO_NOT_CALL, do_not_call=True)  # DNC at import, never called

    for d, state in (
        (d1, ContactState.DONE),
        (d2, ContactState.DONE),
        (d3, ContactState.PENDING),
        (d4, ContactState.DONE),
    ):
        session.add(CampaignContact(campaign_id=campaign.id, distributor_id=d.id, state=state))

    t = IN_WINDOW_UTC

    def call(d, status, *, campaign_id=campaign.id, **kw):
        c = Call(distributor_id=d.id, campaign_id=campaign_id, provider="simulator", status=status, **kw)
        session.add(c)
        session.flush()
        return c

    human = {"answered_by": "human", "answered_at": t}
    c1 = call(
        d1, CallStatus.COMPLETED, duration_seconds=120, outcome=CallOutcome.INTERESTED, turn_count=5, **human
    )
    c2 = call(
        d2, CallStatus.COMPLETED, duration_seconds=60, outcome=CallOutcome.LINK_SENT, turn_count=4, **human
    )
    call(d3, CallStatus.NO_ANSWER, duration_seconds=0, outcome=CallOutcome.NO_OUTCOME)
    call(d4, CallStatus.COMPLETED, duration_seconds=30, outcome=CallOutcome.OPTED_OUT, turn_count=1, **human)
    call(
        d3,
        CallStatus.VOICEMAIL,
        duration_seconds=20,
        outcome=CallOutcome.VOICEMAIL,
        answered_by="machine",
        answered_at=t,
    )
    c6 = call(
        d5,
        CallStatus.COMPLETED,
        campaign_id=other_campaign.id,
        duration_seconds=100,
        outcome=CallOutcome.ALREADY_EMPANELLED,
        turn_count=2,
        **human,
    )

    def message(c, status, link):
        session.add(
            OutboundMessage(
                distributor_id=c.distributor_id,
                call_id=c.id,
                channel=MessageChannel.SMS,
                destination="+919800000000",
                body="link",
                link=link,
                status=status,
            )
        )

    message(c2, MessageStatus.QUEUED, "https://bot.example.test/r/a")
    message(c2, MessageStatus.FAILED, "https://bot.example.test/r/b")
    message(c6, MessageStatus.SENT, "https://bot.example.test/r/c")
    message(c1, MessageStatus.SENT, None)
    session.add_all(
        [
            LinkClick(distributor_id=d2.id, call_id=c2.id),
            LinkClick(distributor_id=d2.id, call_id=c2.id),
            LinkClick(distributor_id=d5.id, call_id=c6.id),
            Callback(distributor_id=d1.id, call_id=c1.id, scheduled_for=t + timedelta(days=1)),
            Callback(distributor_id=d2.id, call_id=c2.id, scheduled_for=t, status=CallbackStatus.DONE),
            Callback(distributor_id=d5.id, call_id=c6.id, scheduled_for=t + timedelta(days=2)),
        ]
    )
    session.flush()
    return campaign


def test_global_stats(session, seeded):
    stats = campaign_stats(session)
    assert stats["distributors_total"] == 6
    assert {k: v for k, v in stats["by_status"].items() if v} == {
        "new": 1,
        "interested": 1,
        "link_sent": 1,
        "empanelled": 1,
        "do_not_call": 2,
    }
    assert set(stats["by_status"]) == {s.value for s in EmpanelmentStatus}
    assert stats["calls_total"] == 6
    assert {k: v for k, v in stats["calls_by_status"].items() if v} == {
        "completed": 4,
        "no_answer": 1,
        "voicemail": 1,
    }
    assert stats["calls_by_outcome"]["interested"] == 1
    assert stats["calls_by_outcome"]["voicemail"] == 1
    assert stats["calls_by_outcome"]["transferred"] == 0
    assert stats["connected"] == 4  # the machine-answered voicemail does not count
    assert stats["connect_rate"] == pytest.approx(4 / 6)
    assert stats["links_sent"] == 2  # failed sends excluded
    assert stats["link_clicks"] == 3
    assert stats["callbacks_pending"] == 2
    assert stats["opt_outs"] == 1
    assert stats["avg_call_duration_seconds"] == 66.0  # zero-length no-answer excluded
    assert "contacts_by_state" not in stats
    assert stats["funnel"] == [
        ("New", 6),
        ("Contacted", 4),  # interested, link sent, empanelled + the opt-out who was called
        ("Interested", 3),
        ("Link sent", 2),
        ("Empanelled", 1),
    ]


def test_campaign_scoped_stats(session, seeded):
    stats = campaign_stats(session, seeded.id)
    assert stats["distributors_total"] == 4
    assert stats["calls_total"] == 5
    assert stats["connected"] == 3
    assert stats["connect_rate"] == pytest.approx(0.6)
    assert stats["links_sent"] == 1
    assert stats["link_clicks"] == 2
    assert stats["callbacks_pending"] == 1
    assert stats["opt_outs"] == 1
    assert stats["avg_call_duration_seconds"] == 57.5
    assert stats["contacts_by_state"] == {"pending": 1, "in_progress": 0, "done": 3, "skipped": 0}
    assert stats["funnel"] == [
        ("New", 4),
        ("Contacted", 3),
        ("Interested", 2),
        ("Link sent", 1),
        ("Empanelled", 0),
    ]


def test_empty_database(session):
    stats = campaign_stats(session)
    assert stats["distributors_total"] == 0
    assert stats["calls_total"] == 0
    assert stats["connect_rate"] == 0.0
    assert stats["avg_call_duration_seconds"] == 0.0
    assert [count for _, count in stats["funnel"]] == [0, 0, 0, 0, 0]
    assert [label for label, _ in stats["funnel"]] == [
        "New",
        "Contacted",
        "Interested",
        "Link sent",
        "Empanelled",
    ]


def test_empty_campaign(session):
    campaign = Campaign(name="Empty")
    session.add(campaign)
    session.flush()
    stats = campaign_stats(session, campaign.id)
    assert stats["distributors_total"] == 0 and stats["calls_total"] == 0
    assert stats["contacts_by_state"] == {s.value: 0 for s in ContactState}

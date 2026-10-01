"""Campaign and funnel statistics for the dashboard and ``callingbot stats``.

All numbers are computed with SQL aggregates so they stay cheap on large distributor lists.
With ``campaign_id`` everything is scoped to that campaign: distributors are its members, calls
are the calls it placed, and messages / clicks / callbacks are those tied to its calls.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from callingbot.models import (
    Call,
    Callback,
    CallbackStatus,
    CallOutcome,
    CallStatus,
    CampaignContact,
    ContactState,
    Distributor,
    EmpanelmentStatus,
    LinkClick,
    MessageStatus,
    OutboundMessage,
)

S = EmpanelmentStatus

# Cumulative funnel: each stage counts distributors who reached *at least* that stage, so the
# bars only ever shrink from left to right. "New" is everyone in scope.
FUNNEL_STAGES: tuple[tuple[str, frozenset[EmpanelmentStatus] | None], ...] = (
    ("New", None),
    # Anyone a call reached. Opt-outs only count when we actually called them (an opt-out can
    # also come from the DNC list at import, which is not contact).
    (
        "Contacted",
        frozenset(
            {
                S.CONTACTED,
                S.NOT_INTERESTED,
                S.INTERESTED,
                S.CALLBACK_SCHEDULED,
                S.LINK_SENT,
                S.WRONG_NUMBER,
                S.ALREADY_EMPANELLED,
                S.EMPANELLED,
            }
        ),
    ),
    ("Interested", frozenset({S.INTERESTED, S.CALLBACK_SCHEDULED, S.LINK_SENT, S.EMPANELLED})),
    ("Link sent", frozenset({S.LINK_SENT, S.EMPANELLED})),
    ("Empanelled", frozenset({S.EMPANELLED})),
)


def _connected_clause():
    # Connected = a person picked up: answered by a human, or reached in-progress / spoke at
    # least once, unless answering-machine detection says it was a machine.
    not_machine = or_(Call.answered_by.is_(None), Call.answered_by != "machine")
    reached = or_(
        Call.answered_at.is_not(None),
        Call.status == CallStatus.IN_PROGRESS,
        Call.turn_count > 0,
    )
    return or_(
        Call.answered_by == "human",
        and_(reached, not_machine, Call.status != CallStatus.VOICEMAIL),
    )


def campaign_stats(session: Session, campaign_id: int | None = None) -> dict[str, Any]:
    """Funnel and call statistics, optionally scoped to one campaign.

    Keys: ``distributors_total``, ``by_status`` {status: count} (every status, zeros included),
    ``calls_total``, ``calls_by_status``, ``calls_by_outcome`` (every value, zeros included),
    ``connected``, ``connect_rate`` (0..1; 0 when there are no calls), ``links_sent`` (messages
    carrying a link that did not fail), ``link_clicks``, ``callbacks_pending``, ``opt_outs``
    (distinct distributors with an opted-out call), ``avg_call_duration_seconds`` (over calls
    with a positive duration; 0.0 when none), ``contacts_by_state`` (only with ``campaign_id``)
    and ``funnel`` - ``[(label, count), ...]`` New -> Contacted -> Interested -> Link sent ->
    Empanelled, cumulative.
    """
    dist_filter = []
    call_filter = []
    if campaign_id is not None:
        members = select(CampaignContact.distributor_id).where(CampaignContact.campaign_id == campaign_id)
        dist_filter.append(Distributor.id.in_(members))
        call_filter.append(Call.campaign_id == campaign_id)
    campaign_calls = select(Call.id).where(*call_filter)

    by_status = {s.value: 0 for s in EmpanelmentStatus}
    for status, n in session.execute(
        select(Distributor.status, func.count(Distributor.id))
        .where(*dist_filter)
        .group_by(Distributor.status)
    ):
        by_status[EmpanelmentStatus(status).value] = n
    distributors_total = sum(by_status.values())

    calls_by_status = {s.value: 0 for s in CallStatus}
    for status, n in session.execute(
        select(Call.status, func.count(Call.id)).where(*call_filter).group_by(Call.status)
    ):
        calls_by_status[CallStatus(status).value] = n
    calls_total = sum(calls_by_status.values())

    calls_by_outcome = {o.value: 0 for o in CallOutcome}
    for outcome, n in session.execute(
        select(Call.outcome, func.count(Call.id))
        .where(*call_filter, Call.outcome.is_not(None))
        .group_by(Call.outcome)
    ):
        calls_by_outcome[CallOutcome(outcome).value] = n

    connected = session.scalar(select(func.count(Call.id)).where(*call_filter, _connected_clause())) or 0

    msg_filter = [OutboundMessage.link.is_not(None), OutboundMessage.status != MessageStatus.FAILED]
    click_filter = []
    callback_filter = [Callback.status == CallbackStatus.PENDING]
    if campaign_id is not None:
        msg_filter.append(OutboundMessage.call_id.in_(campaign_calls))
        click_filter.append(LinkClick.call_id.in_(campaign_calls))
        callback_filter.append(Callback.call_id.in_(campaign_calls))
    links_sent = session.scalar(select(func.count(OutboundMessage.id)).where(*msg_filter)) or 0
    link_clicks = session.scalar(select(func.count(LinkClick.id)).where(*click_filter)) or 0
    callbacks_pending = session.scalar(select(func.count(Callback.id)).where(*callback_filter)) or 0

    opt_outs = (
        session.scalar(
            select(func.count(func.distinct(Call.distributor_id))).where(
                *call_filter, Call.outcome == CallOutcome.OPTED_OUT
            )
        )
        or 0
    )

    avg_duration = session.scalar(
        select(func.avg(Call.duration_seconds)).where(*call_filter, Call.duration_seconds > 0)
    )

    funnel: list[tuple[str, int]] = []
    for label, statuses in FUNNEL_STAGES:
        if statuses is None:
            funnel.append((label, distributors_total))
            continue
        count = sum(by_status[s.value] for s in statuses)
        if label == "Contacted":
            # Opted-out distributors who were actually called (at least one connected call).
            called_dnc = session.scalar(
                select(func.count(Distributor.id)).where(
                    *dist_filter,
                    Distributor.status == S.DO_NOT_CALL,
                    exists().where(Call.distributor_id == Distributor.id, *call_filter, _connected_clause()),
                )
            )
            count += called_dnc or 0
        funnel.append((label, count))

    stats: dict[str, Any] = {
        "distributors_total": distributors_total,
        "by_status": by_status,
        "calls_total": calls_total,
        "calls_by_status": calls_by_status,
        "calls_by_outcome": calls_by_outcome,
        "connected": connected,
        "connect_rate": (connected / calls_total) if calls_total else 0.0,
        "links_sent": links_sent,
        "link_clicks": link_clicks,
        "callbacks_pending": callbacks_pending,
        "opt_outs": opt_outs,
        "avg_call_duration_seconds": round(float(avg_duration), 1) if avg_duration is not None else 0.0,
        "funnel": funnel,
    }
    if campaign_id is not None:
        contacts_by_state = {s.value: 0 for s in ContactState}
        for state, n in session.execute(
            select(CampaignContact.state, func.count(CampaignContact.id))
            .where(CampaignContact.campaign_id == campaign_id)
            .group_by(CampaignContact.state)
        ):
            contacts_by_state[ContactState(state).value] = n
        stats["contacts_by_state"] = contacts_by_state
    return stats

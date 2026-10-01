"""Campaign dialling: pick due contacts and place calls within policy.

Every call the dialer places has passed, at that moment:

* the campaign being ACTIVE (pausing stops new calls immediately);
* the calling window of ``config/campaign.yaml`` in the business timezone (TRAI TCCCPR hours;
  the AMC's window is stricter);
* pacing and concurrency (``calls_per_minute``, ``max_concurrent_calls``);
* a fresh eligibility check of the distributor - internal DNC list / opt-out, funnel status,
  ARN validity (AMFI: an expired ARN may not sell or be empanelled), a valid Indian mobile and
  the per-campaign attempt limit. The check is repeated here, not only when the contact was
  added, because an opt-out can arrive at any time between the two.

:func:`dial_due_contacts` and :func:`reap_stale_calls` **commit**: the call row must be visible
to the webhook handlers (another process) before the provider can ring the phone.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from callingbot import compliance, funnel
from callingbot.compliance import WindowDecision
from callingbot.knowledge import KnowledgeBase
from callingbot.models import (
    TERMINAL_CALL_STATUSES,
    Call,
    Callback,
    CallbackStatus,
    CallOutcome,
    CallStatus,
    Campaign,
    CampaignContact,
    CampaignStatus,
    ContactState,
    Distributor,
    DNCEntry,
    EmpanelmentStatus,
    audit,
)
from callingbot.phone import mask_phone, normalize_indian_mobile
from callingbot.services.lifecycle import apply_status_update, create_call, finalize_call
from callingbot.settings import Settings
from callingbot.telephony.base import CallStatusUpdate, TelephonyError, TelephonyProvider
from callingbot.timeutil import to_local

log = logging.getLogger(__name__)

_ACTIVE_STATUSES = (CallStatus.INITIATED, CallStatus.RINGING, CallStatus.IN_PROGRESS)
# A QUEUED call is normally handed to the provider within a second. One still QUEUED after this
# long was never placed (crash between commit and place_call, or an abandoned web-simulator
# session) and must not block capacity until the reaper gets to it.
QUEUED_STALE_SECONDS = 120
_DUE_BATCH = 50


@dataclass
class DialReport:
    placed: int = 0
    skipped: int = 0
    failed: int = 0
    window: WindowDecision | None = None
    messages: list[str] = field(default_factory=list)


def stale_after_seconds(settings: Settings) -> int:
    """Age after which a non-terminal call is assumed lost: twice the longest call plus slack."""
    return settings.max_call_seconds * 2 + 120


def _dnc_phones(session: Session) -> set[str]:
    # Same rule as compliance.is_dnc (DNC list, or any do-not-call distributor using the
    # number), precomputed once so adding thousands of contacts is not thousands of queries.
    phones = set(session.scalars(select(DNCEntry.phone)))
    flagged = session.execute(
        select(Distributor.phone, Distributor.alt_phone).where(
            or_(Distributor.do_not_call.is_(True), Distributor.status == EmpanelmentStatus.DO_NOT_CALL)
        )
    )
    for phone, alt in flagged:
        phones.update(p for p in (phone, alt) if p)
    return phones


def add_distributors_to_campaign(
    session: Session, campaign: Campaign, distributor_ids: Iterable[int] | None = None
) -> int:
    """Add PENDING contacts for dialable distributors; returns how many were added.

    With ``distributor_ids`` only those distributors are considered, otherwise every distributor.
    Either way do-not-call, non-dialable (opted out, empanelled, wrong number) and DNC-listed
    distributors are left out and existing members are not duplicated. Flushes; caller commits.
    """
    stmt = select(Distributor).where(
        Distributor.do_not_call.is_(False),
        Distributor.status.not_in(list(funnel.NON_DIALABLE_STATUSES)),
    )
    if distributor_ids is not None:
        ids = list(dict.fromkeys(distributor_ids))
        if not ids:
            return 0
        stmt = stmt.where(Distributor.id.in_(ids))
    candidates = session.scalars(stmt.order_by(Distributor.id)).all()

    members = set(
        session.scalars(
            select(CampaignContact.distributor_id).where(CampaignContact.campaign_id == campaign.id)
        )
    )
    dnc = _dnc_phones(session)
    added = 0
    for d in candidates:
        if d.id in members:
            continue
        if (normalize_indian_mobile(d.phone) or d.phone) in dnc:
            continue
        session.add(
            CampaignContact(
                campaign_id=campaign.id,
                distributor_id=d.id,
                state=ContactState.PENDING,
                attempts=0,
                next_attempt_at=None,
            )
        )
        members.add(d.id)
        added += 1
    session.flush()
    return added


def _due_stmt(campaign: Campaign, now_utc: datetime):
    return (
        select(CampaignContact)
        .where(
            CampaignContact.campaign_id == campaign.id,
            CampaignContact.state == ContactState.PENDING,
            or_(CampaignContact.next_attempt_at.is_(None), CampaignContact.next_attempt_at <= now_utc),
        )
        # "NULLS FIRST" spelled portably: False (no time set = as soon as possible) sorts first
        # on both SQLite and PostgreSQL.
        .order_by(
            CampaignContact.next_attempt_at.is_not(None),
            CampaignContact.next_attempt_at,
            CampaignContact.id,
        )
    )


def select_due_contacts(
    session: Session, campaign: Campaign, *, now_utc: datetime, limit: int
) -> list[CampaignContact]:
    """PENDING contacts whose ``next_attempt_at`` is unset or due, ASAP ones first, then by time and id."""
    if limit <= 0:
        return []
    return list(session.scalars(_due_stmt(campaign, now_utc).limit(limit)))


def count_active_calls(session: Session, *, now_utc: datetime) -> int:
    """Calls currently using a line, across all campaigns and providers."""
    queued_cutoff = now_utc - timedelta(seconds=QUEUED_STALE_SECONDS)
    return (
        session.scalar(
            select(func.count(Call.id)).where(
                or_(
                    Call.status.in_(_ACTIVE_STATUSES),
                    (Call.status == CallStatus.QUEUED) & (Call.created_at >= queued_cutoff),
                )
            )
        )
        or 0
    )


def _skip(
    session: Session, contact: CampaignContact, distributor: Distributor, reason: str, report: DialReport
):
    contact.state = ContactState.SKIPPED
    report.skipped += 1
    audit(
        session,
        "dial_skipped",
        distributor_id=distributor.id,
        reason=reason,
        campaign_id=contact.campaign_id,
        contact_id=contact.id,
    )
    log.info(
        "Skipped %s (%s) in campaign %s: %s",
        distributor.arn,
        mask_phone(distributor.phone),
        contact.campaign_id,
        reason,
    )


def _ineligible_reason(session: Session, distributor: Distributor, local_today) -> str | None:
    if distributor.do_not_call or compliance.is_dnc(session, distributor.phone):
        return "dnc"
    if distributor.status in funnel.NON_DIALABLE_STATUSES:
        return f"status_{EmpanelmentStatus(distributor.status).value}"
    if distributor.arn_valid_till is not None and distributor.arn_valid_till < local_today:
        return "arn_expired"
    if normalize_indian_mobile(distributor.phone) is None:
        return "invalid_phone"
    return None


def _close_due_bot_callbacks(session: Session, distributor: Distributor, now_utc: datetime) -> None:
    # The re-call a bot callback asked for is the call just placed; leave RM callbacks alone.
    for cb in session.scalars(
        select(Callback).where(
            Callback.distributor_id == distributor.id,
            Callback.with_rm.is_(False),
            Callback.status == CallbackStatus.PENDING,
            Callback.scheduled_for <= now_utc,
        )
    ):
        cb.status = CallbackStatus.DONE


def dial_due_contacts(
    session: Session,
    *,
    campaign: Campaign,
    provider: TelephonyProvider,
    kb: KnowledgeBase,
    settings: Settings,
    now_utc: datetime,
    max_new_calls: int | None = None,
) -> DialReport:
    """Place calls for the campaign's due contacts, within window, pacing and concurrency limits.

    Commits after each contact is processed, and before ``place_call`` so the provider's first
    webhook always finds the call row. A provider failure marks the call FAILED and schedules a
    retry via :func:`finalize_call`; it never aborts the round.
    """
    report = DialReport()
    policy = kb.campaign
    if campaign.status != CampaignStatus.ACTIVE:
        report.messages.append(
            f"Campaign {campaign.name!r} is {CampaignStatus(campaign.status).value}, not active: nothing dialled."
        )
        return report

    window = compliance.check_calling_window(policy, now_utc, settings.timezone)
    report.window = window
    if not window.allowed:
        next_text = ""
        if window.next_allowed_utc is not None:
            next_local = to_local(window.next_allowed_utc, settings.timezone)
            next_text = f"; next window opens {next_local:%a %d %b %H:%M} ({settings.timezone})"
        report.messages.append(f"Outside the calling window ({window.reason}){next_text}.")
        return report

    active = count_active_calls(session, now_utc=now_utc)
    limits = [policy.calls_per_minute, policy.max_concurrent_calls - active]
    if max_new_calls is not None:
        limits.append(max_new_calls)
    capacity = max(0, min(limits))
    if capacity == 0:
        report.messages.append(
            f"No capacity: {active} active call(s), max_concurrent_calls={policy.max_concurrent_calls}."
        )
        return report

    local_today = to_local(now_utc, settings.timezone).date()
    processed: set[int] = set()
    while report.placed + report.failed < capacity:
        stmt = _due_stmt(campaign, now_utc)
        if processed:
            # A contact retried with zero backoff would otherwise be picked again this round.
            stmt = stmt.where(CampaignContact.id.not_in(processed))
        batch = list(session.scalars(stmt.limit(_DUE_BATCH)))
        if not batch:
            break
        for contact in batch:
            if report.placed + report.failed >= capacity:
                break
            processed.add(contact.id)
            _dial_one(session, contact, campaign, provider, kb, settings, now_utc, local_today, report)
            session.commit()

    if report.placed + report.failed == 0 and report.skipped == 0:
        report.messages.append("No contacts due.")
    return report


def _dial_one(
    session: Session,
    contact: CampaignContact,
    campaign: Campaign,
    provider: TelephonyProvider,
    kb: KnowledgeBase,
    settings: Settings,
    now_utc: datetime,
    local_today,
    report: DialReport,
) -> None:
    distributor = contact.distributor
    reason = _ineligible_reason(session, distributor, local_today)
    if reason is not None:
        _skip(session, contact, distributor, reason, report)
        return
    if (contact.attempts or 0) >= kb.campaign.max_attempts:
        contact.state = ContactState.DONE
        contact.final_outcome = contact.final_outcome or CallOutcome.NO_OUTCOME
        report.skipped += 1
        return

    call = create_call(
        session,
        distributor=distributor,
        provider=provider.name,
        campaign=campaign,
        contact=contact,
        language=distributor.preferred_language,
    )
    contact.attempts = (contact.attempts or 0) + 1
    contact.last_attempt_at = now_utc
    contact.state = ContactState.IN_PROGRESS
    session.flush()
    session.commit()  # the provider may call our webhooks before place_call even returns

    try:
        result = provider.place_call(to_number=distributor.phone, call_id=call.id)
    except Exception as exc:
        # TelephonyError is the documented failure. Anything else is an adapter bug; it is logged
        # with a traceback but handled the same way so one bad dial cannot stall the campaign.
        if not isinstance(exc, TelephonyError):
            log.exception("Unexpected error from %s.place_call for call %s", provider.name, call.id)
        error = str(exc)[:1000] or type(exc).__name__
        call.status = CallStatus.FAILED
        call.error = error
        call.ended_at = now_utc
        audit(
            session,
            "dial_failed",
            call_id=call.id,
            distributor_id=distributor.id,
            provider=provider.name,
            campaign_id=campaign.id,
            contact_id=contact.id,
            error=error,
        )
        finalize_call(session, call, kb=kb, now_utc=now_utc, tz=settings.timezone)
        report.failed += 1
        log.warning("Dial failed for call %s (%s): %s", call.id, mask_phone(distributor.phone), error)
        return

    # A status webhook may already have moved the call on in another process; reload it and
    # merge the result forward-only instead of overwriting.
    session.refresh(call)
    apply_status_update(
        session,
        call,
        CallStatusUpdate(provider_call_id=result.provider_call_id, status=result.status),
        kb=kb,
        now_utc=now_utc,
        tz=settings.timezone,
    )
    _close_due_bot_callbacks(session, distributor, now_utc)
    report.placed += 1
    log.info("Placed call %s to %s via %s", call.id, mask_phone(distributor.phone), provider.name)


def reap_stale_calls(session: Session, *, kb: KnowledgeBase, settings: Settings, now_utc: datetime) -> int:
    """Fail and finalise calls stuck in a non-terminal status (lost webhook, crash). Commits.

    A call is stale when it was created more than ``max_call_seconds * 2 + 120`` seconds ago and
    is still not terminal; finalising it frees its capacity slot and schedules the retry.
    """
    cutoff = now_utc - timedelta(seconds=stale_after_seconds(settings))
    stale = session.scalars(
        select(Call).where(Call.status.not_in(list(TERMINAL_CALL_STATUSES)), Call.created_at < cutoff)
    ).all()
    for call in stale:
        call.status = CallStatus.FAILED
        call.error = "stale: no terminal status received"
        call.ended_at = now_utc
        finalize_call(session, call, kb=kb, now_utc=now_utc, tz=settings.timezone)
        log.warning("Reaped stale call %s (created %s)", call.id, call.created_at)
    session.commit()
    return len(stale)

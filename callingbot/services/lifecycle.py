"""Call lifecycle: create a call, apply provider status callbacks, finalise the outcome.

Status callbacks arrive out of order, more than once, and sometimes never (a lost webhook). The
rules here make the call record converge regardless:

* Non-terminal statuses only move forward (queued < initiated < ringing < in-progress), so a
  late "ringing" never overwrites "in-progress".
* The first terminal status wins and is sticky; later callbacks may only fill in data that was
  missing (duration, recording URL).
* :func:`finalize_call` runs exactly once per call (``engine_state["finalized"]``). It decides
  the business consequences: default outcome, the distributor's funnel status and whether the
  campaign contact is retried (inside the calling window, after the configured backoff) or done.

Nothing here commits; callers (webhook routes, the dialer, the CLI) own the transaction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from callingbot import compliance, funnel
from callingbot.knowledge import KnowledgeBase
from callingbot.models import (
    Call,
    Callback,
    CallbackStatus,
    CallOutcome,
    CallStatus,
    Campaign,
    CampaignContact,
    ContactState,
    Distributor,
    EmpanelmentStatus,
    audit,
)
from callingbot.telephony.base import CallStatusUpdate

log = logging.getLogger(__name__)

DEFAULT_TZ = "Asia/Kolkata"
DEFAULT_LANGUAGE = "en-IN"

# Forward-only ordering of the non-terminal statuses.
_PROGRESS: dict[CallStatus, int] = {
    CallStatus.QUEUED: 0,
    CallStatus.INITIATED: 1,
    CallStatus.RINGING: 2,
    CallStatus.IN_PROGRESS: 3,
}

# Terminal statuses that contradict a conversation having happened. Answering-machine detection
# can misfire on a human and some carriers report "no-answer"/"busy" for a call that was in fact
# connected; once the distributor has spoken, the call is recorded as completed.
_CONTRADICTED_BY_CONVERSATION = frozenset({CallStatus.VOICEMAIL, CallStatus.NO_ANSWER, CallStatus.BUSY})

# A connected call with at least this many distributor turns and no recorded outcome is closed
# rather than retried: the person engaged and chose to end the call, and re-pitching them in the
# same campaign is unwelcome (and close to the line of unsolicited repeated calls under TCCCPR).
CONNECTED_TURNS_NO_RETRY = 2


def create_call(
    session: Session,
    *,
    distributor: Distributor,
    provider: str,
    campaign: Campaign | None = None,
    contact: CampaignContact | None = None,
    language: str | None = None,
) -> Call:
    """Create a QUEUED call row for ``distributor`` and flush (``call.id`` is available)."""
    campaign_id = campaign.id if campaign is not None else (contact.campaign_id if contact else None)
    call = Call(
        distributor=distributor,
        distributor_id=distributor.id,
        campaign_id=campaign_id,
        contact_id=contact.id if contact is not None else None,
        provider=provider,
        status=CallStatus.QUEUED,
        language=language or distributor.preferred_language or DEFAULT_LANGUAGE,
        llm_messages=[],
        engine_state={},
        turn_count=0,
        no_input_count=0,
    )
    session.add(call)
    session.flush()
    return call


def apply_status_update(
    session: Session,
    call: Call,
    update: CallStatusUpdate,
    *,
    kb: KnowledgeBase,
    now_utc: datetime,
    tz: str = DEFAULT_TZ,
) -> None:
    """Apply a provider status callback to ``call``. Idempotent; flushes, does not commit.

    Never moves a terminal call back to non-terminal. On the first terminal status the end time,
    duration and recording are recorded and :func:`finalize_call` runs. ``tz`` is the business
    timezone used to place any retry inside the calling window.
    """
    if update.provider_call_id and not call.provider_call_id:
        call.provider_call_id = update.provider_call_id
    if update.answered_by and not call.answered_by:
        call.answered_by = update.answered_by

    current = CallStatus(call.status)
    new = CallStatus(update.status)

    if current.is_terminal:
        # Sticky. Late callbacks may still carry data the first one lacked.
        if call.duration_seconds is None and update.duration_seconds is not None:
            call.duration_seconds = update.duration_seconds
        if not call.recording_url and update.recording_url:
            call.recording_url = update.recording_url
        if new.is_terminal and not _is_finalized(call):
            # Someone set the terminal status without finalising (e.g. a crash in between).
            finalize_call(session, call, kb=kb, now_utc=now_utc, tz=tz)
        session.flush()
        return

    if not new.is_terminal:
        if _PROGRESS[new] > _PROGRESS[current]:
            call.status = new
        if new == CallStatus.IN_PROGRESS and call.answered_at is None:
            call.answered_at = now_utc
        session.flush()
        return

    if (
        new in _CONTRADICTED_BY_CONVERSATION
        and current == CallStatus.IN_PROGRESS
        and (call.turn_count or 0) > 0
    ):
        new = CallStatus.COMPLETED
    call.status = new
    call.ended_at = now_utc
    if update.duration_seconds is not None:
        call.duration_seconds = update.duration_seconds
    elif call.duration_seconds is None and call.answered_at is not None:
        call.duration_seconds = max(0, int((now_utc - call.answered_at).total_seconds()))
    if update.recording_url:
        call.recording_url = update.recording_url
    finalize_call(session, call, kb=kb, now_utc=now_utc, tz=tz)
    session.flush()


def _is_finalized(call: Call) -> bool:
    return bool((call.engine_state or {}).get("finalized"))


def _distributor_dialable(distributor: Distributor) -> bool:
    return not distributor.do_not_call and distributor.status not in funnel.NON_DIALABLE_STATUSES


def _bot_callback(session: Session, call: Call) -> Callback | None:
    """The latest pending bot (not RM) callback created during this call, if any."""
    return session.scalar(
        select(Callback)
        .where(
            Callback.call_id == call.id,
            Callback.with_rm.is_(False),
            Callback.status == CallbackStatus.PENDING,
        )
        .order_by(Callback.id.desc())
        .limit(1)
    )


def _retry_at(kb: KnowledgeBase, attempts: int, now_utc: datetime, tz: str) -> datetime:
    backoffs = kb.campaign.retry_backoff_minutes or [0]
    minutes = backoffs[min(max(attempts - 1, 0), len(backoffs) - 1)]
    earliest = now_utc + timedelta(minutes=minutes)
    try:
        return compliance.next_window_start(kb.campaign, earliest, tz)
    except ValueError:
        # No window in the scan horizon (misconfigured holidays). The dialer re-checks the window
        # before every call, so falling back to the bare backoff can never call out of hours.
        log.warning("No calling window found after %s; retry scheduled without window alignment", earliest)
        return earliest


def finalize_call(
    session: Session, call: Call, *, kb: KnowledgeBase, now_utc: datetime, tz: str = DEFAULT_TZ
) -> None:
    """Apply the business consequences of a finished call, exactly once. Flushes, does not commit.

    1. Defensive close: a non-terminal call is marked COMPLETED.
    2. Default outcome: VOICEMAIL for a voicemail call, otherwise NO_OUTCOME.
    3. Funnel: the outcome's status (``funnel.outcome_to_status``); a human conversation with at
       least one distributor turn also counts as CONTACTED. ``advance_status`` only moves up.
    4. Campaign contact (attempts were counted at dial time):

       * a pending *bot* callback booked on this call re-queues the contact for that time, while
         attempts remain and the distributor is still dialable - even for CALLBACK_REQUESTED;
       * a final outcome (``funnel.FINAL_OUTCOMES``) closes it (DONE, ``final_outcome``);
       * a distributor who became non-dialable (opt-out, wrong number, empanelled) closes it;
       * a connected call (``CONNECTED_TURNS_NO_RETRY``+ turns) without an outcome closes it -
         the person engaged, so we do not pitch them again in this campaign;
       * otherwise it is retried after ``retry_backoff_minutes`` at the next calling-window
         start, or closed with NO_OUTCOME once ``max_attempts`` is used up.
    5. Audit ``call_finalized``.
    """
    if _is_finalized(call):
        return

    status = CallStatus(call.status)
    if not status.is_terminal:
        status = CallStatus.COMPLETED
        call.status = status
    if call.ended_at is None:
        call.ended_at = now_utc

    outcome = CallOutcome(call.outcome) if call.outcome else None
    if outcome is None:
        outcome = CallOutcome.VOICEMAIL if status == CallStatus.VOICEMAIL else CallOutcome.NO_OUTCOME
        call.outcome = outcome

    distributor = call.distributor or session.get(Distributor, call.distributor_id)
    turns = call.turn_count or 0
    mapped = funnel.outcome_to_status(outcome)
    if mapped is not None:
        funnel.advance_status(distributor, mapped)
    human_answered = (call.answered_by or "human") != "machine" and status != CallStatus.VOICEMAIL
    if human_answered and turns >= 1:
        funnel.advance_status(distributor, EmpanelmentStatus.CONTACTED)

    contact = session.get(CampaignContact, call.contact_id) if call.contact_id else None
    contact_reason: str | None = None
    if contact is not None:
        contact_reason = _settle_contact(session, call, contact, distributor, outcome, kb, now_utc, tz)

    # Assign a new dict: in-place mutation of a JSON column is not change-tracked.
    call.engine_state = {**(call.engine_state or {}), "finalized": True, "finalized_at": now_utc.isoformat()}

    audit(
        session,
        "call_finalized",
        call_id=call.id,
        distributor_id=distributor.id,
        status=status.value,
        outcome=outcome.value,
        distributor_status=EmpanelmentStatus(distributor.status).value,
        contact_state=ContactState(contact.state).value if contact is not None else None,
        contact_reason=contact_reason,
        next_attempt_at=(
            contact.next_attempt_at.isoformat() if contact is not None and contact.next_attempt_at else None
        ),
    )
    session.flush()


def _settle_contact(
    session: Session,
    call: Call,
    contact: CampaignContact,
    distributor: Distributor,
    outcome: CallOutcome,
    kb: KnowledgeBase,
    now_utc: datetime,
    tz: str,
) -> str:
    """Set the contact's state for the next dial round; returns a short reason for the audit."""
    max_attempts = kb.campaign.max_attempts
    attempts = contact.attempts or 0
    dialable = _distributor_dialable(distributor)

    callback = _bot_callback(session, call) if dialable and attempts < max_attempts else None
    if callback is not None:
        contact.state = ContactState.PENDING
        contact.next_attempt_at = callback.scheduled_for
        contact.final_outcome = None
        return "bot_callback"

    if outcome in funnel.FINAL_OUTCOMES:
        contact.state = ContactState.DONE
        contact.final_outcome = outcome
        contact.next_attempt_at = None
        return "final_outcome"

    if not dialable:
        contact.state = ContactState.DONE
        contact.final_outcome = outcome
        contact.next_attempt_at = None
        return "not_dialable"

    if outcome == CallOutcome.NO_OUTCOME and (call.turn_count or 0) >= CONNECTED_TURNS_NO_RETRY:
        contact.state = ContactState.DONE
        contact.final_outcome = CallOutcome.NO_OUTCOME
        contact.next_attempt_at = None
        return "connected_no_outcome"

    if attempts < max_attempts:
        contact.state = ContactState.PENDING
        contact.next_attempt_at = _retry_at(kb, attempts, now_utc, tz)
        return "retry"

    contact.state = ContactState.DONE
    contact.final_outcome = CallOutcome.NO_OUTCOME
    contact.next_attempt_at = None
    return "max_attempts"

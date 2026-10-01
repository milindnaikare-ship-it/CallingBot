"""Empanelment funnel rules shared by the conversation engine, call lifecycle and dialer."""

from __future__ import annotations

from callingbot.models import CallOutcome, Distributor, EmpanelmentStatus

S = EmpanelmentStatus

# Higher rank = further along (or more final). A distributor's status only moves "up", so a
# later, weaker signal (e.g. an unanswered call) never erases progress (e.g. link sent).
_RANK: dict[EmpanelmentStatus, int] = {
    S.NEW: 0,
    S.CONTACTED: 1,
    S.NOT_INTERESTED: 2,
    S.INTERESTED: 3,
    S.CALLBACK_SCHEDULED: 4,
    S.LINK_SENT: 5,
    S.WRONG_NUMBER: 8,
    S.ALREADY_EMPANELLED: 9,
    S.EMPANELLED: 9,
    S.DO_NOT_CALL: 10,  # always wins and is never overwritten automatically
}

# Distributors in these states are never dialled or added to new campaigns automatically.
NON_DIALABLE_STATUSES = frozenset({S.DO_NOT_CALL, S.EMPANELLED, S.ALREADY_EMPANELLED, S.WRONG_NUMBER})

# Call outcomes that close a campaign contact (no more attempts in that campaign).
FINAL_OUTCOMES = frozenset(
    {
        CallOutcome.INTERESTED,
        CallOutcome.LINK_SENT,
        CallOutcome.CALLBACK_REQUESTED,
        CallOutcome.ALREADY_EMPANELLED,
        CallOutcome.NOT_INTERESTED,
        CallOutcome.WRONG_PERSON,
        CallOutcome.OPTED_OUT,
        CallOutcome.TRANSFERRED,
    }
)

_OUTCOME_TO_STATUS: dict[CallOutcome, EmpanelmentStatus] = {
    CallOutcome.INTERESTED: S.INTERESTED,
    CallOutcome.LINK_SENT: S.LINK_SENT,
    CallOutcome.CALLBACK_REQUESTED: S.CALLBACK_SCHEDULED,
    CallOutcome.ALREADY_EMPANELLED: S.ALREADY_EMPANELLED,
    CallOutcome.NOT_INTERESTED: S.NOT_INTERESTED,
    CallOutcome.WRONG_PERSON: S.WRONG_NUMBER,
    CallOutcome.OPTED_OUT: S.DO_NOT_CALL,
    CallOutcome.TRANSFERRED: S.INTERESTED,
}


def outcome_to_status(outcome: CallOutcome | None) -> EmpanelmentStatus | None:
    return _OUTCOME_TO_STATUS.get(outcome) if outcome else None


def rank(status: EmpanelmentStatus) -> int:
    return _RANK[status]


def advance_status(distributor: Distributor, new_status: EmpanelmentStatus, *, force: bool = False) -> bool:
    """Move ``distributor.status`` to ``new_status`` if it ranks higher (or ``force``).

    Returns True when the status changed. Opting out also sets ``do_not_call``.
    """
    current = distributor.status or S.NEW
    if new_status == S.DO_NOT_CALL:
        distributor.do_not_call = True
    if not force and (current == S.DO_NOT_CALL or _RANK[new_status] <= _RANK[current]):
        return False
    distributor.status = new_status
    return True

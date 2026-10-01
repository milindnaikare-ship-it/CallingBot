"""ORM models.

All ``DateTime`` columns hold **naive UTC** values (see :mod:`callingbot.timeutil`).
Enum columns store the enum's string value (``native_enum=False``) so the schema is
portable between SQLite and PostgreSQL.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from callingbot.db import Base
from callingbot.timeutil import utcnow


def _enum(e: type[enum.Enum]) -> Enum:
    return Enum(e, native_enum=False, length=32, values_callable=lambda x: [m.value for m in x])


class EmpanelmentStatus(enum.StrEnum):
    """Where a distributor is in the empanelment funnel (with this AMC)."""

    NEW = "new"  # imported, never reached
    CONTACTED = "contacted"  # spoke at least once, no clear outcome yet
    INTERESTED = "interested"  # expressed interest
    LINK_SENT = "link_sent"  # empanelment link / form sent
    CALLBACK_SCHEDULED = "callback_scheduled"  # asked to be called back / RM meeting
    EMPANELLED = "empanelled"  # empanelment completed (updated by ops / CRM sync)
    ALREADY_EMPANELLED = "already_empanelled"  # was already empanelled before the call
    NOT_INTERESTED = "not_interested"
    WRONG_NUMBER = "wrong_number"
    DO_NOT_CALL = "do_not_call"  # opted out - never call again


class CampaignStatus(enum.StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class ContactState(enum.StrEnum):
    """Per-campaign dialling state for one distributor."""

    PENDING = "pending"  # eligible to be dialled when next_attempt_at <= now
    IN_PROGRESS = "in_progress"  # a call is currently active
    DONE = "done"  # reached a final outcome or exhausted attempts - do not dial again
    SKIPPED = "skipped"  # excluded (DNC, invalid number, ...)


class CallStatus(enum.StrEnum):
    QUEUED = "queued"
    INITIATED = "initiated"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    CANCELED = "canceled"
    VOICEMAIL = "voicemail"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_CALL_STATUSES


TERMINAL_CALL_STATUSES = frozenset(
    {
        CallStatus.COMPLETED,
        CallStatus.NO_ANSWER,
        CallStatus.BUSY,
        CallStatus.FAILED,
        CallStatus.CANCELED,
        CallStatus.VOICEMAIL,
    }
)


class CallOutcome(enum.StrEnum):
    """Business disposition of a connected call (set by the bot via the record_outcome tool)."""

    INTERESTED = "interested"
    LINK_SENT = "link_sent"
    CALLBACK_REQUESTED = "callback_requested"
    ALREADY_EMPANELLED = "already_empanelled"
    NOT_INTERESTED = "not_interested"
    WRONG_PERSON = "wrong_person"
    OPTED_OUT = "opted_out"
    TRANSFERRED = "transferred"
    VOICEMAIL = "voicemail"
    NO_OUTCOME = "no_outcome"  # call ended without a disposition (hang-up, silence, error)


class InterestLevel(enum.StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class TurnRole(enum.StrEnum):
    BOT = "bot"
    DISTRIBUTOR = "distributor"
    SYSTEM = "system"


class MessageChannel(enum.StrEnum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"


class MessageStatus(enum.StrEnum):
    QUEUED = "queued"  # stored in outbox, not handed to a provider (outbox mode)
    SENT = "sent"
    FAILED = "failed"


class CallbackStatus(enum.StrEnum):
    PENDING = "pending"
    DONE = "done"
    CANCELED = "canceled"


class FollowUpKind(enum.StrEnum):
    """What a Callback row asks the team to do."""

    CALLBACK = "callback"  # call back at scheduled_for (bot or RM)
    RM_REQUEST = "rm_request"  # partner asked for a relationship manager
    COMMISSION_QUERY = "commission_query"  # commission structure question for the team
    COLLATERAL_REQUEST = "collateral_request"  # single pagers / presentations to email
    EMAIL_ISSUE = "email_issue"  # empanelment email not received
    EMPANELMENT_HELP = "empanelment_help"  # needs help completing empanelment
    OTHER = "other"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Distributor(TimestampMixin, Base):
    """A mutual fund distributor (AMFI ARN holder)."""

    __tablename__ = "distributors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Canonical "ARN-12345". Optional: some lists (e.g. CRM exports) have no ARN; such rows are matched by phone.
    arn: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    firm_name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(20), index=True)  # E.164, e.g. +919876543210
    alt_phone: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100), index=True)
    state: Mapped[str | None] = mapped_column(String(100))
    pincode: Mapped[str | None] = mapped_column(String(10))
    euin: Mapped[str | None] = mapped_column(String(16))
    arn_valid_till: Mapped[date | None] = mapped_column(Date)
    preferred_language: Mapped[str | None] = mapped_column(String(16))  # BCP-47, e.g. "hi-IN"
    status: Mapped[EmpanelmentStatus] = mapped_column(
        _enum(EmpanelmentStatus), default=EmpanelmentStatus.NEW, index=True
    )
    do_not_call: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    dnc_reason: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str | None] = mapped_column(String(100))  # e.g. "amfi_export_2026-09", "crm"
    notes: Mapped[str | None] = mapped_column(Text)

    calls: Mapped[list[Call]] = relationship(back_populates="distributor", order_by="Call.id")
    contacts: Mapped[list[CampaignContact]] = relationship(back_populates="distributor")


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[CampaignStatus] = mapped_column(_enum(CampaignStatus), default=CampaignStatus.DRAFT)
    description: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    contacts: Mapped[list[CampaignContact]] = relationship(back_populates="campaign")


class CampaignContact(TimestampMixin, Base):
    """A distributor targeted by a campaign, with per-campaign retry bookkeeping."""

    __tablename__ = "campaign_contacts"
    __table_args__ = (UniqueConstraint("campaign_id", "distributor_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    distributor_id: Mapped[int] = mapped_column(ForeignKey("distributors.id"), index=True)
    state: Mapped[ContactState] = mapped_column(_enum(ContactState), default=ContactState.PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)  # None = ASAP
    final_outcome: Mapped[CallOutcome | None] = mapped_column(_enum(CallOutcome))

    campaign: Mapped[Campaign] = relationship(back_populates="contacts")
    distributor: Mapped[Distributor] = relationship(back_populates="contacts")


class Call(TimestampMixin, Base):
    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    distributor_id: Mapped[int] = mapped_column(ForeignKey("distributors.id"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("campaign_contacts.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))  # "twilio" | "exotel" | "simulator"
    provider_call_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[CallStatus] = mapped_column(_enum(CallStatus), default=CallStatus.QUEUED, index=True)
    outcome: Mapped[CallOutcome | None] = mapped_column(_enum(CallOutcome), index=True)
    interest_level: Mapped[InterestLevel | None] = mapped_column(_enum(InterestLevel))
    language: Mapped[str] = mapped_column(String(16), default="en-IN")
    answered_by: Mapped[str | None] = mapped_column(String(32))  # "human" | "machine" | ...
    answered_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    recording_url: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[str | None] = mapped_column(Text)  # notes for the RM, from record_outcome
    # Raw Claude conversation (list of Messages-API message dicts). Append-only: earlier entries
    # are never edited so thinking blocks and the prompt cache stay valid between turns.
    llm_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Engine scratch state that must survive between webhooks, e.g. operator notes to attach to
    # the next user message, link-send counter, end/transfer flags. Owned by callingbot.agent.engine.
    engine_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)  # distributor utterances handled
    no_input_count: Mapped[int] = mapped_column(Integer, default=0)
    # Action the bot decided on that the telephony layer must carry out after speaking:
    # "hangup" or "transfer". None = keep listening.
    pending_action: Mapped[str | None] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)

    distributor: Mapped[Distributor] = relationship(back_populates="calls")
    turns: Mapped[list[Turn]] = relationship(back_populates="call", order_by="Turn.id")


class Turn(Base):
    """Human-readable transcript line."""

    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id"), index=True)
    role: Mapped[TurnRole] = mapped_column(_enum(TurnRole))
    text: Mapped[str] = mapped_column(Text)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)  # compliance screen blocked/rewrote it
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    call: Mapped[Call] = relationship(back_populates="turns")


class Callback(TimestampMixin, Base):
    __tablename__ = "callbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    distributor_id: Mapped[int] = mapped_column(ForeignKey("distributors.id"), index=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"))
    kind: Mapped[FollowUpKind] = mapped_column(_enum(FollowUpKind), default=FollowUpKind.CALLBACK, index=True)
    # For CALLBACK: when to call. For other kinds: when the request was logged (handle ASAP).
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, index=True)
    with_rm: Mapped[bool] = mapped_column(Boolean, default=True)  # human RM vs. bot re-call
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[CallbackStatus] = mapped_column(_enum(CallbackStatus), default=CallbackStatus.PENDING)


class OutboundMessage(TimestampMixin, Base):
    __tablename__ = "outbound_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    distributor_id: Mapped[int | None] = mapped_column(ForeignKey("distributors.id"), index=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"))
    channel: Mapped[MessageChannel] = mapped_column(_enum(MessageChannel))
    destination: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[MessageStatus] = mapped_column(_enum(MessageStatus), default=MessageStatus.QUEUED)
    provider_message_id: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class DNCEntry(Base):
    """Internal do-not-call list (in addition to TRAI NCPR/DLT preference scrubbing)."""

    __tablename__ = "dnc_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    reason: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str | None] = mapped_column(String(100))  # "call_opt_out", "manual", "ncpr_scrub"...
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LinkClick(Base):
    __tablename__ = "link_clicks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    distributor_id: Mapped[int | None] = mapped_column(ForeignKey("distributors.id"), index=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEvent(Base):
    """Append-only audit trail (compliance evidence: disclosures played, opt-outs, flags, errors)."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"), index=True)
    distributor_id: Mapped[int | None] = mapped_column(ForeignKey("distributors.id"), index=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def audit(
    session, kind: str, *, call_id: int | None = None, distributor_id: int | None = None, **detail
) -> AuditEvent:
    """Add an audit event to the session (caller commits)."""
    ev = AuditEvent(kind=kind, call_id=call_id, distributor_id=distributor_id, detail=detail or None)
    session.add(ev)
    return ev

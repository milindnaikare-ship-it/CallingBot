"""Tools the voice agent can call, and their server-side handlers.

Design rules:

* **Strict schemas.** Every tool is ``"strict": True``: each object lists all of its properties in
  ``required`` with ``additionalProperties: false``, optional values are ``anyOf [..., null]``, and
  no unsupported keywords (lengths, ranges, patterns, formats) are used. Handlers still validate
  every input themselves - strict mode is a guarantee from one provider, and the offline
  :class:`~callingbot.agent.demo_llm.DemoLLM` or a future model may not give it.
* **Deterministic definitions.** :func:`build_tool_definitions` returns the same JSON for the
  same knowledge base, so tools stay inside the cached prompt prefix.
* **Never raise.** :func:`execute_tool` turns every problem into ``ToolOutcome(is_error=True)``
  with a message the model can act on; a crashing tool must not drop a live call.
* **PII minimisation.** The model never sees the distributor's phone number or email. Tools
  resolve "the number/email on file" here and return masked values only.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from callingbot import compliance, funnel, links
from callingbot.agent.prompts import spoken_datetime
from callingbot.knowledge import KnowledgeBase
from callingbot.messaging import Messenger
from callingbot.models import (
    Call,
    Callback,
    CallbackStatus,
    CallOutcome,
    Distributor,
    EmpanelmentStatus,
    FollowUpKind,
    InterestLevel,
    MessageChannel,
    MessageStatus,
    OutboundMessage,
    audit,
)
from callingbot.phone import mask_phone, normalize_indian_mobile
from callingbot.settings import Settings
from callingbot.timeutil import to_local, to_utc_naive

log = logging.getLogger(__name__)

MAX_LINK_SENDS_PER_CALL = 3
# Guards against a misheard year ("2027") booking a callback nobody will remember.
MAX_CALLBACK_DAYS_AHEAD = 60
CALLBACK_SLOT_MINUTES = 30

OUTCOME_CHOICES = (
    "interested",
    "link_sent",
    "callback_requested",
    "already_empanelled",
    "not_interested",
    "wrong_person",
    "no_outcome",
)
INTEREST_CHOICES = ("hot", "warm", "cold")
REQUEST_KINDS = (
    FollowUpKind.RM_REQUEST.value,
    FollowUpKind.COMMISSION_QUERY.value,
    FollowUpKind.COLLATERAL_REQUEST.value,
    FollowUpKind.EMAIL_ISSUE.value,
    FollowUpKind.EMPANELMENT_HELP.value,
    FollowUpKind.OTHER.value,
)
CHANNEL_CHOICES = ("sms", "whatsapp", "email")

# Outcomes set by an action that has already happened on the call; nothing the model records
# afterwards may replace them.
_LOCKED_OUTCOMES = frozenset({CallOutcome.OPTED_OUT, CallOutcome.TRANSFERRED})
# Progress outcomes, weakest first. A weaker one never replaces a stronger one.
_PROGRESS_RANK: dict[CallOutcome | None, int] = {
    None: 0,
    CallOutcome.NO_OUTCOME: 0,
    CallOutcome.INTERESTED: 1,
    CallOutcome.CALLBACK_REQUESTED: 2,
    CallOutcome.LINK_SENT: 3,
}

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")


# ---------------------------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------------------------


@dataclass
class ToolContext:
    session: Session
    call: Call
    distributor: Distributor
    kb: KnowledgeBase
    settings: Settings
    messenger: Messenger
    now_utc: datetime


@dataclass
class ToolOutcome:
    content: str  # JSON string returned to the model as tool_result content
    is_error: bool = False
    end_call: bool = False  # hang up after speaking this turn's reply
    transfer_to: str | None = None


# ---------------------------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------------------------


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": " ".join(description.split()),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


def build_tool_definitions(kb: KnowledgeBase | None = None) -> list[dict[str, Any]]:
    """Messages API tool definitions.

    With ``kb``, language parameters are an enum of the knowledge base's language codes; without
    it they are free strings validated by the handlers. Same input, same output - byte for byte.
    """
    lang_desc = "BCP-47 code of one of the supported languages listed in the system prompt, e.g. 'hi-IN'."
    language: dict[str, Any] = {"type": "string", "description": lang_desc}
    if kb is not None:
        language = {
            "type": "string",
            "enum": [lang.code for lang in kb.amc.languages],
            "description": lang_desc,
        }

    return [
        _tool(
            "verify_arn",
            """Check an ARN (AMFI Registration Number) the distributor has just stated against the ARN
            we hold for them. Call it only when they state their ARN, for example because they say our
            record is wrong; never ask for the ARN just to use this tool. Returns whether the format is
            valid and whether it matches our record, never anyone else's details.""",
            {
                "arn": {
                    "type": "string",
                    "description": "The ARN as stated, e.g. 'ARN-123456', 'ARN 123456' or just the digits. "
                    "Convert spoken number words to digits.",
                }
            },
        ),
        _tool(
            "update_distributor_details",
            """Save contact details or preferences the distributor gives you during the call: an email
            address, a preferred language for future calls, an alternate Indian mobile number, or a short
            note for their relationship manager. Pass null for anything they did not give. Never use it
            for PAN, bank, Aadhaar or other sensitive data, and do not ask for details just to fill it.""",
            {
                "email": _nullable(
                    {"type": "string", "description": "Email address exactly as the distributor gave it."}
                ),
                "preferred_language": _nullable(language),
                "alt_phone": _nullable(
                    {"type": "string", "description": "Alternate Indian mobile number, digits only."}
                ),
                "notes": _nullable(
                    {"type": "string", "description": "Short factual note for the relationship manager."}
                ),
            },
        ),
        _tool(
            "send_empanelment_link",
            """Send the distributor their personal empanelment link. Call it only after they agree to
            receive it. 'sms' and 'whatsapp' go to the mobile number of this call; 'email' goes to the
            address in 'email' or, when that is null, to the email on file. Never read the link aloud. At
            most three sends per call; if a channel fails, offer another one.""",
            {
                "channel": {"type": "string", "enum": list(CHANNEL_CHOICES)},
                "email": _nullable(
                    {
                        "type": "string",
                        "description": "Email address the distributor just gave, only for channel 'email'. "
                        "Null to use the email on file, and for SMS or WhatsApp.",
                    }
                ),
            },
        ),
        _tool(
            "schedule_callback",
            """Schedule a call back at a time the distributor chooses: from a human relationship manager
            (with_rm true - use this for questions you cannot answer) or from you, the virtual assistant
            (with_rm false). The time must be in the future and inside calling hours; otherwise the tool
            returns an error with the next available slot, which you can propose. Do not use it when the
            person wants no more calls - use opt_out.""",
            {
                "when_local": {
                    "type": "string",
                    "description": "Local India time (IST) as YYYY-MM-DD HH:MM, e.g. '2026-10-14 11:30'.",
                },
                "with_rm": {
                    "type": "boolean",
                    "description": "True for a human relationship manager, false for the virtual assistant.",
                },
                "notes": {
                    "type": "string",
                    "description": "What the callback is about, for whoever calls back. Empty string if nothing.",
                },
            },
        ),
        _tool(
            "set_language",
            """Switch the call's speech recognition and voice to another supported language when the
            distributor speaks it or asks for it. From your next sentence, write in that language (Hindi
            in Devanagari script). Do not call it if the call is already in that language.""",
            {"language": language},
        ),
        _tool(
            "transfer_to_human",
            """Transfer the call to a human relationship manager right now, when the distributor asks to
            speak to a person. Works only during calling hours when a transfer desk is configured; if it
            returns available=false, offer a callback with schedule_callback instead. In the same
            response, tell them you are connecting them.""",
            {"reason": {"type": "string", "description": "Why the distributor wants a person, briefly."}},
        ),
        _tool(
            "opt_out",
            """Honour a request not to be called again: puts the number on the do-not-call list and ends
            the call after your reply. Call it immediately when the person says they do not want calls,
            asks to be removed or mentions DND; never try to persuade them. In the same response,
            apologise and confirm in one sentence that they will not be called again. Not for "call me
            later" - use schedule_callback for that.""",
            {"reason": {"type": "string", "description": "The person's request in a few words."}},
        ),
        _tool(
            "log_request",
            """Note a request for our team to follow up on, when the distributor asks for something that
            has no fixed time: a relationship manager (rm_request), the commission or brokerage structure
            (commission_query), single pagers, presentations or other marketing collateral
            (collateral_request), an empanelment email they have not received (email_issue), help with
            the empanelment form (empanelment_help), or anything else the team must handle (other). In
            the same response, give the matching approved response to the distributor. For a call back
            at a specific time use schedule_callback instead.""",
            {
                "kind": {"type": "string", "enum": list(REQUEST_KINDS)},
                "details": {
                    "type": "string",
                    "description": "What the team should do, in one or two factual sentences. No phone "
                    "numbers or email addresses.",
                },
            },
        ),
        _tool(
            "record_outcome",
            """Record the call's disposition and a summary for the relationship manager. Call it once
            before end_call on every call (not needed after opt_out, which records its own outcome). Be
            honest: 'link_sent' only if send_empanelment_link succeeded on this call, 'callback_requested'
            if a callback was scheduled, 'interested' if they want to proceed but nothing was sent or
            scheduled, 'no_outcome' if the call ended without a clear answer.""",
            {
                "outcome": {"type": "string", "enum": list(OUTCOME_CHOICES)},
                "interest_level": _nullable({"type": "string", "enum": list(INTEREST_CHOICES)}),
                "summary_for_rm": {
                    "type": "string",
                    "description": "Two or three factual sentences for the relationship manager: what was "
                    "discussed, questions asked and agreed next steps. No phone numbers or email addresses.",
                },
                "objections": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concerns or objections the distributor raised, in a few words each. "
                    "Empty if none.",
                },
            },
        ),
        _tool(
            "end_call",
            """Hang up once the reply you write in this same response has been spoken. Use it only after
            a polite goodbye sentence and after record_outcome. Not needed after opt_out, which ends the
            call itself.""",
            {"reason": {"type": "string", "description": "Why the call is ending, briefly."}},
        ),
    ]


TOOL_DEFINITIONS: list[dict[str, Any]] = build_tool_definitions()
TOOL_NAMES: tuple[str, ...] = tuple(t["name"] for t in TOOL_DEFINITIONS)
_SCHEMAS: dict[str, dict[str, Any]] = {t["name"]: t["input_schema"] for t in TOOL_DEFINITIONS}


# ---------------------------------------------------------------------------------------------
# Input validation (against the published schemas)
# ---------------------------------------------------------------------------------------------


def _accepts_null(schema: dict[str, Any]) -> bool:
    if schema.get("type") == "null":
        return True
    return any(_accepts_null(s) for s in schema.get("anyOf", ()))


def _validate(value: Any, schema: dict[str, Any], path: str) -> str | None:
    """Return a human-readable problem, or None if ``value`` matches ``schema``."""
    if "anyOf" in schema:
        problems = [_validate(value, s, path) for s in schema["anyOf"]]
        if any(p is None for p in problems):
            return None
        return next(p for p in problems if p)
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return f"{path} must be an object"
        props: dict[str, Any] = schema.get("properties", {})
        extra = sorted(k for k in value if k not in props)
        if extra and schema.get("additionalProperties") is False:
            return f"unexpected field(s): {', '.join(extra)}"
        # A missing nullable field is read as null: lenient towards clients without strict mode,
        # while a missing mandatory field is still an error.
        missing = [k for k in schema.get("required", []) if k not in value and not _accepts_null(props[k])]
        if missing:
            return f"missing required field(s): {', '.join(missing)}"
        for key, sub in props.items():
            if key in value:
                problem = _validate(value[key], sub, key)
                if problem:
                    return problem
        return None
    if kind == "string" and not isinstance(value, str):
        return f"{path} must be a string"
    if kind == "boolean" and not isinstance(value, bool):
        return f"{path} must be true or false"
    if kind == "null" and value is not None:
        return f"{path} must be null"
    if kind == "array":
        if not isinstance(value, list):
            return f"{path} must be an array"
        for i, item in enumerate(value):
            problem = _validate(item, schema.get("items", {}), f"{path}[{i}]")
            if problem:
                return problem
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of: {', '.join(map(str, schema['enum']))}"
    return None


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _ok(**data: Any) -> ToolOutcome:
    return ToolOutcome(content=_json(data))


def _error(message: str, **data: Any) -> ToolOutcome:
    return ToolOutcome(content=_json({"error": message, **data}), is_error=True)


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def _update_state(call: Call, **updates: Any) -> None:
    # SQLAlchemy does not track in-place changes to JSON columns: always assign a new dict.
    call.engine_state = {**(call.engine_state or {}), **updates}


def _append_note(distributor: Distributor, note: str) -> None:
    existing = (distributor.notes or "").rstrip()
    if note in existing.splitlines():
        return
    distributor.notes = f"{existing}\n{note}" if existing else note


def mask_email(email: str | None) -> str:
    """``ravi.k@gmail.com -> r***@gmail.com`` (what the model and logs get to see)."""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def normalize_email(raw: str | None) -> str | None:
    """Validated, lower-cased email, also accepting the spoken form ``ravi at gmail dot com``."""
    text = _clean(raw)
    if not text:
        return None
    text = text.lower()
    if "@" not in text:
        text = re.sub(r"\s+at\s+", "@", text)
    text = re.sub(r"\s+dot\s+", ".", text).replace(" ", "")
    return text if _EMAIL_RE.fullmatch(text) else None


def normalize_arn_input(raw: str) -> tuple[str | None, bool]:
    """``("ARN-<digits>", valid_format)`` from what the distributor said.

    Accepts "ARN 12345", "arn-012345", "12345" and the like: everything but the digits is dropped.
    Leading zeros are kept (they are part of what was said); :func:`_same_arn` ignores them.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None, False
    return f"ARN-{digits}", len(digits) <= 7 and int(digits) > 0


def _same_arn(a: str | None, b: str | None) -> bool:
    def key(x: str | None) -> str:
        return re.sub(r"\D", "", x or "").lstrip("0")

    return bool(key(a)) and key(a) == key(b)


def _set_progress_outcome(call: Call, new: CallOutcome) -> None:
    """Apply an outcome implied by an action (link sent, callback booked) unless a stronger one exists."""
    current = call.outcome
    if current in _LOCKED_OUTCOMES:
        return
    if current not in _PROGRESS_RANK or _PROGRESS_RANK[current] < _PROGRESS_RANK[new]:
        call.outcome = new


def _round_up_local(dt_local: datetime, minutes: int) -> datetime:
    dt_local = dt_local.replace(second=0, microsecond=0) + (
        timedelta(minutes=1) if dt_local.second or dt_local.microsecond else timedelta(0)
    )
    remainder = dt_local.minute % minutes
    return dt_local + timedelta(minutes=minutes - remainder) if remainder else dt_local


def next_callback_slot(ctx: ToolContext, after_utc: datetime) -> datetime | None:
    """Earliest speakable slot (rounded to the half hour, IST) inside the calling window, naive UTC."""
    policy, tz = ctx.kb.campaign, ctx.settings.timezone
    try:
        start = compliance.next_window_start(policy, after_utc, tz)
        slot = to_utc_naive(_round_up_local(to_local(start, tz), CALLBACK_SLOT_MINUTES))
        if not compliance.check_calling_window(policy, slot, tz).allowed:
            slot = compliance.next_window_start(policy, slot, tz)
        return slot
    except ValueError:
        log.warning("No calling window found after %s; check the campaign policy", after_utc)
        return None


def _slot_fields(ctx: ToolContext, slot_utc: datetime | None) -> dict[str, Any]:
    if slot_utc is None:
        return {"suggestion": "Offer a callback from a relationship manager without a fixed time."}
    local = to_local(slot_utc, ctx.settings.timezone)
    return {
        "next_available_spoken": spoken_datetime(local),
        "next_available_local": f"{local:%Y-%m-%d %H:%M}",
    }


def _parse_local(text: str) -> datetime | None:
    text = " ".join(text.replace("T", " ").split())
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------------------------


def _verify_arn(ctx: ToolContext, inp: dict[str, Any]) -> ToolOutcome:
    arn, valid = normalize_arn_input(inp["arn"])
    if arn is None:
        return _error("No digits found in the ARN. Ask the distributor to repeat it digit by digit.")
    matches = valid and _same_arn(arn, ctx.distributor.arn)
    if valid and not matches:
        # Keep what they said for ops to reconcile; the model learns nothing about the record.
        _append_note(ctx.distributor, f"Distributor stated ARN {arn} on call {ctx.call.id}")
    result: dict[str, Any] = {"arn": arn, "valid_format": valid, "matches_record": matches}
    if not valid:
        result["hint"] = "That does not look like a valid ARN. Ask them to repeat it digit by digit."
    elif not matches:
        result["hint"] = "Thank them; the relationship manager will check and update the record."
    return _ok(**result)


def _update_details(ctx: ToolContext, inp: dict[str, Any]) -> ToolOutcome:
    d = ctx.distributor
    updated: list[str] = []
    rejected: dict[str, str] = {}

    if inp.get("email") is not None:
        email = normalize_email(inp["email"])
        if email:
            d.email = email
            updated.append("email")
        else:
            rejected["email"] = "not a valid email address - ask them to spell it once more"

    if inp.get("preferred_language") is not None:
        code = _clean(inp["preferred_language"])
        codes = [lang.code for lang in ctx.kb.amc.languages]
        if code in codes:
            d.preferred_language = code
            updated.append("preferred_language")
        else:
            rejected["preferred_language"] = f"not supported; supported codes: {', '.join(codes)}"

    if inp.get("alt_phone") is not None:
        phone = normalize_indian_mobile(inp["alt_phone"])
        if phone and phone == d.phone:
            rejected["alt_phone"] = "same as the number on this call"
        elif phone:
            d.alt_phone = phone
            updated.append("alt_phone")
        else:
            rejected["alt_phone"] = "not a valid 10-digit Indian mobile number"

    if inp.get("notes") is not None:
        note = _clean(inp["notes"])
        if note:
            _append_note(d, f"Call {ctx.call.id}: {note[:500]}")
            updated.append("notes")
        else:
            rejected["notes"] = "empty note"

    if not updated and not rejected:
        return _error("No details given. Pass at least one non-null field.")
    result: dict[str, Any] = {"updated": updated, "rejected": rejected}
    if "email" in updated:
        result["email_on_file"] = mask_email(d.email)
    if "alt_phone" in updated:
        result["alt_phone_on_file"] = mask_phone(d.alt_phone)
    outcome = _ok(**result)
    outcome.is_error = not updated
    return outcome


def _send_link(ctx: ToolContext, inp: dict[str, Any]) -> ToolOutcome:
    call, d, kb = ctx.call, ctx.distributor, ctx.kb
    channel = MessageChannel(inp["channel"])
    if call.outcome == CallOutcome.OPTED_OUT:
        return _error("The distributor has opted out; do not send anything.")
    sends = int((call.engine_state or {}).get("link_sends", 0))
    if sends >= MAX_LINK_SENDS_PER_CALL:
        return _error(
            f"The link has already been sent {sends} times on this call; do not send it again. "
            "Offer to have the relationship manager share it instead."
        )

    subject: str | None = None
    if channel == MessageChannel.EMAIL:
        given = inp.get("email")
        if given is not None and _clean(given):
            email = normalize_email(given)
            if email is None:
                return _error(
                    "That email address is not valid. Ask them to spell it once more, or offer SMS."
                )
            d.email = email
        if not d.email:
            return _error("No email address on file. Ask for their email address, or offer SMS or WhatsApp.")
        destination = d.email
        masked = mask_email(destination)
        subject = f"Empanelment with {kb.amc.name}"
    else:
        destination = d.phone
        masked = mask_phone(destination)

    link = links.tracked_link(ctx.settings, d.id, call.id)
    body = (
        f"Dear {d.name}, thank you for speaking with {kb.amc.bot_name} from {kb.amc.short_name}. "
        f"Complete your empanelment here: {link} ."
    )
    if kb.amc.distributor_helpline:
        body += f" For help call {kb.amc.distributor_helpline}."
    template_vars = {"name": d.name, "link": link} if channel == MessageChannel.WHATSAPP else None

    _update_state(call, link_sends=sends + 1)
    row = ctx.messenger.send(
        ctx.session,
        channel=channel,
        to=destination,
        body=body,
        subject=subject,
        link=link,
        distributor_id=d.id,
        call_id=call.id,
        template_vars=template_vars,
    )
    if row.status == MessageStatus.FAILED:
        others = [c for c in CHANNEL_CHOICES if c != channel.value]
        return _error(
            f"Could not send the link by {channel.value}. Apologise and offer {' or '.join(others)} instead.",
            sent=False,
            channel=channel.value,
        )

    funnel.advance_status(d, EmpanelmentStatus.LINK_SENT)
    _set_progress_outcome(call, CallOutcome.LINK_SENT)
    audit(
        ctx.session,
        "link_sent",
        call_id=call.id,
        distributor_id=d.id,
        channel=channel.value,
        message_id=row.id,
        destination=masked,
        status=row.status.value,
    )
    return _ok(sent=True, channel=channel.value, destination=masked)


def _schedule_callback(ctx: ToolContext, inp: dict[str, Any]) -> ToolOutcome:
    call, d, tz = ctx.call, ctx.distributor, ctx.settings.timezone
    if call.outcome == CallOutcome.OPTED_OUT:
        return _error("The distributor has opted out; do not schedule a callback.")
    local = _parse_local(inp["when_local"])
    if local is None:
        return _error("when_local must be a local IST time in the format YYYY-MM-DD HH:MM.")
    when_utc = to_utc_naive(local, tz)

    if when_utc <= ctx.now_utc:
        slot = next_callback_slot(ctx, ctx.now_utc + timedelta(hours=1))
        return _error(
            "That time has already passed. Propose the next available slot.", **_slot_fields(ctx, slot)
        )
    if when_utc > ctx.now_utc + timedelta(days=MAX_CALLBACK_DAYS_AHEAD):
        return _error(
            f"That is more than {MAX_CALLBACK_DAYS_AHEAD} days away. Check the date with the distributor."
        )
    decision = compliance.check_calling_window(ctx.kb.campaign, when_utc, tz)
    if not decision.allowed:
        slot = next_callback_slot(ctx, when_utc)
        return _error(
            f"That time is outside our calling hours ({decision.reason}). Propose the next available slot.",
            **_slot_fields(ctx, slot),
        )

    with_rm = bool(inp["with_rm"])
    notes = _clean(inp["notes"])
    # A second booking on the same call is a reschedule, not an extra callback.
    existing = ctx.session.scalar(
        select(Callback).where(
            Callback.call_id == call.id,
            Callback.kind == FollowUpKind.CALLBACK,
            Callback.status == CallbackStatus.PENDING,
        )
    )
    if existing is not None:
        existing.scheduled_for, existing.with_rm, existing.notes = when_utc, with_rm, notes
        callback = existing
    else:
        callback = Callback(
            distributor_id=d.id, call_id=call.id, scheduled_for=when_utc, with_rm=with_rm, notes=notes
        )
        ctx.session.add(callback)
    ctx.session.flush()

    funnel.advance_status(d, EmpanelmentStatus.CALLBACK_SCHEDULED)
    _set_progress_outcome(call, CallOutcome.CALLBACK_REQUESTED)
    audit(
        ctx.session,
        "callback_scheduled",
        call_id=call.id,
        distributor_id=d.id,
        callback_id=callback.id,
        scheduled_for=when_utc.isoformat(),
        with_rm=with_rm,
        rescheduled=existing is not None,
    )
    return _ok(scheduled=True, when_spoken=spoken_datetime(to_local(when_utc, tz)), with_rm=with_rm)


def _log_request(ctx: ToolContext, inp: dict[str, Any]) -> ToolOutcome:
    call, d = ctx.call, ctx.distributor
    kind = FollowUpKind(inp["kind"])
    details = _clean(inp["details"])
    # One open request per kind per call: repeating the same ask just refreshes the note.
    existing = ctx.session.scalar(
        select(Callback).where(
            Callback.call_id == call.id, Callback.kind == kind, Callback.status == CallbackStatus.PENDING
        )
    )
    if existing is not None:
        existing.notes = details
        request = existing
    else:
        request = Callback(
            distributor_id=d.id,
            call_id=call.id,
            kind=kind,
            scheduled_for=ctx.now_utc,  # handle as soon as possible
            with_rm=True,
            notes=details,
        )
        ctx.session.add(request)
    ctx.session.flush()
    # A follow-up request signals interest, but must not overwrite a clearer disposition
    # (e.g. already_empanelled partners asking for collateral).
    if call.outcome in (None, CallOutcome.NO_OUTCOME):
        call.outcome = CallOutcome.INTERESTED
    funnel.advance_status(d, EmpanelmentStatus.INTERESTED)
    audit(
        ctx.session,
        "request_logged",
        call_id=call.id,
        distributor_id=d.id,
        request_kind=kind.value,
        request_id=request.id,
    )
    return _ok(logged=True, kind=kind.value, handled_by=ctx.kb.amc.rm_team_description)


def _set_language(ctx: ToolContext, inp: dict[str, Any]) -> ToolOutcome:
    code = _clean(inp["language"])
    by_code = {lang.code: lang for lang in ctx.kb.amc.languages}
    if code not in by_code:
        return _error(f"Language {code!r} is not supported. Supported: {', '.join(by_code)}.")
    ctx.call.language = code
    return _ok(language=code, name=by_code[code].name)


def _transfer(ctx: ToolContext, inp: dict[str, Any]) -> ToolOutcome:
    number = ctx.settings.rm_transfer_number
    window = compliance.check_calling_window(ctx.kb.campaign, ctx.now_utc, ctx.settings.timezone)
    if not number or not window.allowed or ctx.call.outcome == CallOutcome.OPTED_OUT:
        return _ok(available=False, suggestion="offer a callback with schedule_callback")
    ctx.call.outcome = CallOutcome.TRANSFERRED
    audit(
        ctx.session,
        "transfer",
        call_id=ctx.call.id,
        distributor_id=ctx.distributor.id,
        reason=_clean(inp["reason"]),
        to=mask_phone(number),
    )
    return ToolOutcome(content=_json({"available": True, "transferring": True}), transfer_to=number)


def _opt_out(ctx: ToolContext, inp: dict[str, Any]) -> ToolOutcome:
    d, call = ctx.distributor, ctx.call
    reason = _clean(inp["reason"]) or "Asked not to be called again"
    # Honour the request for every number we hold for this person.
    for phone in dict.fromkeys(p for p in (d.phone, d.alt_phone) if p):
        compliance.add_to_dnc(ctx.session, phone, reason=reason, source="call_opt_out")
    funnel.advance_status(d, EmpanelmentStatus.DO_NOT_CALL)
    call.outcome = CallOutcome.OPTED_OUT
    audit(ctx.session, "opt_out", call_id=call.id, distributor_id=d.id, reason=reason)
    return ToolOutcome(
        content=_json(
            {
                "opted_out": True,
                "instruction": "Confirm in one short sentence that they will not be called again, then stop.",
            }
        ),
        end_call=True,
    )


def _delivered_links(ctx: ToolContext) -> int:
    return ctx.session.scalar(
        select(func.count(OutboundMessage.id)).where(
            OutboundMessage.call_id == ctx.call.id, OutboundMessage.status != MessageStatus.FAILED
        )
    )


def _record_outcome(ctx: ToolContext, inp: dict[str, Any]) -> ToolOutcome:
    call = ctx.call
    requested = CallOutcome(inp["outcome"])
    note: str | None = None
    if requested == CallOutcome.LINK_SENT and not _delivered_links(ctx):
        requested = CallOutcome.INTERESTED
        note = "No link was sent on this call, so the outcome was recorded as interested."

    current = call.outcome
    if current in _LOCKED_OUTCOMES:
        final = current
        note = f"The call outcome stays {current.value}."
    elif (
        requested in _PROGRESS_RANK
        and current in _PROGRESS_RANK
        and _PROGRESS_RANK[current] > _PROGRESS_RANK[requested]
    ):
        final = current
        note = f"Kept the stronger outcome {current.value}."
    else:
        final = requested
    call.outcome = final

    level = inp.get("interest_level")
    call.interest_level = InterestLevel(level) if level else None
    summary = _clean(inp["summary_for_rm"]) or ""
    objections = [o for o in (_clean(x) for x in inp.get("objections") or []) if o]
    if objections:
        summary = f"{summary}\nObjections: {'; '.join(objections)}".strip()
    call.summary = summary or None

    result: dict[str, Any] = {"recorded": True, "outcome": final.value}
    if note:
        result["note"] = note
    return _ok(**result)


def _end_call(ctx: ToolContext, inp: dict[str, Any]) -> ToolOutcome:
    return ToolOutcome(content=_json({"ending": True}), end_call=True)


_HANDLERS = {
    "verify_arn": _verify_arn,
    "update_distributor_details": _update_details,
    "send_empanelment_link": _send_link,
    "schedule_callback": _schedule_callback,
    "set_language": _set_language,
    "transfer_to_human": _transfer,
    "log_request": _log_request,
    "opt_out": _opt_out,
    "record_outcome": _record_outcome,
    "end_call": _end_call,
}


def execute_tool(ctx: ToolContext, name: str, tool_input: dict) -> ToolOutcome:
    """Run one tool call. Never raises: problems come back as ``is_error=True`` outcomes."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"Unknown tool {name!r}. Available tools: {', '.join(TOOL_NAMES)}.")
    problem = _validate(tool_input, _SCHEMAS[name], "input")
    if problem:
        return _error(f"Invalid input for {name}: {problem}.")
    try:
        return handler(ctx, copy.deepcopy(tool_input))
    except Exception:  # a tool bug must never drop a live call
        log.exception("Tool %s failed on call %s", name, getattr(ctx.call, "id", None))
        return _error(
            f"{name} failed because of an internal problem. Do not retry it; offer a callback from a "
            "relationship manager instead."
        )

"""Admin dashboard: stats, distributors, CSV import, campaigns, calls, callbacks, outbox, exports.

Server-rendered Jinja2 pages (autoescaped, no external CDNs) behind HTTP Basic auth; every
state-changing request also passes the same-origin CSRF check (see :mod:`callingbot.web.deps`).
Forms use POST-redirect-GET (303) with a one-shot flash message, except the CSV import, which
renders its report directly.

Privacy (DPDP minimisation): list views show masked phone numbers and e-mail addresses; full
contact details appear only on the distributor detail page (and in the leads export, whose
purpose is RM follow-up).

Compliance guards in the manual actions:

* Status changes are direct assignments (ops may record EMPANELLED etc.) audited as
  ``manual_status_change``. Setting DO_NOT_CALL goes through :func:`compliance.add_to_dnc`.
  An opt-out is never reversed from here: a do-not-call distributor's status cannot be changed.
* "Add to DNC" lists every number held for the distributor (source ``manual``) and cancels their
  pending callbacks, so no one calls a person who asked not to be called.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from contextlib import closing
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, joinedload
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from callingbot import compliance, db, funnel
from callingbot.agent.tools import mask_email
from callingbot.cli import LEAD_COLUMNS, LEAD_STATUSES
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
    Distributor,
    EmpanelmentStatus,
    MessageChannel,
    MessageStatus,
    OutboundMessage,
    Turn,
    audit,
)
from callingbot.phone import mask_phone
from callingbot.services.dialer import add_distributors_to_campaign, dial_due_contacts, reap_stale_calls
from callingbot.services.distributors import import_distributors_csv
from callingbot.services.reporting import campaign_stats
from callingbot.timeutil import to_local
from callingbot.web.deps import db_session, read_form, redirect_with_flash, render, require_admin

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)], tags=["admin"])

AdminUser = Annotated[str, Depends(require_admin)]
DBSession = Annotated[Session, Depends(db_session)]

PAGE_SIZE = 50
RECENT_CALLS = 20
UPCOMING_CALLBACKS = 20
DETAIL_ROWS = 200
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_MAX_NOTES_CHARS = 10_000
_MAX_REASON_CHARS = 200


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def _clean(value: str | None, limit: int | None = None) -> str | None:
    text = " ".join((value or "").split())
    if limit:
        text = text[:limit]
    return text or None


def _enum_or_none(enum_cls, raw: str | None):
    try:
        return enum_cls(raw) if raw else None
    except ValueError:
        return None


def _get_or_404(session: Session, model, object_id: int):
    obj = session.get(model, object_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} {object_id} not found")
    return obj


def _safe_next(value: str | None, default: str) -> str:
    # Only same-site relative paths: never an open redirect to another host ("//evil", "https://").
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return default


def _like(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _pager(request: Request, total: int, page: int) -> dict[str, Any]:
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(1, page), pages)

    def url(n: int) -> str:
        params = dict(request.query_params)
        params["page"] = str(n)
        return f"{request.url.path}?{urlencode(params)}"

    return {
        "page": page,
        "pages": pages,
        "total": total,
        "prev_url": url(page - 1) if page > 1 else None,
        "next_url": url(page + 1) if page < pages else None,
    }


def _paginate(session: Session, stmt, request: Request, page: int) -> tuple[list, dict[str, Any]]:
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    pager = _pager(request, total, page)
    rows = session.scalars(stmt.limit(PAGE_SIZE).offset((pager["page"] - 1) * PAGE_SIZE)).unique().all()
    return list(rows), pager


def _distributors_by_id(session: Session, ids) -> dict[int, Distributor]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {d.id: d for d in session.scalars(select(Distributor).where(Distributor.id.in_(ids)))}


def mask_destination(channel: MessageChannel | str, destination: str | None) -> str:
    if not destination:
        return ""
    if MessageChannel(channel) == MessageChannel.EMAIL:
        return mask_email(destination)
    return mask_phone(destination)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None  # naive UTC in the DB


def _value(enum_value) -> str | None:
    return None if enum_value is None else str(getattr(enum_value, "value", enum_value))


def _now(request: Request) -> datetime:
    return request.app.state.clock()


async def _post(request: Request, handler, *args):
    """Read the form (after auth/CSRF), then run the blocking handler in the threadpool."""
    form = await read_form(request)
    return await run_in_threadpool(handler, request, form, *args)


# ---------------------------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: DBSession):
    state = request.app.state
    active = session.scalars(
        select(Campaign).where(Campaign.status == CampaignStatus.ACTIVE).order_by(Campaign.id)
    ).all()
    recent_calls = session.scalars(
        select(Call).options(joinedload(Call.distributor)).order_by(Call.id.desc()).limit(RECENT_CALLS)
    ).all()
    upcoming = session.scalars(
        select(Callback)
        .where(Callback.status == CallbackStatus.PENDING)
        .order_by(Callback.scheduled_for, Callback.id)
        .limit(UPCOMING_CALLBACKS)
    ).all()
    stats = campaign_stats(session)
    return render(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "funnel_max": max([n for _, n in stats["funnel"]] + [1]),
            "campaign_stats": [(c, campaign_stats(session, c.id)) for c in active],
            "recent_calls": recent_calls,
            "upcoming": upcoming,
            "callback_distributors": _distributors_by_id(session, (cb.distributor_id for cb in upcoming)),
            "llm_model": getattr(state.llm, "model", "unknown"),
            "provider_name": state.provider.name,
            "kb": state.kb,
        },
    )


# ---------------------------------------------------------------------------------------------
# Distributors
# ---------------------------------------------------------------------------------------------


@router.get("/distributors", response_class=HTMLResponse)
def distributors_list(
    request: Request, session: DBSession, status: str | None = None, q: str | None = None, page: int = 1
):
    stmt = select(Distributor)
    status_filter = _enum_or_none(EmpanelmentStatus, status)
    if status_filter is not None:
        stmt = stmt.where(Distributor.status == status_filter)
    query = _clean(q, 100)
    if query:
        like = _like(query)
        stmt = stmt.where(
            or_(
                Distributor.name.ilike(like, escape="\\"),
                Distributor.arn.ilike(like, escape="\\"),
                Distributor.city.ilike(like, escape="\\"),
                Distributor.firm_name.ilike(like, escape="\\"),
            )
        )
    rows, pager = _paginate(session, stmt.order_by(Distributor.name, Distributor.id), request, page)
    return render(
        request,
        "distributors.html",
        {
            "distributors": rows,
            "pager": pager,
            "status": status_filter.value if status_filter else "",
            "q": query or "",
            "statuses": list(EmpanelmentStatus),
        },
    )


@router.get("/distributors/{distributor_id}", response_class=HTMLResponse)
def distributor_detail(request: Request, distributor_id: int, session: DBSession):
    d = _get_or_404(session, Distributor, distributor_id)
    calls = session.scalars(
        select(Call).where(Call.distributor_id == d.id).order_by(Call.id.desc()).limit(DETAIL_ROWS)
    ).all()
    messages = session.scalars(
        select(OutboundMessage)
        .where(OutboundMessage.distributor_id == d.id)
        .order_by(OutboundMessage.id.desc())
        .limit(DETAIL_ROWS)
    ).all()
    callbacks = session.scalars(
        select(Callback).where(Callback.distributor_id == d.id).order_by(Callback.id.desc()).limit(DETAIL_ROWS)
    ).all()
    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.distributor_id == d.id)
        .order_by(AuditEvent.id.desc())
        .limit(DETAIL_ROWS)
    ).all()
    contacts = session.scalars(
        select(CampaignContact)
        .options(joinedload(CampaignContact.campaign))
        .where(CampaignContact.distributor_id == d.id)
        .order_by(CampaignContact.id)
    ).all()
    return render(
        request,
        "distributor_detail.html",
        {
            "d": d,
            "calls": calls,
            "messages": messages,
            "callbacks": callbacks,
            "events": events,
            "contacts": contacts,
            "on_dnc_list": d.do_not_call or compliance.is_dnc(session, d.phone),
            "statuses": list(EmpanelmentStatus),
        },
    )


@router.post("/distributors/{distributor_id}/status")
async def distributor_status(request: Request, distributor_id: int, user: AdminUser):
    return await _post(request, _change_status, distributor_id, user)


def _change_status(request: Request, form: dict[str, str], distributor_id: int, user: str):
    back = f"/distributors/{distributor_id}"
    with closing(db.new_session()) as session:
        d = _get_or_404(session, Distributor, distributor_id)
        new = _enum_or_none(EmpanelmentStatus, form.get("status"))
        if new is None:
            return redirect_with_flash(request, back, f"Unknown status {form.get('status')!r}.", "error")
        reason = _clean(form.get("reason"), _MAX_REASON_CHARS)
        old = EmpanelmentStatus(d.status)
        if (d.do_not_call or old == EmpanelmentStatus.DO_NOT_CALL) and new != EmpanelmentStatus.DO_NOT_CALL:
            return redirect_with_flash(
                request,
                back,
                "This distributor opted out (do-not-call). Opt-outs cannot be reversed from the dashboard; "
                "contact Compliance if the opt-out was recorded in error.",
                "error",
            )
        if new == old:
            return redirect_with_flash(request, back, f"Status is already {new.value}.", "warn")
        if new == EmpanelmentStatus.DO_NOT_CALL:
            cancelled = _mark_do_not_call(session, d, reason or "Status set to do-not-call by admin")
        else:
            d.status = new  # deliberate direct assignment: ops record facts the funnel cannot infer
            cancelled = 0
        audit(
            session,
            "manual_status_change",
            distributor_id=d.id,
            old=old.value,
            new=new.value,
            reason=reason,
            by=user,
        )
        session.commit()
        log.info("Admin %s changed distributor %s status %s -> %s", user, d.id, old.value, new.value)
        message = f"Status changed from {old.value} to {new.value}."
        if cancelled:
            message += f" {cancelled} pending callback(s) cancelled."
        return redirect_with_flash(request, back, message)


@router.post("/distributors/{distributor_id}/notes")
async def distributor_notes(request: Request, distributor_id: int):
    return await _post(request, _save_notes, distributor_id)


def _save_notes(request: Request, form: dict[str, str], distributor_id: int):
    with closing(db.new_session()) as session:
        d = _get_or_404(session, Distributor, distributor_id)
        notes = (form.get("notes") or "").strip()
        d.notes = notes[:_MAX_NOTES_CHARS] or None
        session.commit()
    return redirect_with_flash(request, f"/distributors/{distributor_id}", "Notes saved.")


@router.post("/distributors/{distributor_id}/dnc")
async def distributor_dnc(request: Request, distributor_id: int, user: AdminUser):
    return await _post(request, _add_dnc, distributor_id, user)


def _add_dnc(request: Request, form: dict[str, str], distributor_id: int, user: str):
    with closing(db.new_session()) as session:
        d = _get_or_404(session, Distributor, distributor_id)
        reason = _clean(form.get("reason"), _MAX_REASON_CHARS) or "Added to do-not-call by admin"
        cancelled = _mark_do_not_call(session, d, reason)
        session.commit()
        log.info("Admin %s added distributor %s to the DNC list", user, d.id)
        message = f"{d.name} is on the do-not-call list and will not be called again."
        if cancelled:
            message += f" {cancelled} pending callback(s) cancelled."
    return redirect_with_flash(request, f"/distributors/{distributor_id}", message)


def _mark_do_not_call(session: Session, d: Distributor, reason: str) -> int:
    """DNC-list every number of ``d``, mark it do-not-call and cancel its pending callbacks."""
    for phone in dict.fromkeys(p for p in (d.phone, d.alt_phone) if p):
        compliance.add_to_dnc(session, phone, reason=reason, source="manual")
    # add_to_dnc matches distributors by normalised phone; make sure this one is marked regardless.
    funnel.advance_status(d, EmpanelmentStatus.DO_NOT_CALL)
    if not d.dnc_reason:
        d.dnc_reason = reason[:_MAX_REASON_CHARS]
    pending = session.scalars(
        select(Callback).where(Callback.distributor_id == d.id, Callback.status == CallbackStatus.PENDING)
    ).all()
    for cb in pending:
        cb.status = CallbackStatus.CANCELED
    return len(pending)


# ---------------------------------------------------------------------------------------------
# CSV / Excel import
# ---------------------------------------------------------------------------------------------


def _open_campaigns(session: Session) -> list[Campaign]:
    return list(
        session.scalars(
            select(Campaign).where(Campaign.status != CampaignStatus.COMPLETED).order_by(Campaign.name)
        )
    )


@router.get("/import", response_class=HTMLResponse)
def import_form(request: Request, session: DBSession):
    return render(request, "import.html", {"campaigns": _open_campaigns(session), "error": None})


@router.post("/import", response_class=HTMLResponse)
async def import_upload(request: Request):
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES + 64 * 1024:
        return await run_in_threadpool(_import_error, request, "The file is too large (limit 20 MB).", 413)
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return await run_in_threadpool(_import_error, request, "Choose a CSV file to upload.", 400)
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return await run_in_threadpool(_import_error, request, "The file is too large (limit 20 MB).", 413)
    fields = {key: value for key, value in form.multi_items() if isinstance(value, str)}
    return await run_in_threadpool(_run_import, request, data, upload.filename, fields)


def _import_error(request: Request, message: str, status_code: int):
    with closing(db.new_session()) as session:
        context = {"campaigns": _open_campaigns(session), "error": message}
        return render(request, "import.html", context, status_code=status_code)


def _run_import(request: Request, data: bytes, filename: str, fields: dict[str, str]):
    source = _clean(fields.get("source"), 100) or _clean(filename, 100) or "upload"
    update_existing = (fields.get("update_existing") or "").lower() in ("1", "on", "true", "yes")
    with closing(db.new_session()) as session:
        campaign = None
        raw_campaign = (fields.get("campaign_id") or "").strip()
        if raw_campaign:
            campaign = session.get(Campaign, int(raw_campaign)) if raw_campaign.isdigit() else None
            if campaign is None:
                return _import_error(request, "The selected campaign does not exist.", 400)
        try:
            # Bytes as uploaded: the importer detects .xlsx workbooks and decodes CSV as UTF-8.
            report = import_distributors_csv(
                session, io.BytesIO(data), source=source, update_existing=update_existing
            )
        except UnicodeDecodeError:
            session.rollback()
            return _import_error(
                request,
                "The file is not UTF-8 text. In Excel use Save As > 'CSV UTF-8 (Comma delimited)', "
                "or upload the .xlsx workbook.",
                400,
            )
        except (csv.Error, ValueError, zipfile.BadZipFile) as exc:
            session.rollback()
            return _import_error(request, f"Could not read the file as CSV or .xlsx: {exc}", 400)
        added = add_distributors_to_campaign(session, campaign, report.distributor_ids) if campaign else None
        session.commit()
        log.info("Admin CSV import %s: %d created, %d updated", source, report.created, report.updated)
        return render(
            request,
            "import_result.html",
            {
                "report": report,
                "filename": filename,
                "source": source,
                "campaign": campaign,
                "added": added,
                "max_rows_shown": 100,
            },
        )


# ---------------------------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------------------------


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_list(request: Request, session: DBSession):
    campaigns = session.scalars(select(Campaign).order_by(Campaign.id.desc())).all()
    counts: dict[int, dict[str, int]] = {}
    for campaign_id, state, n in session.execute(
        select(CampaignContact.campaign_id, CampaignContact.state, func.count(CampaignContact.id)).group_by(
            CampaignContact.campaign_id, CampaignContact.state
        )
    ):
        counts.setdefault(campaign_id, {})[ContactState(state).value] = n
    return render(
        request,
        "campaigns.html",
        {
            "campaigns": campaigns,
            "counts": counts,
            "states": [s.value for s in ContactState],
            "provider_name": request.app.state.provider.name,
        },
    )


@router.post("/campaigns")
async def campaign_create(request: Request):
    return await _post(request, _create_campaign)


def _create_campaign(request: Request, form: dict[str, str]):
    name = _clean(form.get("name"), 200)
    if not name:
        return redirect_with_flash(request, "/campaigns", "Give the campaign a name.", "error")
    description = (form.get("description") or "").strip()[:2000] or None
    with closing(db.new_session()) as session:
        exists = session.scalar(select(Campaign.id).where(func.lower(Campaign.name) == name.lower()))
        if exists is not None:
            return redirect_with_flash(request, "/campaigns", f"A campaign named {name!r} already exists.", "error")
        session.add(Campaign(name=name, description=description, status=CampaignStatus.DRAFT))
        session.commit()
    return redirect_with_flash(
        request, "/campaigns", f"Created campaign {name!r} (draft). Add eligible distributors, then start it."
    )


@router.post("/campaigns/{campaign_id}/{action}")
async def campaign_action(request: Request, campaign_id: int, action: str, user: AdminUser):
    if action not in _CAMPAIGN_ACTIONS:
        raise HTTPException(status_code=404, detail="Unknown campaign action")
    return await _post(request, _campaign_action, campaign_id, action, user)


def _campaign_action(request: Request, form: dict[str, str], campaign_id: int, action: str, user: str):
    with closing(db.new_session()) as session:
        campaign = _get_or_404(session, Campaign, campaign_id)
        message, level = _CAMPAIGN_ACTIONS[action](request, session, campaign)
        session.commit()
        log.info("Admin %s: campaign %s %s", user, campaign.id, action)
    return redirect_with_flash(request, _safe_next(form.get("next"), "/campaigns"), message, level)


def _add_eligible(request: Request, session: Session, campaign: Campaign) -> tuple[str, str]:
    if campaign.status == CampaignStatus.COMPLETED:
        return f"Campaign {campaign.name!r} is completed; create a new campaign instead.", "error"
    added = add_distributors_to_campaign(session, campaign)
    total = session.scalar(
        select(func.count(CampaignContact.id)).where(CampaignContact.campaign_id == campaign.id)
    )
    return f"Added {added} eligible distributor(s) to {campaign.name!r}; {total} contact(s) in total.", "ok"


def _start(request: Request, session: Session, campaign: Campaign) -> tuple[str, str]:
    # Same semantics as "callingbot campaign start": a paused or completed campaign resumes.
    campaign.status = CampaignStatus.ACTIVE
    campaign.started_at = campaign.started_at or _now(request)
    campaign.completed_at = None
    pending = session.scalar(
        select(func.count(CampaignContact.id)).where(
            CampaignContact.campaign_id == campaign.id, CampaignContact.state == ContactState.PENDING
        )
    )
    if not pending:
        return f"Campaign {campaign.name!r} is active, but it has no pending contacts yet.", "warn"
    return f"Campaign {campaign.name!r} is active ({pending} pending contact(s)).", "ok"


def _pause(request: Request, session: Session, campaign: Campaign) -> tuple[str, str]:
    if campaign.status != CampaignStatus.ACTIVE:
        return f"Campaign {campaign.name!r} is {campaign.status.value}, not active.", "warn"
    campaign.status = CampaignStatus.PAUSED
    return f"Campaign {campaign.name!r} is paused; no new calls will be placed.", "ok"


def _complete(request: Request, session: Session, campaign: Campaign) -> tuple[str, str]:
    campaign.status = CampaignStatus.COMPLETED
    campaign.completed_at = _now(request)
    return f"Campaign {campaign.name!r} is completed.", "ok"


def _dial_now(request: Request, session: Session, campaign: Campaign) -> tuple[str, str]:
    state = request.app.state
    now = _now(request)
    # Free the capacity of calls whose final webhook was lost before counting free lines.
    reaped = reap_stale_calls(session, kb=state.kb, settings=state.settings, now_utc=now)
    report = dial_due_contacts(
        session, campaign=campaign, provider=state.provider, kb=state.kb, settings=state.settings, now_utc=now
    )
    parts = [
        f"Dial now via {state.provider.name}: placed {report.placed}, skipped {report.skipped}, "
        f"failed {report.failed}."
    ]
    if reaped:
        parts.append(f"Closed {reaped} stale call(s).")
    parts.extend(report.messages)
    if state.provider.name == "simulator" and report.placed:
        parts.append("Simulated calls are never answered; use the Simulator page to talk to the bot.")
    level = "error" if report.failed else ("ok" if report.placed else "warn")
    return " ".join(parts), level


_CAMPAIGN_ACTIONS = {
    "add-eligible": _add_eligible,
    "start": _start,
    "pause": _pause,
    "complete": _complete,
    "dial-now": _dial_now,
}


# ---------------------------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------------------------


@router.get("/calls", response_class=HTMLResponse)
def calls_list(
    request: Request,
    session: DBSession,
    status: str | None = None,
    outcome: str | None = None,
    campaign_id: int | None = None,
    page: int = 1,
):
    stmt = select(Call).options(joinedload(Call.distributor))
    status_filter = _enum_or_none(CallStatus, status)
    outcome_filter = _enum_or_none(CallOutcome, outcome)
    if status_filter is not None:
        stmt = stmt.where(Call.status == status_filter)
    if outcome_filter is not None:
        stmt = stmt.where(Call.outcome == outcome_filter)
    if campaign_id is not None:
        stmt = stmt.where(Call.campaign_id == campaign_id)
    calls, pager = _paginate(session, stmt.order_by(Call.id.desc()), request, page)
    flagged: dict[int, int] = {}
    if calls:
        flagged = dict(
            session.execute(
                select(Turn.call_id, func.count(Turn.id))
                .where(Turn.call_id.in_([c.id for c in calls]), Turn.flagged.is_(True))
                .group_by(Turn.call_id)
            ).all()
        )
    return render(
        request,
        "calls.html",
        {
            "calls": calls,
            "pager": pager,
            "flagged": flagged,
            "status": status_filter.value if status_filter else "",
            "outcome": outcome_filter.value if outcome_filter else "",
            "statuses": list(CallStatus),
            "outcomes": list(CallOutcome),
            "campaigns": {c.id: c for c in session.scalars(select(Campaign))},
        },
    )


@router.get("/calls/{call_id}", response_class=HTMLResponse)
def call_detail(request: Request, call_id: int, session: DBSession):
    call = _get_or_404(session, Call, call_id)
    turns = session.scalars(select(Turn).where(Turn.call_id == call.id).order_by(Turn.id)).all()
    messages = session.scalars(
        select(OutboundMessage).where(OutboundMessage.call_id == call.id).order_by(OutboundMessage.id)
    ).all()
    callbacks = session.scalars(select(Callback).where(Callback.call_id == call.id).order_by(Callback.id)).all()
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.call_id == call.id).order_by(AuditEvent.id)
    ).all()
    recording = call.recording_url if (call.recording_url or "").startswith(("https://", "http://")) else None
    return render(
        request,
        "call_detail.html",
        {
            "call": call,
            "d": call.distributor,
            "campaign": session.get(Campaign, call.campaign_id) if call.campaign_id else None,
            "turns": turns,
            "messages": messages,
            "callbacks": callbacks,
            "events": events,
            "recording_url": recording,
            "flagged_count": sum(1 for t in turns if t.flagged),
            "mask_destination": mask_destination,
        },
    )


# ---------------------------------------------------------------------------------------------
# Callbacks and outbox
# ---------------------------------------------------------------------------------------------


@router.get("/callbacks", response_class=HTMLResponse)
def callbacks_list(request: Request, session: DBSession, status: str | None = None, page: int = 1):
    stmt = select(Callback)
    status_filter = _enum_or_none(CallbackStatus, status)
    if status_filter is not None:
        stmt = stmt.where(Callback.status == status_filter)
    pending_first = case((Callback.status == CallbackStatus.PENDING, 0), else_=1)
    rows, pager = _paginate(
        session, stmt.order_by(pending_first, Callback.scheduled_for, Callback.id), request, page
    )
    return render(
        request,
        "callbacks.html",
        {
            "callbacks": rows,
            "pager": pager,
            "distributors": _distributors_by_id(session, (cb.distributor_id for cb in rows)),
            "status": status_filter.value if status_filter else "",
            "statuses": list(CallbackStatus),
        },
    )


@router.post("/callbacks/{callback_id}/done")
async def callback_done(request: Request, callback_id: int):
    return await _post(request, _mark_callback_done, callback_id)


def _mark_callback_done(request: Request, form: dict[str, str], callback_id: int):
    back = _safe_next(form.get("next"), "/callbacks")
    with closing(db.new_session()) as session:
        cb = _get_or_404(session, Callback, callback_id)
        if cb.status != CallbackStatus.PENDING:
            return redirect_with_flash(request, back, f"Callback #{cb.id} is already {cb.status.value}.", "warn")
        cb.status = CallbackStatus.DONE
        session.commit()
    return redirect_with_flash(request, back, f"Callback #{callback_id} marked done.")


@router.get("/messages", response_class=HTMLResponse)
def messages_list(
    request: Request, session: DBSession, status: str | None = None, channel: str | None = None, page: int = 1
):
    stmt = select(OutboundMessage)
    status_filter = _enum_or_none(MessageStatus, status)
    channel_filter = _enum_or_none(MessageChannel, channel)
    if status_filter is not None:
        stmt = stmt.where(OutboundMessage.status == status_filter)
    if channel_filter is not None:
        stmt = stmt.where(OutboundMessage.channel == channel_filter)
    rows, pager = _paginate(session, stmt.order_by(OutboundMessage.id.desc()), request, page)
    return render(
        request,
        "messages.html",
        {
            "messages": rows,
            "pager": pager,
            "distributors": _distributors_by_id(session, (m.distributor_id for m in rows)),
            "status": status_filter.value if status_filter else "",
            "channel": channel_filter.value if channel_filter else "",
            "statuses": list(MessageStatus),
            "channels": list(MessageChannel),
            "mask_destination": mask_destination,
        },
    )


@router.post("/messages/{message_id}/mark-sent")
async def message_mark_sent(request: Request, message_id: int, user: AdminUser):
    return await _post(request, _mark_message_sent, message_id, user)


def _mark_message_sent(request: Request, form: dict[str, str], message_id: int, user: str):
    back = _safe_next(form.get("next"), "/messages")
    with closing(db.new_session()) as session:
        message = _get_or_404(session, OutboundMessage, message_id)
        if message.status == MessageStatus.SENT:
            return redirect_with_flash(request, back, f"Message #{message.id} is already sent.", "warn")
        previous = message.status.value
        message.status = MessageStatus.SENT
        message.sent_at = _now(request)
        # Evidence of what was sent to whom, by hand, outside any provider.
        audit(
            session,
            "message_marked_sent",
            call_id=message.call_id,
            distributor_id=message.distributor_id,
            message_id=message.id,
            channel=message.channel.value,
            previous_status=previous,
            by=user,
        )
        session.commit()
    return redirect_with_flash(request, back, f"Message #{message_id} marked as sent.")


# ---------------------------------------------------------------------------------------------
# JSON API and exports
# ---------------------------------------------------------------------------------------------


@router.get("/api/stats")
def api_stats(request: Request, session: DBSession):
    campaigns = session.scalars(select(Campaign).order_by(Campaign.id)).all()
    return {
        "generated_at": _iso(_now(request)),
        "overall": campaign_stats(session),
        "campaigns": [
            {"id": c.id, "name": c.name, "status": c.status.value, "stats": campaign_stats(session, c.id)}
            for c in campaigns
        ],
    }


@router.get("/api/calls/{call_id}")
def api_call(call_id: int, session: DBSession):
    call = _get_or_404(session, Call, call_id)
    d = call.distributor
    turns = session.scalars(select(Turn).where(Turn.call_id == call.id).order_by(Turn.id)).all()
    messages = session.scalars(
        select(OutboundMessage).where(OutboundMessage.call_id == call.id).order_by(OutboundMessage.id)
    ).all()
    return JSONResponse(
        {
            "id": call.id,
            "distributor": {"id": d.id, "name": d.name, "arn": d.arn, "phone": mask_phone(d.phone)},
            "campaign_id": call.campaign_id,
            "provider": call.provider,
            "provider_call_id": call.provider_call_id,
            "status": _value(call.status),
            "outcome": _value(call.outcome),
            "interest_level": _value(call.interest_level),
            "language": call.language,
            "answered_by": call.answered_by,
            "created_at": _iso(call.created_at),
            "answered_at": _iso(call.answered_at),
            "ended_at": _iso(call.ended_at),
            "duration_seconds": call.duration_seconds,
            "turn_count": call.turn_count,
            "summary": call.summary,
            "error": call.error,
            "recording_url": call.recording_url,
            "turns": [
                {
                    "id": t.id,
                    "role": _value(t.role),
                    "text": t.text,
                    "flagged": bool(t.flagged),
                    "meta": t.meta,
                    "created_at": _iso(t.created_at),
                }
                for t in turns
            ],
            "messages": [
                {
                    "id": m.id,
                    "channel": _value(m.channel),
                    "destination": mask_destination(m.channel, m.destination),
                    "status": _value(m.status),
                    "link": m.link,
                    "created_at": _iso(m.created_at),
                }
                for m in messages
            ],
        }
    )


def _csv_safe(value) -> str:
    # Spreadsheet formula injection: Excel executes a cell starting with = + - @. Names and call
    # summaries (written by the LLM) are free text, so neutralise them with a leading quote.
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/export/leads.csv")
def export_leads(request: Request, session: DBSession, campaign_id: int | None = None):
    """Interested / link-sent / callback leads: the same columns as ``callingbot export-leads``."""
    tz = request.app.state.settings.timezone
    stmt = select(Distributor).where(Distributor.status.in_(LEAD_STATUSES)).order_by(Distributor.id)
    if campaign_id is not None:
        _get_or_404(session, Campaign, campaign_id)
        members = select(CampaignContact.distributor_id).where(CampaignContact.campaign_id == campaign_id)
        stmt = stmt.where(Distributor.id.in_(members))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=LEAD_COLUMNS)
    writer.writeheader()
    for d in session.scalars(stmt):
        call_stmt = select(Call).where(Call.distributor_id == d.id)
        if campaign_id is not None:
            call_stmt = call_stmt.where(Call.campaign_id == campaign_id)
        last = session.scalar(call_stmt.order_by(Call.created_at.desc(), Call.id.desc()).limit(1))
        last_at = (last.ended_at or last.created_at) if last else None
        writer.writerow(
            {
                "arn": d.arn or "",
                "name": _csv_safe(d.name),
                "firm_name": _csv_safe(d.firm_name),
                "phone": d.phone,
                "email": _csv_safe(d.email),
                "city": _csv_safe(d.city),
                "status": EmpanelmentStatus(d.status).value,
                "last_call_outcome": last.outcome.value if last and last.outcome else "",
                "last_call_summary": _csv_safe(last.summary if last else None),
                "last_call_at": to_local(last_at, tz).isoformat(timespec="seconds") if last_at else "",
            }
        )
    stamp = to_local(_now(request), tz).strftime("%Y%m%d")
    # BOM so Excel (what the RM team opens it in) shows Hindi names correctly.
    return Response(
        content="﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="leads-{stamp}.csv"',
            "Cache-Control": "no-store",
        },
    )

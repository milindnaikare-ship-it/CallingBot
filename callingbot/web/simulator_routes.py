"""Browser call simulator: an admin plays the distributor and talks to the real conversation engine.

The page (``GET /simulator``) is a small chat UI (``static/simulator.js``) over this JSON API::

    POST /api/simulator/calls               {"distributor_id": int | null, "language": str | null}
         -> {"call_id", "say": [...], "action", "language"}
    POST /api/simulator/calls/{id}/input    {"text": str | null}       (null / "" = silence)
         -> {"say", "action", "outcome", "ended", "status", "transfer_to", "messages"}
    POST /api/simulator/calls/{id}/hangup   -> {"status", "outcome", "messages"}

Calls are created with provider ``"simulator"`` whatever ``TELEPHONY_PROVIDER`` is, and only such
calls can be driven here, so the API can never inject turns into a live phone call. The engine
uses the app's LLM and messenger, so a simulated call behaves exactly like a real one: tools
run, links are sent through the configured messaging providers and the funnel is updated.
That is why distributors who opted out cannot be picked, and why the page warns when a
messaging provider is live. ``distributor_id: null`` uses a synthetic demo distributor
(``ARN-000000``, ``+919000000000``, Mumbai), created on first use.

When the bot hangs up or transfers, or the admin hangs up, the call is finalised through
:func:`lifecycle.apply_status_update` with a ``completed`` status, as a provider callback would.
"""

from __future__ import annotations

import json
import logging
from contextlib import closing
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from callingbot import db
from callingbot.cli import DEMO_ARN, DEMO_PHONE
from callingbot.models import (
    Call,
    CallStatus,
    Distributor,
    EmpanelmentStatus,
    OutboundMessage,
)
from callingbot.services import lifecycle
from callingbot.telephony.base import CallStatusUpdate, VoiceResponse
from callingbot.web.admin import mask_destination
from callingbot.web.deps import db_session, make_engine, render, require_admin

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)], tags=["simulator"])

SIMULATOR_PROVIDER = "simulator"
PICKER_LIMIT = 200
MAX_UTTERANCE_CHARS = 2000


class StartCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    distributor_id: int | None = Field(default=None, ge=1)
    language: str | None = Field(default=None, max_length=16)


class CallInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=MAX_UTTERANCE_CHARS)


# ---------------------------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------------------------


@router.get("/simulator", response_class=HTMLResponse)
def simulator_page(request: Request, session: Annotated[Session, Depends(db_session)]):
    state = request.app.state
    settings = state.settings
    distributors = session.scalars(
        select(Distributor)
        .where(
            Distributor.do_not_call.is_(False),
            Distributor.status != EmpanelmentStatus.DO_NOT_CALL,
            or_(Distributor.arn.is_(None), Distributor.arn != DEMO_ARN),
        )
        .order_by(Distributor.name, Distributor.id)
        .limit(PICKER_LIMIT)
    ).all()
    live_channels = [
        f"{channel}={provider}"
        for channel, provider in (
            ("SMS", settings.sms_provider),
            ("WhatsApp", settings.whatsapp_provider),
            ("email", settings.email_provider),
        )
        if provider != "outbox"
    ]
    return render(
        request,
        "simulator.html",
        {
            "distributors": distributors,
            "languages": state.kb.amc.languages,
            "default_language": state.kb.amc.default_language,
            "live_channels": live_channels,
            "llm_model": getattr(state.llm, "model", "unknown"),
            "picker_limit": PICKER_LIMIT,
        },
    )


# ---------------------------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------------------------


async def _payload(request: Request, model: type[BaseModel]) -> Any:
    # Parsed here, after require_admin, rather than as a FastAPI body parameter (parsed pre-auth).
    body = await request.body()
    try:
        raw = json.loads(body) if body.strip() else {}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Request body must be JSON") from exc
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False, include_context=False)
        ) from exc


@router.post("/api/simulator/calls")
async def start_call(request: Request):
    payload = await _payload(request, StartCallRequest)
    return await run_in_threadpool(_start_call, request, payload)


@router.post("/api/simulator/calls/{call_id}/input")
async def call_input(request: Request, call_id: int):
    payload = await _payload(request, CallInputRequest)
    return await run_in_threadpool(_call_input, request, call_id, payload)


@router.post("/api/simulator/calls/{call_id}/hangup")
async def hangup(request: Request, call_id: int):
    return await run_in_threadpool(_hangup, request, call_id)


# ---------------------------------------------------------------------------------------------
# Handlers (threadpool)
# ---------------------------------------------------------------------------------------------


def _start_call(request: Request, payload: StartCallRequest) -> dict[str, Any]:
    state = request.app.state
    codes = [lang.code for lang in state.kb.amc.languages]
    if payload.language is not None and payload.language not in codes:
        raise HTTPException(status_code=422, detail=f"language must be one of: {', '.join(codes)}")
    with closing(db.new_session()) as session:
        if payload.distributor_id is None:
            distributor = _demo_distributor(session)
            # The demo distributor is synthetic: it simply prefers whatever language was picked.
            distributor.preferred_language = payload.language
        else:
            distributor = session.get(Distributor, payload.distributor_id)
            if distributor is None:
                raise HTTPException(status_code=404, detail="Distributor not found")
            if distributor.do_not_call or distributor.status == EmpanelmentStatus.DO_NOT_CALL:
                # A simulated call can send real messages; never to someone who opted out.
                raise HTTPException(status_code=409, detail="This distributor opted out (do-not-call)")

        now = state.clock()
        call = lifecycle.create_call(
            session, distributor=distributor, provider=SIMULATOR_PROVIDER, language=payload.language
        )
        # Same clock as the dialer's pacing / stale-call checks, which compare created_at with now.
        call.created_at = now
        lifecycle.apply_status_update(
            session,
            call,
            CallStatusUpdate(provider_call_id=None, status=CallStatus.IN_PROGRESS, answered_by="human"),
            kb=state.kb,
            now_utc=now,
            tz=state.settings.timezone,
        )
        session.commit()
        log.info("Simulator call %s started for distributor %s", call.id, distributor.id)

        engine = make_engine(session, request)
        response = _start_in_language(session, engine, call, distributor, payload.language)
        if response.action != "gather":
            _finalise(request, session, call)
        return {
            "call_id": call.id,
            "say": list(response.say),
            "action": response.action,
            "language": response.language,
        }


def _call_input(request: Request, call_id: int, payload: CallInputRequest) -> dict[str, Any]:
    with closing(db.new_session()) as session:
        call = _simulator_call(session, call_id)
        engine = make_engine(session, request)
        text = (payload.text or "").strip() or None
        response = engine.handle_input(call, text)
        ended = response.action != "gather"
        if ended:
            _finalise(request, session, call)
        return {
            "say": list(response.say),
            "action": response.action,
            "transfer_to": response.transfer_to,
            "language": response.language,
            "outcome": call.outcome.value if call.outcome else None,
            "status": CallStatus(call.status).value,
            "ended": ended,
            "messages": _messages(session, call),
        }


def _hangup(request: Request, call_id: int) -> dict[str, Any]:
    with closing(db.new_session()) as session:
        call = _simulator_call(session, call_id)
        _finalise(request, session, call)
        return {
            "status": CallStatus(call.status).value,
            "outcome": call.outcome.value if call.outcome else None,
            "messages": _messages(session, call),
        }


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _simulator_call(session: Session, call_id: int) -> Call:
    call = session.get(Call, call_id)
    # Only simulator calls: the API must never be able to inject turns into a live phone call.
    if call is None or call.provider != SIMULATOR_PROVIDER:
        raise HTTPException(status_code=404, detail="Simulator call not found")
    return call


def _demo_distributor(session: Session) -> Distributor:
    distributor = session.scalar(select(Distributor).where(Distributor.arn == DEMO_ARN))
    if distributor is None:
        distributor = Distributor(
            arn=DEMO_ARN,
            name="Demo Distributor",
            phone=DEMO_PHONE,
            city="Mumbai",
            status=EmpanelmentStatus.NEW,
            do_not_call=False,
            source="demo",
        )
        session.add(distributor)
        session.flush()
    return distributor


def _start_in_language(
    session: Session, engine, call: Call, distributor: Distributor, language: str | None
) -> VoiceResponse:
    """``engine.start`` in ``language`` without permanently changing a real distributor's preference.

    The engine picks the call language from ``distributor.preferred_language`` when the call is
    answered, so the choice is applied by overriding that preference for ``start`` only.
    """
    original = distributor.preferred_language
    if not language or language == original:
        return engine.start(call, answered_by="human")
    distributor.preferred_language = language
    try:
        return engine.start(call, answered_by="human")
    finally:
        distributor.preferred_language = original
        session.commit()


def _finalise(request: Request, session: Session, call: Call) -> None:
    state = request.app.state
    lifecycle.apply_status_update(
        session,
        call,
        CallStatusUpdate(provider_call_id=None, status=CallStatus.COMPLETED),
        kb=state.kb,
        now_utc=state.clock(),
        tz=state.settings.timezone,
    )
    session.commit()


def _messages(session: Session, call: Call) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(OutboundMessage).where(OutboundMessage.call_id == call.id).order_by(OutboundMessage.id)
    ).all()
    return [
        {
            "id": m.id,
            "channel": m.channel.value,
            "destination": mask_destination(m.channel, m.destination),
            "status": m.status.value,
            "link": m.link,
            "body": m.body,
        }
        for m in rows
    ]

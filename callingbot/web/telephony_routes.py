"""Telephony provider webhooks: answer, conversational turn and status callback.

::

    POST /telephony/{provider}/answer/{call_id}   call connected  -> greeting
    POST /telephony/{provider}/turn/{call_id}     caller spoke / silence -> next reply
    POST /telephony/{provider}/status/{call_id}   lifecycle status callback -> 204

``{provider}`` must be the configured provider (``app.state.provider.name``), otherwise 404. The
simulator provider verifies nothing, so its webhooks are switched off (404) in production.
Every request is authenticated with :meth:`TelephonyProvider.verify_webhook` against the URL the
provider was given (``PUBLIC_BASE_URL`` + path, see :meth:`TelephonyProvider.webhook_url`) - not
the internal URL behind the reverse proxy - and rejected with 403 when that fails.

**The caller is never left in silence.** An unknown call id, a webhook for a different call
(provider or ``CallSid`` mismatch, e.g. after a database reset reused ids) or an unexpected
exception all produce a short scripted apology and a hang-up. On an exception the turn is
rolled back, ``webhook_error`` is audited and the call is marked ended so retries hang up. If
the failed turn was an opt-out ("don't call me again"), the opt-out is still honoured: the
number goes on the DNC list and the caller hears the opt-out confirmation (TRAI TCCCPR / DPDP).

The handlers are ``async`` only to read the form; the database / LLM work is blocking and runs
in Starlette's threadpool.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from callingbot import compliance, db
from callingbot.agent.engine import script
from callingbot.knowledge import KnowledgeBase
from callingbot.models import Call, CallOutcome, Turn, TurnRole, audit
from callingbot.phone import mask_phone
from callingbot.services import lifecycle
from callingbot.telephony.base import TelephonyProvider, VoiceResponse
from callingbot.web.deps import make_engine

log = logging.getLogger(__name__)

router = APIRouter(prefix="/telephony", tags=["telephony"])

CallId = Annotated[int, Path(ge=1, le=2**63 - 1)]

# Lines spoken by this layer only, when the engine cannot be used. Deliberately promise nothing
# (no callback): after a crash we cannot know what was already arranged. Keyed by base language.
_APOLOGY = {
    "en": "I'm sorry, we are facing a technical issue and need to end this call. Thank you for your time.",
    "hi": "क्षमा करें, तकनीकी समस्या के कारण हमें यह कॉल समाप्त करनी होगी। आपके समय के लिए धन्यवाद।",
}
_UNKNOWN_CALL = {
    "en": "Sorry, this call cannot be continued. Goodbye.",
    "hi": "क्षमा करें, यह कॉल जारी नहीं रखी जा सकती। धन्यवाद।",
}
# Same rule as the engine's closing disclaimer: not for people the scheme must not be pitched to.
_NO_DISCLAIMER_OUTCOMES = frozenset({CallOutcome.OPTED_OUT, CallOutcome.WRONG_PERSON, CallOutcome.VOICEMAIL})


# ---------------------------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------------------------


@router.post("/{provider}/answer/{call_id}")
async def answer_webhook(provider: str, call_id: CallId, request: Request) -> Response:
    telephony = _configured_provider(request, provider)
    params = await _verified_params(request, telephony)
    return await run_in_threadpool(_handle_voice, request, telephony, call_id, params, "answer")


@router.post("/{provider}/turn/{call_id}")
async def turn_webhook(provider: str, call_id: CallId, request: Request) -> Response:
    telephony = _configured_provider(request, provider)
    params = await _verified_params(request, telephony)
    return await run_in_threadpool(_handle_voice, request, telephony, call_id, params, "turn")


@router.post("/{provider}/status/{call_id}", status_code=204)
async def status_webhook(provider: str, call_id: CallId, request: Request) -> Response:
    telephony = _configured_provider(request, provider)
    params = await _verified_params(request, telephony)
    return await run_in_threadpool(_handle_status, request, telephony, call_id, params)


# ---------------------------------------------------------------------------------------------
# Request checks
# ---------------------------------------------------------------------------------------------


def _configured_provider(request: Request, name: str) -> TelephonyProvider:
    provider: TelephonyProvider = request.app.state.provider
    if name != provider.name:
        raise HTTPException(status_code=404, detail="Not Found")
    if provider.name == "simulator" and request.app.state.settings.app_env == "prod":
        # Unsigned webhooks that drive real conversations must not be reachable in production.
        raise HTTPException(status_code=404, detail="Not Found")
    return provider


def verification_url(request: Request) -> str:
    """The public URL the provider called: ``PUBLIC_BASE_URL`` + path (+ query)."""
    url = request.app.state.settings.base_url + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    return url


def form_params(form: FormData) -> dict[str, str]:
    """Flatten a form to ``{name: value}``: the last value of a repeated name wins, files are dropped."""
    return {key: value for key, value in form.multi_items() if isinstance(value, str)}


async def _verified_params(request: Request, provider: TelephonyProvider) -> dict[str, str]:
    form = await request.form()
    # The full multi-dict goes to the signature check: a signature covers every value of a
    # repeated parameter, which the flattened dict would lose.
    if not provider.verify_webhook(url=verification_url(request), params=form, headers=request.headers):
        log.warning("Rejected %s webhook %s: signature verification failed", provider.name, request.url.path)
        raise HTTPException(status_code=403, detail="Webhook verification failed")
    return form_params(form)


def _find_call(session: Session, provider: TelephonyProvider, call_id: int, provider_call_id: str | None):
    """The call this webhook is about, or None when it does not belong to us / to this call."""
    call = session.get(Call, call_id)
    if call is None:
        log.warning("%s webhook for unknown call %s", provider.name, call_id)
        return None
    if call.provider != provider.name:
        log.warning("%s webhook for call %s, which was placed via %s", provider.name, call_id, call.provider)
        return None
    if provider_call_id and call.provider_call_id and provider_call_id != call.provider_call_id:
        log.warning(
            "%s webhook for call %s carries CallSid %s, but the call is %s",
            provider.name,
            call_id,
            provider_call_id,
            call.provider_call_id,
        )
        return None
    return call


# ---------------------------------------------------------------------------------------------
# Handlers (run in the threadpool)
# ---------------------------------------------------------------------------------------------


def _handle_voice(
    request: Request, provider: TelephonyProvider, call_id: int, params: Mapping[str, str], stage: str
) -> Response:
    kb: KnowledgeBase = request.app.state.kb
    inp = provider.parse_voice_input(params)
    session = db.new_session()
    try:
        try:
            call = _find_call(session, provider, call_id, inp.provider_call_id)
        except Exception as exc:  # database unavailable: still answer the caller
            return _fail(request, session, provider, call_id, stage, exc, inp.speech_text)
        if call is None:
            default = kb.amc.default_language
            return _render(provider, _hangup(kb, default, [_line(_UNKNOWN_CALL, default)]), call_id)
        try:
            if inp.provider_call_id and not call.provider_call_id:
                call.provider_call_id = inp.provider_call_id
            engine = make_engine(session, request)
            if stage == "answer":
                response = engine.start(call, answered_by=inp.answered_by)
            else:
                response = engine.handle_input(call, inp.speech_text, confidence=inp.confidence)
            body, media_type = provider.render(response, call_id=call_id)
        except Exception as exc:
            return _fail(request, session, provider, call_id, stage, exc, inp.speech_text)
        return Response(content=body, media_type=media_type)
    finally:
        session.close()


def _handle_status(
    request: Request, provider: TelephonyProvider, call_id: int, params: Mapping[str, str]
) -> Response:
    state = request.app.state
    update = provider.parse_status(params)
    session = db.new_session()
    try:
        call = _find_call(session, provider, call_id, update.provider_call_id)
        if call is None:
            # 2xx anyway: a retried callback for a call we do not know will never succeed.
            return Response(status_code=204)
        try:
            lifecycle.apply_status_update(
                session, call, update, kb=state.kb, now_utc=state.clock(), tz=state.settings.timezone
            )
            session.commit()
        except Exception as exc:
            log.exception("%s status webhook failed for call %s", provider.name, call_id)
            session.rollback()
            _record_error(session, call_id, "status", provider, exc)
            return Response(status_code=500)
        return Response(status_code=204)
    finally:
        session.close()


# ---------------------------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------------------------


def _fail(
    request: Request,
    session: Session,
    provider: TelephonyProvider,
    call_id: int,
    stage: str,
    exc: Exception,
    speech_text: str | None,
) -> Response:
    """Speak a scripted goodbye after an unexpected error, and record what happened."""
    log.error("%s %s webhook failed for call %s", provider.name, stage, call_id, exc_info=exc)
    kb: KnowledgeBase = request.app.state.kb
    language = kb.amc.default_language
    say = [_line(_APOLOGY, language)]
    try:
        session.rollback()
        call = session.get(Call, call_id)
        planned = say
        if call is not None:
            language = call.language or language
            say = [_line(_APOLOGY, language)]
            if speech_text is not None and compliance.detect_opt_out(speech_text):
                _honour_opt_out(session, call)
                planned = [script("opt_out_confirm", language)]
            else:
                planned = list(say)
            state = dict(call.engine_state or {})
            if (
                (call.turn_count or 0) >= 1
                and not state.get("disclaimer_spoken")
                and call.outcome not in _NO_DISCLAIMER_OUTCOMES
            ):
                planned.append(kb.nfo.disclaimer(kb.amc.language(language).code))
                state["disclaimer_spoken"] = True
            for text in planned:
                session.add(
                    Turn(call_id=call.id, role=TurnRole.BOT, text=text, meta={"scripted": "webhook_error"})
                )
            # Ended: a retried webhook now gets a silent hang-up instead of a second conversation.
            call.engine_state = {**state, "ended": True, "last_say": planned, "last_action": "hangup"}
            call.pending_action = "hangup"
            call.error = f"{stage} webhook: {type(exc).__name__}: {exc}"[:2000]
        _record_error(session, call.id if call is not None else None, stage, provider, exc, commit=False)
        session.commit()
        # Only now: never tell someone "we won't call you again" unless the opt-out was stored.
        say = planned
    except Exception:
        log.exception("Could not record the %s webhook failure for call %s", stage, call_id)
        try:
            session.rollback()
        except Exception:
            log.exception("Rollback failed")
    return _render(provider, _hangup(kb, language, say), call_id)


def _honour_opt_out(session: Session, call: Call) -> None:
    distributor = call.distributor
    for phone in dict.fromkeys(p for p in (distributor.phone, distributor.alt_phone) if p):
        compliance.add_to_dnc(session, phone, reason="Asked not to be called again", source="call_opt_out")
    call.outcome = CallOutcome.OPTED_OUT
    audit(
        session,
        "opt_out",
        call_id=call.id,
        distributor_id=distributor.id,
        reason="Detected by the platform after a webhook error",
    )
    log.info("Call %s: opt-out honoured after a webhook error (%s)", call.id, mask_phone(distributor.phone))


def _record_error(
    session: Session,
    call_id: int | None,
    stage: str,
    provider: TelephonyProvider,
    exc: Exception,
    *,
    commit: bool = True,
) -> None:
    try:
        distributor_id = None
        if call_id is not None:
            call = session.get(Call, call_id)
            distributor_id = call.distributor_id if call is not None else None
            call_id = call.id if call is not None else None
        audit(
            session,
            "webhook_error",
            call_id=call_id,
            distributor_id=distributor_id,
            stage=stage,
            provider=provider.name,
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
        if commit:
            session.commit()
    except Exception:
        if not commit:
            raise
        log.exception("Could not audit the %s webhook failure", stage)
        session.rollback()


# ---------------------------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------------------------


def _line(lines: dict[str, str], language: str | None) -> str:
    base = (language or "").strip().lower().split("-")[0]
    return lines.get(base, lines["en"])


def _hangup(kb: KnowledgeBase, language: str | None, say: list[str]) -> VoiceResponse:
    lang = kb.amc.language(language)
    return VoiceResponse(
        say=say, language=lang.code, voice=lang.twilio_voice, stt_language=lang.stt_language, action="hangup"
    )


def _render(provider: TelephonyProvider, response: VoiceResponse, call_id: int) -> Response:
    try:
        body, media_type = provider.render(response, call_id=call_id)
    except Exception:
        # E.g. Exotel, whose conversational turns are not implemented yet (Phase 3).
        log.exception("%s cannot render a voice response for call %s", provider.name, call_id)
        return PlainTextResponse("Voice response not available for this provider", status_code=501)
    return Response(content=body, media_type=media_type)

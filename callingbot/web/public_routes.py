"""Public routes (no login): health check and tracked empanelment links.

``GET /r/{token}`` is what distributors open from the SMS / WhatsApp / e-mail the bot sends. The
token is HMAC-signed and carries ids only (:mod:`callingbot.links`); a valid one records a
:class:`~callingbot.models.LinkClick` and redirects (302) to the AMC's empanelment form with the
ARN pre-filled. Invalid or tampered tokens get a plain 404 page that reveals nothing.

Link-preview fetchers (WhatsApp, Telegram, Slack, iMessage/Facebook, ...) request the URL as
soon as a message is delivered or pasted; they are redirected too, but not counted as clicks,
otherwise every WhatsApp link would look clicked.
"""

from __future__ import annotations

import logging
import re
from contextlib import closing
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from callingbot import db, links
from callingbot.models import Call, Distributor, LinkClick
from callingbot.web.deps import db_session

log = logging.getLogger(__name__)

router = APIRouter(tags=["public"])

_MAX_TOKEN_CHARS = 200  # real tokens are ~45 characters
_MAX_USER_AGENT_CHARS = 500  # LinkClick.user_agent is String(500)
_PREVIEW_BOTS = re.compile(
    r"facebookexternalhit|facebot|whatsapp/|telegrambot|twitterbot|slackbot|linkedinbot|discordbot"
    r"|skypeuripreview|googlebot|bingbot|applebot|embedly|pinterest|vkshare|redditbot",
    re.IGNORECASE,
)

DBSession = Annotated[Session, Depends(db_session)]


@router.get("/healthz")
def healthz():
    """Liveness + database reachability, for the load balancer / container health check."""
    # Its own session (not the dependency) so an unreachable database is a 503, not a 500.
    try:
        with closing(db.new_session()) as session:
            session.execute(text("SELECT 1"))
    except Exception:
        log.exception("Health check: database unavailable")
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}


@router.get("/r/{token}")
def tracked_link(token: str, request: Request, session: DBSession):
    state = request.app.state
    parsed = (
        links.parse_link_token(state.settings.secret_key, token) if len(token) <= _MAX_TOKEN_CHARS else None
    )
    distributor = session.get(Distributor, parsed[0]) if parsed else None
    if parsed is None or distributor is None:
        return _invalid_link(request)

    call_id = parsed[1]
    # The ref in the target URL keeps the token's call id; the click row only links a call that
    # still exists and belongs to this distributor (a foreign key would reject anything else).
    call = session.get(Call, call_id) if call_id is not None else None
    click_call_id = call.id if call is not None and call.distributor_id == distributor.id else None

    user_agent = request.headers.get("user-agent") or None
    if user_agent and _PREVIEW_BOTS.search(user_agent):
        log.info("Link preview fetch for distributor %s not counted as a click", distributor.id)
    else:
        try:
            session.add(
                LinkClick(
                    distributor_id=distributor.id,
                    call_id=click_call_id,
                    user_agent=user_agent[:_MAX_USER_AGENT_CHARS] if user_agent else None,
                )
            )
            session.commit()
        except Exception:
            # The distributor must still reach the form; a lost click count is the lesser evil.
            log.exception("Could not record a link click for distributor %s", distributor.id)
            session.rollback()
    return RedirectResponse(links.empanelment_target_url(state.kb, distributor, call_id), status_code=302)


def _invalid_link(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "link_invalid.html", {"amc": request.app.state.kb.amc}, status_code=404
    )

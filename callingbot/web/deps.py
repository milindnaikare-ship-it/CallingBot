"""FastAPI dependencies shared by the web routers: database session, app state, admin auth, CSRF.

Everything the routes need at runtime (settings, knowledge base, LLM client, messenger,
telephony provider, templates, clock) is stored on ``app.state`` by
:func:`callingbot.web.app.create_app`; the getters here read it from the request so tests can
build an app with fakes and nothing is resolved at import time.

**Admin authentication** is HTTP Basic (``ADMIN_USERNAME`` / ``ADMIN_PASSWORD``), compared in
constant time. Serve the app over HTTPS only (see docs/DEPLOYMENT.md).

**CSRF policy.** Browsers re-send HTTP Basic credentials automatically, so a page on another
site could make an admin's browser submit our forms. Every state-changing admin or simulator
request (any method other than GET / HEAD / OPTIONS) must therefore prove it came from our own
pages:

* if an ``Origin`` header is present, its host must equal the request's ``Host`` (or the host of
  ``PUBLIC_BASE_URL``, for proxies that rewrite ``Host``); ``Origin: null`` is rejected;
* otherwise, if a ``Referer`` header is present, its host must match the same way;
* a request with neither header is allowed **only** when its ``Content-Type`` is
  ``application/json``. Browsers always send ``Origin`` on cross-origin POSTs, and a cross-site
  page cannot send a JSON body without a CORS preflight (which this app never grants), so such a
  request comes from a script or ``curl`` holding the credentials, not from a forged form.

Anything else is rejected with 403. The check runs after authentication, so an anonymous
request still gets the 401 challenge.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from callingbot import db
from callingbot.agent.engine import ConversationEngine
from callingbot.agent.llm import LLMClient
from callingbot.knowledge import KnowledgeBase
from callingbot.messaging import Messenger
from callingbot.settings import Settings
from callingbot.telephony.base import TelephonyProvider

log = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORTS = (":80", ":443")

# auto_error=False so a missing header gets our 401 (with realm); a malformed one is rejected with
# 401 by HTTPBasic itself. Wrong username and wrong password get the same response.
_basic = HTTPBasic(auto_error=False)


# ---------------------------------------------------------------------------------------------
# Database and app state
# ---------------------------------------------------------------------------------------------


def db_session() -> Iterator[Session]:
    """A session for one request; route handlers commit, the session is always closed."""
    session = db.new_session()
    try:
        yield session
    finally:
        session.close()


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_kb(request: Request) -> KnowledgeBase:
    return request.app.state.kb


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_messenger(request: Request) -> Messenger:
    return request.app.state.messenger


def get_provider(request: Request) -> TelephonyProvider:
    return request.app.state.provider


def get_clock(request: Request) -> Callable[[], datetime]:
    """The app's clock (naive UTC). Tests replace ``app.state.clock`` to pin "now"."""
    return request.app.state.clock


def now_utc(request: Request) -> datetime:
    return request.app.state.clock()


async def read_form(request: Request) -> dict[str, str]:
    """The request's form fields as ``{name: value}`` (last value wins; uploaded files are left out).

    Endpoints call this *after* their dependencies (auth, CSRF) have run, unlike FastAPI ``Form()``
    parameters, which make FastAPI parse the body before authenticating the request.
    """
    form = await request.form()
    return {key: value for key, value in form.multi_items() if isinstance(value, str)}


def make_engine(session: Session, request: Request) -> ConversationEngine:
    """A conversation engine wired to the app's knowledge base, LLM, messenger and clock."""
    state = request.app.state
    return ConversationEngine(
        session=session,
        kb=state.kb,
        settings=state.settings,
        llm=state.llm,
        messenger=state.messenger,
        now=state.clock,
    )


# ---------------------------------------------------------------------------------------------
# Admin authentication and CSRF
# ---------------------------------------------------------------------------------------------


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="CallingBot admin"'},
    )


def _credentials_ok(settings: Settings, credentials: HTTPBasicCredentials) -> bool:
    # Compare both parts, always: short-circuiting on the username would reveal valid usernames
    # through timing.
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"), settings.admin_username.encode("utf-8")
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"), settings.admin_password.encode("utf-8")
    )
    return user_ok and password_ok


def _normalise_host(host: str | None) -> str:
    host = (host or "").strip().lower()
    for port in _DEFAULT_PORTS:
        if host.endswith(port):
            return host[: -len(port)]
    return host


def _url_host(url: str | None) -> str:
    try:
        return _normalise_host(urlsplit(url or "").netloc)
    except ValueError:
        return ""


def _allowed_hosts(request: Request) -> set[str]:
    hosts = {_normalise_host(request.headers.get("host")), _url_host(request.app.state.settings.base_url)}
    hosts.discard("")
    return hosts


def _is_json(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


def check_same_origin(request: Request) -> None:
    """Enforce the CSRF policy in the module docstring for a state-changing request (403 on failure)."""
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin is not None:
        source, which = _url_host(origin) if origin.strip().lower() != "null" else "", "Origin"
    elif referer is not None:
        source, which = _url_host(referer), "Referer"
    elif _is_json(request):
        return
    else:
        log.warning("CSRF check failed for %s %s: no Origin or Referer", request.method, request.url.path)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request rejected (no Origin or Referer)")
    if source and source in _allowed_hosts(request):
        return
    log.warning("CSRF check failed for %s %s: %s %r", request.method, request.url.path, which, source)
    raise HTTPException(status.HTTP_403_FORBIDDEN, f"Cross-site request rejected ({which} mismatch)")


def require_admin(request: Request, credentials: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    """HTTP Basic admin login plus the CSRF check for state-changing requests. Returns the username."""
    if credentials is None or not _credentials_ok(request.app.state.settings, credentials):
        raise _unauthorized()
    if request.method.upper() not in SAFE_METHODS:
        check_same_origin(request)
    return credentials.username


# ---------------------------------------------------------------------------------------------
# Flash messages and rendering
# ---------------------------------------------------------------------------------------------

# A one-shot message shown after a POST-redirect-GET. It travels in a cookie rather than the
# query string so nobody can craft a link that makes our admin pages display arbitrary text.
FLASH_COOKIE = "callingbot_flash"
_FLASH_MAX_CHARS = 1500  # cookies are limited to ~4 KB


def redirect_with_flash(request: Request, url: str, message: str | None = None, level: str = "ok"):
    """303 redirect to ``url`` carrying ``message`` (level ``ok`` / ``warn`` / ``error``) to the next page."""
    response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    if message:
        payload = json.dumps({"message": message[:_FLASH_MAX_CHARS], "level": level}, ensure_ascii=True)
        response.set_cookie(
            FLASH_COOKIE,
            base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii"),
            max_age=60,
            path="/",
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
    return response


def read_flash(request: Request) -> dict[str, str] | None:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("ascii"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("message"), str):
        return None
    level = data.get("level") if data.get("level") in ("ok", "warn", "error") else "ok"
    return {"message": data["message"], "level": level}


def render(request: Request, name: str, context: dict[str, Any] | None = None, *, status_code: int = 200):
    """Render a Jinja2 template (autoescaped), consuming the flash message left by a redirect."""
    context = dict(context or {})
    flash = read_flash(request)
    context.setdefault("flash", flash)
    response = request.app.state.templates.TemplateResponse(request, name, context, status_code=status_code)
    if FLASH_COOKIE in request.cookies:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response

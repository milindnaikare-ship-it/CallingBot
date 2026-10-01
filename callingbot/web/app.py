"""FastAPI application factory.

``uvicorn --factory callingbot.web.app:create_app`` (what ``callingbot serve`` and the Docker
image run). There is deliberately no module-level ``app``: importing this module must not open
a database, read configuration or build provider clients.

The factory wires every runtime dependency onto ``app.state`` (settings, knowledge base, LLM
client, messenger, telephony provider, templates and the clock) so routers read them per
request and tests can inject fakes::

    app = create_app(settings, llm=ScriptedLLM([...]), provider=SimulatorProvider())

Routers: public (``/healthz``, ``/r/{token}``), telephony webhooks (``/telephony/...``), admin
dashboard (``/``, HTTP Basic) and the browser call simulator (``/simulator``, HTTP Basic).
"""

from __future__ import annotations

import logging
from pathlib import Path

import jinja2
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from callingbot import db
from callingbot.agent.llm import LLMClient, build_llm
from callingbot.knowledge import load_knowledge
from callingbot.messaging import Messenger, build_messenger
from callingbot.phone import mask_phone
from callingbot.settings import Settings, get_settings
from callingbot.telephony import get_provider
from callingbot.telephony.base import TelephonyProvider
from callingbot.timeutil import to_local, utcnow

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# Pages render LLM-written transcripts and imported spreadsheet data. Autoescaping is the main
# defence; the CSP is the backstop: only our own scripts/styles, no framing, forms post to us.
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'"
)
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Same-origin requests keep their Referer (the CSRF check may need it); other sites get none.
    "Referrer-Policy": "same-origin",
}


def create_app(
    settings: Settings | None = None,
    *,
    llm: LLMClient | None = None,
    messenger: Messenger | None = None,
    provider: TelephonyProvider | None = None,
) -> FastAPI:
    """Build the web app. Raises ``RuntimeError`` for an unsafe production configuration."""
    settings = settings or get_settings()
    _configure_logging(settings.log_level)
    _check_production_settings(settings)

    # Synchronously, not in a lifespan hook: tests use the app without running startup events,
    # and a missing table should fail here, not on the first webhook of a live call.
    db.configure_engine(settings.database_url)
    db.init_db()
    kb = load_knowledge(settings.config_dir)

    is_prod = settings.app_env == "prod"
    app = FastAPI(
        title="CallingBot",
        # The interactive API docs are unauthenticated; keep them out of production.
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/openapi.json",
    )
    app.state.settings = settings
    app.state.kb = kb
    app.state.llm = llm or build_llm(settings)
    app.state.messenger = messenger or build_messenger(settings)
    app.state.provider = provider or get_provider(settings.telephony_provider, settings)
    # Every route takes "now" from here; tests pin it (e.g. inside the calling window).
    app.state.clock = utcnow
    app.state.templates = _build_templates(settings)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Content-Security-Policy", _CSP)
            # Admin pages show personal data; never let a shared proxy or the browser cache keep them.
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    from callingbot.web import admin, public_routes, simulator_routes, telephony_routes

    app.include_router(public_routes.router)
    app.include_router(telephony_routes.router)
    app.include_router(admin.router)
    app.include_router(simulator_routes.router)

    log.info(
        "CallingBot web app ready (env=%s, telephony=%s, llm=%s)",
        settings.app_env,
        app.state.provider.name,
        getattr(app.state.llm, "model", "?"),
    )
    return app


def _configure_logging(level_name: str) -> None:
    level = logging.getLevelNamesMapping().get((level_name or "").strip().upper(), logging.INFO)
    # basicConfig is a no-op when the host (uvicorn, pytest) already configured the root logger.
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("callingbot").setLevel(level)


def _check_production_settings(settings: Settings) -> None:
    """Refuse to serve production traffic with default secrets or without HTTPS."""
    if settings.app_env != "prod":
        return
    defaults = Settings.model_fields
    problems: list[str] = []
    if not settings.admin_password or settings.admin_password == defaults["admin_password"].default:
        problems.append("ADMIN_PASSWORD is empty or still the default")
    if not settings.secret_key or settings.secret_key == defaults["secret_key"].default:
        problems.append("SECRET_KEY is empty or still the default (tracking links could be forged)")
    if not settings.base_url.lower().startswith("https://"):
        problems.append(f"PUBLIC_BASE_URL must be an https:// URL, got {settings.base_url!r}")
    if problems:
        raise RuntimeError("Refusing to start with APP_ENV=prod: " + "; ".join(problems))


def _build_templates(settings: Settings) -> Jinja2Templates:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,  # every template, whatever its extension
        trim_blocks=True,
        lstrip_blocks=True,
    )
    tz = settings.timezone

    def local_dt(value, fmt: str = "%d %b %Y %H:%M") -> str:
        # DB datetimes are naive UTC; operators think in IST.
        return to_local(value, tz).strftime(fmt) if value else "-"

    def enum_value(value) -> str:
        return "" if value is None else str(getattr(value, "value", value))

    env.filters["local"] = local_dt
    env.filters["mask"] = mask_phone
    env.filters["val"] = enum_value
    env.filters["label"] = lambda value: enum_value(value).replace("_", " ")
    env.globals["timezone"] = tz
    env.globals["app_env"] = settings.app_env
    return Jinja2Templates(env=env)

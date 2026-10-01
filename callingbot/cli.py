"""The ``callingbot`` command line.

::

    callingbot init-db
    callingbot check-config
    callingbot import-distributors PATH [--source NAME] [--campaign NAME] [--no-update]
    callingbot campaign create NAME [--description TEXT]
    callingbot campaign add NAME | start NAME | pause NAME | list
    callingbot run-dialer --campaign NAME [--once] [--interval SECONDS]
    callingbot simulate [--arn ARN] [--language CODE]
    callingbot serve [--host HOST] [--port PORT] [--reload]
    callingbot stats [--campaign NAME]
    callingbot export-leads PATH [--campaign NAME]

Every command reads settings from the environment / ``.env``, opens the database they name and
creates missing tables, so the commands work in any order on a fresh install. Commands print
short human-readable output and return a non-zero exit code on errors (unknown campaign,
missing file, invalid configuration).

The conversation engine and the web app are imported lazily inside their commands: the
administrative commands must keep working even when an optional part (e.g. the LLM SDK
credentials) is not set up.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from callingbot import db
from callingbot.knowledge import KnowledgeBase, load_knowledge
from callingbot.models import (
    Call,
    Callback,
    CallbackStatus,
    CallStatus,
    Campaign,
    CampaignContact,
    CampaignStatus,
    ContactState,
    Distributor,
    EmpanelmentStatus,
    OutboundMessage,
)
from callingbot.phone import mask_phone
from callingbot.services.dialer import add_distributors_to_campaign, dial_due_contacts, reap_stale_calls
from callingbot.services.distributors import import_distributors_csv, normalize_arn
from callingbot.services.lifecycle import apply_status_update, create_call
from callingbot.services.reporting import campaign_stats
from callingbot.settings import Settings, get_settings
from callingbot.telephony.base import CallStatusUpdate
from callingbot.timeutil import to_local, utcnow

log = logging.getLogger("callingbot.cli")

DEMO_ARN = "ARN-000000"
DEMO_PHONE = "+919000000000"
LEAD_STATUSES = (
    EmpanelmentStatus.INTERESTED,
    EmpanelmentStatus.LINK_SENT,
    EmpanelmentStatus.CALLBACK_SCHEDULED,
)
LEAD_COLUMNS = (
    "arn",
    "name",
    "firm_name",
    "phone",
    "email",
    "city",
    "status",
    "last_call_outcome",
    "last_call_summary",
    "last_call_at",
)
MAX_ERRORS_SHOWN = 20
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})
# Which YAML file each knowledge model comes from, to point config errors at the right file.
_MODEL_FILES = {
    "AMCProfile": "amc.yaml",
    "LanguageOption": "amc.yaml",
    "NFOInfo": "nfo.yaml",
    "FAQ": "faq.yaml",
    "CampaignPolicy": "campaign.yaml",
}


class CLIError(Exception):
    """A user-facing error: printed without a traceback, exit code 1."""


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _open_db(settings: Settings) -> None:
    try:
        db.configure_engine(settings.database_url)
        db.init_db()
    except (SQLAlchemyError, ImportError) as exc:  # bad URL, unreachable server, missing driver
        raise CLIError(f"cannot open database {_safe_url(settings.database_url)}: {exc}") from exc


def _safe_url(url: str) -> str:
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # an unparseable URL fails later in configure_engine with a clear error
        return url


def _load_kb(settings: Settings) -> KnowledgeBase:
    """Load the knowledge base, turning every failure into a readable :class:`CLIError`."""
    try:
        return load_knowledge(settings.config_dir)
    except FileNotFoundError as exc:
        raise CLIError(f"configuration file not found: {exc.filename}") from exc
    except ValidationError as exc:
        where = _MODEL_FILES.get(exc.title, exc.title)
        lines = [f"invalid configuration in {Path(settings.config_dir) / where}:"]
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ())) or "(file)"
            lines.append(f"  - {loc}: {err.get('msg')}")
        raise CLIError("\n".join(lines)) from exc
    except (ValueError, TypeError, yaml.YAMLError) as exc:
        raise CLIError(f"invalid configuration in {settings.config_dir}: {exc}") from exc


def _find_campaign(session: Session, name: str) -> Campaign | None:
    campaign = session.scalar(select(Campaign).where(Campaign.name == name))
    if campaign is None:
        # Forgive capitalisation ("nfo launch") when it is unambiguous.
        matches = session.scalars(select(Campaign).where(func.lower(Campaign.name) == name.lower())).all()
        campaign = matches[0] if len(matches) == 1 else None
    return campaign


def _require_campaign(session: Session, name: str) -> Campaign:
    campaign = _find_campaign(session, name)
    if campaign is None:
        known = session.scalars(select(Campaign.name).order_by(Campaign.id)).all()
        hint = (
            f" Known campaigns: {', '.join(known)}."
            if known
            else " Create it with 'callingbot campaign create'."
        )
        raise CLIError(f"unknown campaign {name!r}.{hint}")
    return campaign


def _local(dt, settings: Settings, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return to_local(dt, settings.timezone).strftime(fmt) if dt else "-"


def _nonzero(counts: dict[str, int]) -> str:
    items = [f"{k} {v}" for k, v in counts.items() if v]
    return ", ".join(items) if items else "none"


def _csv_safe(value) -> str:
    # Spreadsheet formula injection: a cell starting with = + - @ is executed by Excel. Free-text
    # fields (names, call summaries written by the LLM) are neutralised with a leading quote.
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


# ---------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------


def cmd_init_db(args: argparse.Namespace, settings: Settings) -> int:
    _open_db(settings)
    print(f"Database ready: {_safe_url(settings.database_url)}")
    return 0


def _config_warnings(settings: Settings, kb: KnowledgeBase) -> list[str]:
    from callingbot.telephony.exotel import ExotelProvider
    from callingbot.telephony.twilio import TwilioProvider, describe_settings, missing_settings

    warnings: list[str] = []
    defaults = Settings.model_fields
    if settings.app_env == "prod":
        if settings.admin_password == defaults["admin_password"].default:
            warnings.append("ADMIN_PASSWORD is still the default; set a strong password before going live.")
        if settings.secret_key == defaults["secret_key"].default:
            warnings.append(
                "SECRET_KEY is still the default; tracking links can be forged. Set a long random value."
            )
        if "example" in kb.amc.empanelment_url_template or "example" in kb.amc.website:
            warnings.append("config/amc.yaml still contains placeholder (example.*) URLs.")
    if settings.llm_provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        warnings.append(
            "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set in the process environment "
            "(the Anthropic SDK does not read .env). Export it, or use LLM_PROVIDER=fake for offline demos."
        )
    for provider, cls in (("twilio", TwilioProvider), ("exotel", ExotelProvider)):
        if settings.telephony_provider == provider:
            missing = missing_settings(settings, cls.required_settings)
            if missing:
                warnings.append(
                    f"TELEPHONY_PROVIDER={provider} but settings are missing: {describe_settings(missing)}."
                )
    host = (urlsplit(settings.base_url).hostname or "").lower()
    if settings.telephony_provider != "simulator" and host in _LOCAL_HOSTS:
        warnings.append(
            f"PUBLIC_BASE_URL is {settings.base_url}, which {settings.telephony_provider} cannot reach; "
            "set it to a public HTTPS URL (e.g. an ngrok tunnel)."
        )
    try:
        from callingbot.messaging import build_messenger

        build_messenger(settings)
    except ValueError as exc:
        warnings.append(str(exc))
    today = to_local(utcnow(), settings.timezone).date()
    if kb.nfo.nfo_close_date < today:
        warnings.append(f"The NFO closed on {kb.nfo.nfo_close_date:%d %b %Y}; update config/nfo.yaml.")
    return warnings


def cmd_check_config(args: argparse.Namespace, settings: Settings) -> int:
    kb = _load_kb(settings)
    _open_db(settings)
    amc, nfo, policy = kb.amc, kb.nfo, kb.campaign
    days = ", ".join(_WEEKDAYS[d] for d in sorted(policy.calling_days))
    languages = ", ".join(f"{lang.code} ({lang.name})" for lang in amc.languages)
    print(f"Configuration loaded from {settings.config_dir} (APP_ENV={settings.app_env})")
    print(f"  AMC:        {amc.name} ({amc.short_name}), SEBI reg. {amc.sebi_registration or '-'}")
    print(f"  Assistant:  {amc.bot_name}; languages {languages}; default {amc.default_language}")
    print(f"  NFO:        {nfo.scheme_name} - {nfo.category}, riskometer {nfo.riskometer}")
    print(f"  NFO dates:  opens {nfo.nfo_open_date:%d %b %Y}, closes {nfo.nfo_close_date:%d %b %Y}")
    print(f"  FAQs:       {len(kb.faqs)}")
    print(
        f"  Calling:    {days} {policy.window_start:%H:%M}-{policy.window_end:%H:%M} ({settings.timezone}), "
        f"{len(policy.holidays)} holiday(s)"
    )
    print(
        f"  Dialling:   max {policy.max_attempts} attempt(s), backoff "
        f"{', '.join(str(m) for m in policy.retry_backoff_minutes)} min, "
        f"{policy.max_concurrent_calls} concurrent, {policy.calls_per_minute}/min"
    )
    print(
        f"  Providers:  telephony={settings.telephony_provider}, llm={settings.llm_provider} "
        f"({settings.llm_model}), sms={settings.sms_provider}, whatsapp={settings.whatsapp_provider}, "
        f"email={settings.email_provider}"
    )
    print(f"  Database:   {_safe_url(settings.database_url)}")
    warnings = _config_warnings(settings, kb)
    if warnings:
        print(f"{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  WARNING: {w}")
    else:
        print("No problems found.")
    return 0


def cmd_import_distributors(args: argparse.Namespace, settings: Settings) -> int:
    path = Path(args.path)
    if not path.is_file():
        raise CLIError(f"file not found: {path}")
    _open_db(settings)
    with db.session_scope() as session:
        try:
            report = import_distributors_csv(
                session, path, source=args.source or path.name, update_existing=not args.no_update
            )
        except (UnicodeDecodeError, csv.Error) as exc:
            raise CLIError(f"could not read {path} as a UTF-8 CSV file: {exc}") from exc
        print(
            f"Imported {path.name}: {report.created} created, {report.updated} updated, "
            f"{report.skipped} skipped, {len(report.errors)} error(s), {report.dnc_marked} marked do-not-call"
        )
        for title, rows in (("Errors (rows not imported)", report.errors), ("Warnings", report.warnings)):
            if not rows:
                continue
            print(f"{title}:")
            for row_no, reason in rows[:MAX_ERRORS_SHOWN]:
                print(f"  row {row_no}: {reason}")
            if len(rows) > MAX_ERRORS_SHOWN:
                print(f"  ... and {len(rows) - MAX_ERRORS_SHOWN} more")

        if args.campaign:
            campaign = _find_campaign(session, args.campaign)
            if campaign is None:
                campaign = Campaign(name=args.campaign, status=CampaignStatus.DRAFT)
                session.add(campaign)
                session.flush()
                print(f"Created campaign {campaign.name!r} (draft).")
            added = add_distributors_to_campaign(session, campaign, report.distributor_ids)
            print(f"Added {added} distributor(s) to campaign {campaign.name!r} ({campaign.status.value}).")
            if campaign.status != CampaignStatus.ACTIVE:
                print(f"Start dialling with: callingbot campaign start {campaign.name!r}")
    return 0


def cmd_campaign(args: argparse.Namespace, settings: Settings) -> int:
    _open_db(settings)
    action = args.campaign_action
    with db.session_scope() as session:
        if action == "list":
            return _campaign_list(session, settings)
        if action == "create":
            if _find_campaign(session, args.name) is not None:
                raise CLIError(f"campaign {args.name!r} already exists")
            campaign = Campaign(name=args.name, description=args.description, status=CampaignStatus.DRAFT)
            session.add(campaign)
            session.flush()
            print(f"Created campaign {campaign.name!r} (draft).")
            return 0

        campaign = _require_campaign(session, args.name)
        if action == "add":
            added = add_distributors_to_campaign(session, campaign)
            total = session.scalar(
                select(func.count(CampaignContact.id)).where(CampaignContact.campaign_id == campaign.id)
            )
            print(f"Added {added} eligible distributor(s) to {campaign.name!r}; {total} contact(s) in total.")
        elif action == "start":
            campaign.status = CampaignStatus.ACTIVE
            campaign.started_at = campaign.started_at or utcnow()
            campaign.completed_at = None
            pending = session.scalar(
                select(func.count(CampaignContact.id)).where(
                    CampaignContact.campaign_id == campaign.id, CampaignContact.state == ContactState.PENDING
                )
            )
            print(f"Campaign {campaign.name!r} is active ({pending} pending contact(s)).")
            if not pending:
                print(
                    "No pending contacts: add some with 'callingbot campaign add' or import with --campaign."
                )
        elif action == "pause":
            campaign.status = CampaignStatus.PAUSED
            print(f"Campaign {campaign.name!r} is paused; no new calls will be placed.")
    return 0


def _campaign_list(session: Session, settings: Settings) -> int:
    campaigns = session.scalars(select(Campaign).order_by(Campaign.id)).all()
    if not campaigns:
        print("No campaigns. Create one with: callingbot campaign create NAME")
        return 0
    counts: dict[int, dict[str, int]] = {}
    for campaign_id, state, n in session.execute(
        select(CampaignContact.campaign_id, CampaignContact.state, func.count(CampaignContact.id)).group_by(
            CampaignContact.campaign_id, CampaignContact.state
        )
    ):
        counts.setdefault(campaign_id, {})[ContactState(state).value] = n
    width = max(len(c.name) for c in campaigns)
    print(f"{'NAME'.ljust(width)}  STATUS     CONTACTS  PENDING  DONE  CREATED")
    for c in campaigns:
        by_state = counts.get(c.id, {})
        print(
            f"{c.name.ljust(width)}  {c.status.value:<9}  {sum(by_state.values()):>8}  "
            f"{by_state.get('pending', 0):>7}  {by_state.get('done', 0):>4}  {_local(c.created_at, settings, '%Y-%m-%d')}"
        )
    return 0


def cmd_run_dialer(args: argparse.Namespace, settings: Settings) -> int:
    from callingbot.telephony import get_provider

    if args.interval <= 0:
        raise CLIError("--interval must be a positive number of seconds")
    if settings.telephony_provider == "simulator" and not args.once:
        raise CLIError(
            "TELEPHONY_PROVIDER=simulator places no real calls, so a looping dialer would only pile up "
            "calls that never progress. Talk to the bot with the /simulator page or 'callingbot simulate', "
            "or test one dialling pass with --once."
        )
    kb = _load_kb(settings)
    _open_db(settings)
    with db.session_scope() as session:
        name = _require_campaign(session, args.campaign).name
    try:
        provider = get_provider(settings.telephony_provider, settings)
    except ValueError as exc:
        raise CLIError(str(exc)) from exc
    if provider.name == "simulator":
        print(
            "Note: simulated calls are recorded but never answered; use the /simulator page to talk to the bot."
        )

    rounds = 0
    try:
        while True:
            rounds += 1
            with db.session_scope() as session:
                campaign = _require_campaign(session, name)
                reaped = reap_stale_calls(session, kb=kb, settings=settings, now_utc=utcnow())
                report = dial_due_contacts(
                    session, campaign=campaign, provider=provider, kb=kb, settings=settings, now_utc=utcnow()
                )
                active = campaign.status == CampaignStatus.ACTIVE
            line = (
                f"[{_local(utcnow(), settings, '%Y-%m-%d %H:%M:%S')}] round {rounds}: placed {report.placed}, "
                f"skipped {report.skipped}, failed {report.failed}"
            )
            if reaped:
                line += f", reaped {reaped} stale call(s)"
            if report.messages:
                line += " - " + " ".join(report.messages)
            print(line, flush=True)
            if not active:
                print(f"Campaign {name!r} is not active; dialer stopped.")
                return 1 if rounds == 1 else 0
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nDialer stopped.")
        return 0
    finally:
        close: Callable[[], None] | None = getattr(provider, "close", None)
        if callable(close):
            close()


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


def _print_bot(response) -> None:
    for text in response.say:
        print(f"BOT: {text}")
    if response.action == "transfer":
        print(f"(The bot transfers the call to {response.transfer_to or 'a relationship manager'}.)")
    elif response.action == "hangup":
        print("(The bot hung up.)")


def cmd_simulate(args: argparse.Namespace, settings: Settings) -> int:
    try:
        from callingbot.agent.engine import ConversationEngine
    except ImportError as exc:
        raise CLIError(f"the conversation engine is not available: {exc}") from exc
    from callingbot.agent.llm import build_llm
    from callingbot.messaging import build_messenger

    kb = _load_kb(settings)
    codes = [lang.code for lang in kb.amc.languages]
    if args.language and args.language not in codes:
        raise CLIError(f"language {args.language!r} is not enabled; choose one of: {', '.join(codes)}")
    try:
        llm = build_llm(settings)
        messenger = build_messenger(settings)
    except Exception as exc:  # missing API key / messaging credentials: explain, don't trace back
        raise CLIError(f"could not start the conversation engine: {exc}") from exc

    _open_db(settings)
    with db.session_scope() as session:
        if args.arn:
            arn = normalize_arn(args.arn)
            if arn is None:
                raise CLIError(f"invalid ARN {args.arn!r}")
            distributor = session.scalar(select(Distributor).where(Distributor.arn == arn))
            if distributor is None:
                raise CLIError(
                    f"no distributor with ARN {arn}; import one or omit --arn to use the demo distributor"
                )
        else:
            distributor = _demo_distributor(session)
        call = create_call(session, distributor=distributor, provider="simulator", language=args.language)
        apply_status_update(
            session,
            call,
            CallStatusUpdate(provider_call_id=None, status=CallStatus.IN_PROGRESS, answered_by="human"),
            kb=kb,
            now_utc=utcnow(),
        )
        session.commit()
        print(
            f"Simulated call #{call.id} to {distributor.name} ({distributor.arn}) in {call.language}. "
            "Press Enter to stay silent, type /quit to hang up."
        )

        engine = ConversationEngine(session=session, kb=kb, settings=settings, llm=llm, messenger=messenger)
        response = engine.start(call, answered_by="human")
        _print_bot(response)
        while response.action == "gather":
            try:
                text = input("YOU: ")
            except (EOFError, KeyboardInterrupt):
                text = "/quit"
            if text.strip().lower() == "/quit":
                print("(You hung up.)")
                break
            response = engine.handle_input(call, text.strip() or None)
            _print_bot(response)

        apply_status_update(
            session,
            call,
            CallStatusUpdate(provider_call_id=call.provider_call_id, status=CallStatus.COMPLETED),
            kb=kb,
            now_utc=utcnow(),
        )
        session.commit()
        _print_call_summary(session, call, distributor)
    return 0


def _print_call_summary(session: Session, call: Call, distributor: Distributor) -> None:
    print("--- Call summary ---")
    print(
        f"Call #{call.id}: {CallStatus(call.status).value}, {call.turn_count} distributor turn(s), "
        f"{call.duration_seconds if call.duration_seconds is not None else '-'}s"
    )
    print(f"Outcome: {call.outcome.value if call.outcome else '-'}")
    print(
        f"Distributor: {distributor.name} ({distributor.arn}) - status {EmpanelmentStatus(distributor.status).value}"
    )
    if call.summary:
        print(f"Notes: {call.summary}")
    messages = session.scalars(
        select(OutboundMessage).where(OutboundMessage.call_id == call.id).order_by(OutboundMessage.id)
    ).all()
    if messages:
        print("Messages in the outbox:")
        for m in messages:
            dest = m.destination if "@" in m.destination else mask_phone(m.destination)
            link = f" link: {m.link}" if m.link else ""
            print(f"  {m.channel.value} to {dest} [{m.status.value}]{link}")
    else:
        print("Messages in the outbox: none")
    callbacks = session.scalars(
        select(Callback).where(Callback.call_id == call.id, Callback.status == CallbackStatus.PENDING)
    ).all()
    for cb in callbacks:
        who = "RM" if cb.with_rm else "bot"
        print(f"Callback ({who}) scheduled for {cb.scheduled_for:%Y-%m-%d %H:%M} UTC")


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    _open_db(settings)
    uvicorn.run(
        "callingbot.web.app:create_app", factory=True, host=args.host, port=args.port, reload=args.reload
    )
    return 0


def cmd_stats(args: argparse.Namespace, settings: Settings) -> int:
    _open_db(settings)
    with db.session_scope() as session:
        campaign = _require_campaign(session, args.campaign) if args.campaign else None
        stats = campaign_stats(session, campaign.id if campaign else None)
        title = f"Campaign {campaign.name!r} ({campaign.status.value})" if campaign else "All campaigns"
    print(title)
    print(f"Distributors: {stats['distributors_total']} ({_nonzero(stats['by_status'])})")
    print(
        f"Calls: {stats['calls_total']}, connected {stats['connected']} ({stats['connect_rate']:.0%}), "
        f"average duration {stats['avg_call_duration_seconds']:.0f}s"
    )
    print(f"  by status:  {_nonzero(stats['calls_by_status'])}")
    print(f"  by outcome: {_nonzero(stats['calls_by_outcome'])}")
    print(
        f"Links sent: {stats['links_sent']}, link clicks: {stats['link_clicks']}, "
        f"callbacks pending: {stats['callbacks_pending']}, opt-outs: {stats['opt_outs']}"
    )
    if "contacts_by_state" in stats:
        print(f"Contacts: {_nonzero(stats['contacts_by_state'])}")
    print("Funnel:")
    width = max(len(label) for label, _ in stats["funnel"])
    for label, count in stats["funnel"]:
        print(f"  {label.ljust(width)}  {count}")
    return 0


def cmd_export_leads(args: argparse.Namespace, settings: Settings) -> int:
    path = Path(args.path)
    if not path.parent.exists():
        raise CLIError(f"directory not found: {path.parent}")
    _open_db(settings)
    with db.session_scope() as session:
        stmt = select(Distributor).where(Distributor.status.in_(LEAD_STATUSES)).order_by(Distributor.id)
        campaign = None
        if args.campaign:
            campaign = _require_campaign(session, args.campaign)
            members = select(CampaignContact.distributor_id).where(CampaignContact.campaign_id == campaign.id)
            stmt = stmt.where(Distributor.id.in_(members))
        leads = session.scalars(stmt).all()
        rows = []
        for d in leads:
            call_stmt = select(Call).where(Call.distributor_id == d.id)
            if campaign is not None:
                call_stmt = call_stmt.where(Call.campaign_id == campaign.id)
            last = session.scalar(call_stmt.order_by(Call.created_at.desc(), Call.id.desc()).limit(1))
            last_at = (last.ended_at or last.created_at) if last else None
            rows.append(
                {
                    "arn": d.arn,
                    "name": _csv_safe(d.name),
                    "firm_name": _csv_safe(d.firm_name),
                    "phone": d.phone,
                    "email": _csv_safe(d.email),
                    "city": _csv_safe(d.city),
                    "status": EmpanelmentStatus(d.status).value,
                    "last_call_outcome": last.outcome.value if last and last.outcome else "",
                    "last_call_summary": _csv_safe(last.summary if last else None),
                    "last_call_at": to_local(last_at, settings.timezone).isoformat(timespec="seconds")
                    if last_at
                    else "",
                }
            )
    # utf-8-sig so Excel (what the RM team opens it in) shows Hindi names correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=LEAD_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Exported {len(rows)} lead(s) to {path}")
    return 0


# ---------------------------------------------------------------------------------------------
# Parser and entry point
# ---------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="callingbot", description="AI voice bot for mutual fund distributor outreach and empanelment."
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("init-db", help="create the database tables (safe to run again)")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("check-config", help="validate settings and config/*.yaml")
    p.set_defaults(func=cmd_check_config)

    p = sub.add_parser("import-distributors", help="import a distributor CSV")
    p.add_argument("path", help="CSV file (AMFI export or CRM list)")
    p.add_argument("--source", help="label for where the list came from (default: the file name)")
    p.add_argument(
        "--campaign", help="also add the imported distributors to this campaign (created if missing)"
    )
    p.add_argument(
        "--no-update", action="store_true", help="leave existing distributors (same ARN) unchanged"
    )
    p.set_defaults(func=cmd_import_distributors)

    p = sub.add_parser("campaign", help="create, fill, start, pause or list campaigns")
    csub = p.add_subparsers(dest="campaign_action", metavar="ACTION")
    cp = csub.add_parser("create", help="create a campaign (draft)")
    cp.add_argument("name")
    cp.add_argument("--description")
    for action, help_text in (
        ("add", "add all eligible distributors"),
        ("start", "start dialling"),
        ("pause", "stop placing new calls"),
    ):
        cp = csub.add_parser(action, help=help_text)
        cp.add_argument("name")
    csub.add_parser("list", help="list campaigns")
    p.set_defaults(func=cmd_campaign, _subparser=p)

    p = sub.add_parser("run-dialer", help="dial due contacts of an active campaign")
    p.add_argument("--campaign", required=True)
    p.add_argument("--once", action="store_true", help="do one dialling pass and exit")
    p.add_argument("--interval", type=float, default=30.0, help="seconds between passes (default 30)")
    p.set_defaults(func=cmd_run_dialer)

    p = sub.add_parser("simulate", help="talk to the bot in the terminal")
    p.add_argument("--arn", help="play this distributor (default: a demo distributor)")
    p.add_argument("--language", help="language code, e.g. en-IN or hi-IN")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("stats", help="print funnel and call statistics")
    p.add_argument("--campaign")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("export-leads", help="export interested / link-sent / callback leads to CSV")
    p.add_argument("path")
    p.add_argument("--campaign")
    p.set_defaults(func=cmd_export_leads)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse usage error or --help
        return exc.code if isinstance(exc.code, int) else 2
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    if args.command == "campaign" and not args.campaign_action:
        args._subparser.print_help()
        return 2

    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"Error: invalid settings (environment / .env):\n{exc}", file=sys.stderr)
        return 1
    logging.basicConfig(
        level=getattr(logging, str(settings.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args, settings)
    except CLIError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

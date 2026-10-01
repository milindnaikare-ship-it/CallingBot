"""Tests for callingbot.cli, run through ``main([...])`` against a temporary SQLite file."""

from __future__ import annotations

import csv
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import IN_WINDOW_UTC, ROOT
from sqlalchemy import func, select

from callingbot import cli, db
from callingbot.models import (
    Call,
    CallOutcome,
    CallStatus,
    Campaign,
    CampaignContact,
    CampaignStatus,
    ContactState,
    Distributor,
    EmpanelmentStatus,
    OutboundMessage,
)
from callingbot.settings import get_settings
from callingbot.telephony import SimulatorProvider

SAMPLE = ROOT / "data" / "sample_distributors.csv"

_ENV_TO_CLEAR = (
    "ANTHROPIC_API_KEY",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "EXOTEL_ACCOUNT_SID",
    "EXOTEL_API_KEY",
    "EXOTEL_API_TOKEN",
    "EXOTEL_CALLER_ID",
    "EXOTEL_APP_ID",
    "SMS_PROVIDER",
    "WHATSAPP_PROVIDER",
    "EMAIL_PROVIDER",
    "ADMIN_PASSWORD",
    "SECRET_KEY",
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Isolated settings: temp SQLite file, repo config, offline providers, fixed clock."""
    monkeypatch.chdir(tmp_path)  # no stray .env from the developer's checkout
    for var in _ENV_TO_CLEAR:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("TELEPHONY_PROVIDER", "simulator")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://bot.example.test")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setattr(cli, "utcnow", lambda: IN_WINDOW_UTC)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    db.get_engine().dispose()
    db.configure_engine("sqlite://")


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def query(fn):
    """Run ``fn(session)`` against the CLI's database in a fresh session."""
    session = db.new_session()
    try:
        return fn(session)
    finally:
        session.close()


# --------------------------------------------------------------------------------------------
# Basics
# --------------------------------------------------------------------------------------------


def test_init_db_creates_database(env, capsys):
    code, out, _ = run(capsys, "init-db")
    assert code == 0
    assert "Database ready" in out
    assert (env / "cli.db").exists()
    assert run(capsys, "init-db")[0] == 0  # idempotent


def test_no_command_and_usage_errors(env, capsys):
    assert cli.main([]) == 2
    assert cli.main(["campaign"]) == 2
    assert cli.main(["no-such-command"]) == 2
    assert cli.main(["run-dialer"]) == 2  # --campaign is required
    capsys.readouterr()


def test_invalid_settings_reported(env, capsys, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bogus")
    get_settings.cache_clear()
    code, _, err = run(capsys, "init-db")
    assert code == 1
    assert "invalid settings" in err


# --------------------------------------------------------------------------------------------
# import-distributors
# --------------------------------------------------------------------------------------------


def test_import_with_campaign(env, capsys):
    code, out, _ = run(capsys, "import-distributors", str(SAMPLE), "--campaign", "NFO Launch")
    assert code == 0
    assert "10 created, 0 updated, 0 skipped, 2 error(s)" in out
    assert "row 6:" in out and "row 13: duplicate ARN" in out
    assert "Created campaign 'NFO Launch' (draft)" in out
    assert "Added 10 distributor(s)" in out

    def check(s):
        campaign = s.scalar(select(Campaign).where(Campaign.name == "NFO Launch"))
        assert campaign.status == CampaignStatus.DRAFT
        members = s.scalar(
            select(func.count(CampaignContact.id)).where(CampaignContact.campaign_id == campaign.id)
        )
        assert members == 10
        assert (
            s.scalar(select(Distributor.source).where(Distributor.arn == "ARN-999901"))
            == "sample_distributors.csv"
        )

    query(check)


def test_import_source_and_no_update(env, capsys, tmp_path):
    first = tmp_path / "a.csv"
    first.write_text("ARN,Name,Mobile\nARN-1,Old,9811111111\n", encoding="utf-8")
    second = tmp_path / "b.csv"
    second.write_text("ARN,Name,Mobile\nARN-1,New,9811111111\nARN-2,Two,9811111112\n", encoding="utf-8")
    assert run(capsys, "import-distributors", str(first), "--source", "crm_2026_10")[0] == 0
    code, out, _ = run(capsys, "import-distributors", str(second), "--no-update")
    assert code == 0
    assert "1 created, 0 updated, 1 skipped" in out
    names = query(lambda s: dict(s.execute(select(Distributor.arn, Distributor.name)).all()))
    assert names == {"ARN-1": "Old", "ARN-2": "Two"}
    assert (
        query(lambda s: s.scalar(select(Distributor.source).where(Distributor.arn == "ARN-1")))
        == "crm_2026_10"
    )


def test_import_missing_file(env, capsys):
    code, _, err = run(capsys, "import-distributors", "nope.csv")
    assert code == 1
    assert "file not found" in err


def test_import_shows_only_first_20_errors(env, capsys, tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("ARN,Name,Mobile\n" + "".join(f"ARN-{i},N{i},123\n" for i in range(25)), encoding="utf-8")
    code, out, _ = run(capsys, "import-distributors", str(path))
    assert code == 0
    assert "25 error(s)" in out
    assert out.count("no valid Indian mobile") == 20
    assert "... and 5 more" in out


# --------------------------------------------------------------------------------------------
# campaign
# --------------------------------------------------------------------------------------------


def test_campaign_lifecycle(env, capsys):
    assert run(capsys, "import-distributors", str(SAMPLE))[0] == 0
    code, out, _ = run(capsys, "campaign", "create", "Pilot", "--description", "First pilot")
    assert code == 0 and "Created campaign 'Pilot'" in out
    code, _, err = run(capsys, "campaign", "create", "Pilot")
    assert code == 1 and "already exists" in err

    code, out, _ = run(capsys, "campaign", "add", "Pilot")
    assert code == 0 and "Added 10 eligible distributor(s)" in out
    code, out, _ = run(capsys, "campaign", "add", "pilot")  # case-insensitive lookup
    assert code == 0 and "Added 0" in out

    code, out, _ = run(capsys, "campaign", "start", "Pilot")
    assert code == 0 and "is active (10 pending" in out
    assert query(lambda s: s.scalar(select(Campaign.status))) == CampaignStatus.ACTIVE
    assert query(lambda s: s.scalar(select(Campaign.started_at))) == IN_WINDOW_UTC

    code, out, _ = run(capsys, "campaign", "list")
    assert code == 0
    assert "Pilot" in out and "active" in out and "10" in out

    code, out, _ = run(capsys, "campaign", "pause", "Pilot")
    assert code == 0 and "paused" in out
    assert query(lambda s: s.scalar(select(Campaign.status))) == CampaignStatus.PAUSED


def test_campaign_unknown(env, capsys):
    for action in ("add", "start", "pause"):
        code, _, err = run(capsys, "campaign", action, "Ghost")
        assert code == 1 and "unknown campaign 'Ghost'" in err


def test_campaign_list_empty(env, capsys):
    code, out, _ = run(capsys, "campaign", "list")
    assert code == 0 and "No campaigns" in out


# --------------------------------------------------------------------------------------------
# run-dialer
# --------------------------------------------------------------------------------------------


def test_run_dialer_refuses_simulator_loop(env, capsys):
    code, _, err = run(capsys, "run-dialer", "--campaign", "Anything")
    assert code == 1
    assert "/simulator" in err and "--once" in err


def test_run_dialer_once_with_simulator(env, capsys):
    run(capsys, "import-distributors", str(SAMPLE), "--campaign", "NFO Launch")
    run(capsys, "campaign", "start", "NFO Launch")
    code, out, _ = run(capsys, "run-dialer", "--campaign", "NFO Launch", "--once")
    assert code == 0
    assert "round 1: placed 3" in out  # max_concurrent_calls = 3
    assert query(lambda s: s.scalar(select(func.count(Call.id)))) == 3


def test_run_dialer_unknown_or_inactive_campaign(env, capsys):
    code, _, err = run(capsys, "run-dialer", "--campaign", "Ghost", "--once")
    assert code == 1 and "unknown campaign" in err
    run(capsys, "campaign", "create", "Draft")
    code, out, _ = run(capsys, "run-dialer", "--campaign", "Draft", "--once")
    assert code == 1
    assert "not active" in out


def test_run_dialer_loops_until_paused(env, capsys, monkeypatch):
    run(capsys, "import-distributors", str(SAMPLE), "--campaign", "NFO Launch")
    run(capsys, "campaign", "start", "NFO Launch")
    monkeypatch.setenv("TELEPHONY_PROVIDER", "twilio")
    get_settings.cache_clear()
    provider = SimulatorProvider()
    monkeypatch.setattr("callingbot.telephony.get_provider", lambda name, settings: provider)
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            cli.main(["campaign", "pause", "NFO Launch"])

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    code, out, _ = run(capsys, "run-dialer", "--campaign", "NFO Launch", "--interval", "5")
    assert code == 0
    assert sleeps == [5.0, 5.0]
    assert "round 1: placed 3" in out
    assert "round 3:" in out and "not active" in out
    assert "dialer stopped" in out


def test_run_dialer_ctrl_c_exits_cleanly(env, capsys, monkeypatch):
    run(capsys, "campaign", "create", "Live")
    run(capsys, "campaign", "start", "Live")
    monkeypatch.setenv("TELEPHONY_PROVIDER", "twilio")
    get_settings.cache_clear()
    monkeypatch.setattr("callingbot.telephony.get_provider", lambda name, settings: SimulatorProvider())

    def interrupt(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", interrupt)
    code, out, _ = run(capsys, "run-dialer", "--campaign", "Live")
    assert code == 0
    assert "Dialer stopped" in out


def test_run_dialer_survives_transient_database_error(env, capsys, monkeypatch):
    from sqlalchemy.exc import OperationalError

    run(capsys, "campaign", "create", "Live")
    run(capsys, "campaign", "start", "Live")
    monkeypatch.setenv("TELEPHONY_PROVIDER", "twilio")
    get_settings.cache_clear()
    monkeypatch.setattr("callingbot.telephony.get_provider", lambda name, settings: SimulatorProvider())
    real_dial = cli.dial_due_contacts
    attempts = []

    def flaky_dial(session, **kw):
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError("UPDATE ...", {}, Exception("database is locked"))
        return real_dial(session, **kw)

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "dial_due_contacts", flaky_dial)
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    code, out, _ = run(capsys, "run-dialer", "--campaign", "Live")
    assert code == 0
    assert "round 1: database error, will retry" in out
    assert "round 2: placed 0" in out
    assert len(attempts) == 2


def test_run_dialer_reports_missing_provider_settings(env, capsys, monkeypatch):
    run(capsys, "campaign", "create", "Live")
    monkeypatch.setenv("TELEPHONY_PROVIDER", "twilio")
    get_settings.cache_clear()
    code, _, err = run(capsys, "run-dialer", "--campaign", "Live", "--once")
    assert code == 1
    assert "TWILIO_ACCOUNT_SID" in err


# --------------------------------------------------------------------------------------------
# stats / export-leads
# --------------------------------------------------------------------------------------------


def _seed_leads(capsys):
    run(capsys, "import-distributors", str(SAMPLE), "--campaign", "NFO Launch")
    run(capsys, "campaign", "create", "Other")

    def seed(s):
        campaign = s.scalar(select(Campaign).where(Campaign.name == "NFO Launch"))
        d1 = s.scalar(select(Distributor).where(Distributor.arn == "ARN-999901"))
        d2 = s.scalar(select(Distributor).where(Distributor.arn == "ARN-999902"))
        d3 = s.scalar(select(Distributor).where(Distributor.arn == "ARN-999903"))
        d1.status = EmpanelmentStatus.INTERESTED
        d2.status = EmpanelmentStatus.LINK_SENT
        d3.status = EmpanelmentStatus.NOT_INTERESTED
        outsider = Distributor(
            arn="ARN-5", name="=cmd()", phone="+919855555555", status=EmpanelmentStatus.CALLBACK_SCHEDULED
        )
        s.add(outsider)
        s.flush()
        s.add_all(
            [
                Call(
                    distributor_id=d1.id,
                    campaign_id=campaign.id,
                    provider="simulator",
                    status=CallStatus.COMPLETED,
                    outcome=CallOutcome.NO_OUTCOME,
                    created_at=IN_WINDOW_UTC - timedelta(days=1),
                    ended_at=IN_WINDOW_UTC - timedelta(days=1),
                ),
                Call(
                    distributor_id=d1.id,
                    campaign_id=campaign.id,
                    provider="simulator",
                    status=CallStatus.COMPLETED,
                    outcome=CallOutcome.INTERESTED,
                    summary="+wants RM visit",
                    answered_by="human",
                    answered_at=IN_WINDOW_UTC,
                    duration_seconds=90,
                    created_at=IN_WINDOW_UTC,
                    ended_at=IN_WINDOW_UTC + timedelta(minutes=2),
                ),
            ]
        )
        s.commit()

    query(seed)


def test_stats(env, capsys):
    _seed_leads(capsys)
    code, out, _ = run(capsys, "stats")
    assert code == 0
    assert "All campaigns" in out
    assert "Distributors: 11" in out
    assert "Calls: 2, connected 1 (50%)" in out
    assert "Funnel:" in out and "Interested" in out
    code, out, _ = run(capsys, "stats", "--campaign", "NFO Launch")
    assert code == 0
    assert "Campaign 'NFO Launch' (draft)" in out
    assert "Distributors: 10" in out
    assert "Contacts: pending 10" in out
    code, _, err = run(capsys, "stats", "--campaign", "Ghost")
    assert code == 1 and "unknown campaign" in err


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_export_leads(env, capsys):
    _seed_leads(capsys)
    out_path = env / "leads.csv"
    code, out, _ = run(capsys, "export-leads", str(out_path))
    assert code == 0 and "Exported 3 lead(s)" in out
    rows = _read_csv(out_path)
    assert list(rows[0]) == list(cli.LEAD_COLUMNS)
    by_arn = {r["arn"]: r for r in rows}
    assert set(by_arn) == {"ARN-999901", "ARN-999902", "ARN-5"}  # not_interested is not a lead
    lead = by_arn["ARN-999901"]
    assert lead["status"] == "interested"
    assert lead["phone"] == "+919000000001"
    assert lead["last_call_outcome"] == "interested"  # the latest call wins
    assert lead["last_call_summary"] == "'+wants RM visit"  # neutralised for spreadsheets
    assert lead["last_call_at"] == "2026-10-13T11:02:00+05:30"
    assert by_arn["ARN-999902"]["last_call_at"] == ""
    assert by_arn["ARN-5"]["name"] == "'=cmd()"


def test_export_leads_campaign_members_only(env, capsys):
    _seed_leads(capsys)
    out_path = env / "leads.csv"
    code, out, _ = run(capsys, "export-leads", str(out_path), "--campaign", "NFO Launch")
    assert code == 0 and "Exported 2 lead(s)" in out
    assert {r["arn"] for r in _read_csv(out_path)} == {"ARN-999901", "ARN-999902"}
    code, out, _ = run(capsys, "export-leads", str(out_path), "--campaign", "Other")
    assert code == 0 and "Exported 0 lead(s)" in out
    assert _read_csv(out_path) == []


def test_export_leads_errors(env, capsys):
    code, _, err = run(capsys, "export-leads", str(env / "missing-dir" / "leads.csv"))
    assert code == 1 and "directory not found" in err
    code, _, err = run(capsys, "export-leads", str(env / "leads.csv"), "--campaign", "Ghost")
    assert code == 1 and "unknown campaign" in err


# --------------------------------------------------------------------------------------------
# check-config
# --------------------------------------------------------------------------------------------


def test_check_config_ok(env, capsys):
    code, out, _ = run(capsys, "check-config")
    assert code == 0
    assert "Sample Mutual Fund" in out
    assert "Sample Flexi Cap Fund" in out
    assert "Mon, Tue, Wed, Thu, Fri, Sat 10:00-19:00 (Asia/Kolkata)" in out
    assert "No problems found." in out


def test_check_config_warnings(env, capsys, monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("TELEPHONY_PROVIDER", "twilio")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
    get_settings.cache_clear()
    code, out, _ = run(capsys, "check-config")
    assert code == 0
    assert "ADMIN_PASSWORD is still the default" in out
    assert "SECRET_KEY is still the default" in out
    assert "ANTHROPIC_API_KEY is not set" in out
    assert "TELEPHONY_PROVIDER=twilio but settings are missing" in out and "TWILIO_AUTH_TOKEN" in out
    assert "PUBLIC_BASE_URL is http://localhost:8000" in out
    assert "placeholder" in out


def test_check_config_quiet_when_configured(env, capsys, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TELEPHONY_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550001111")
    get_settings.cache_clear()
    code, out, _ = run(capsys, "check-config")
    assert code == 0 and "No problems found." in out


def test_check_config_invalid_yaml_values(env, capsys, monkeypatch, tmp_path):
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    (config / "campaign.yaml").write_text('window_start: "19:00"\nwindow_end: "10:00"\n', encoding="utf-8")
    monkeypatch.setenv("CONFIG_DIR", str(config))
    get_settings.cache_clear()
    code, _, err = run(capsys, "check-config")
    assert code == 1
    assert "campaign.yaml" in err
    assert "window_end must be after window_start" in err


def test_check_config_missing_files(env, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "nowhere"))
    get_settings.cache_clear()
    code, _, err = run(capsys, "check-config")
    assert code == 1
    assert "configuration file not found" in err and "amc.yaml" in err


# --------------------------------------------------------------------------------------------
# serve / simulate
# --------------------------------------------------------------------------------------------


def test_serve_runs_uvicorn_factory(env, capsys, monkeypatch):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    assert cli.main(["serve", "--port", "9000", "--reload"]) == 0
    assert calls == [
        (
            ("callingbot.web.app:create_app",),
            {"factory": True, "host": "127.0.0.1", "port": 9000, "reload": True},
        )
    ]


def test_simulate_argument_errors(env, capsys):
    code, _, err = run(capsys, "simulate", "--language", "xx-XX")
    assert code == 1 and "en-IN" in err and "hi-IN" in err
    code, _, err = run(capsys, "simulate", "--arn", "not an arn")
    assert code == 1 and "invalid ARN" in err
    code, _, err = run(capsys, "simulate", "--arn", "424242")
    assert code == 1 and "no distributor with ARN ARN-424242" in err


def _scripted_input(monkeypatch, lines: list[str]) -> list[str]:
    prompts: list[str] = []
    feed = iter(lines)

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(feed, "/quit")

    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def test_simulate_conversation_with_demo_llm(env, capsys, monkeypatch):
    pytest.importorskip("callingbot.agent.engine")
    pytest.importorskip("callingbot.agent.demo_llm")
    prompts = _scripted_input(
        monkeypatch, ["Yes speaking", "", "No, not yet empanelled", "Yes please send it on WhatsApp"]
    )
    code, out, _ = run(capsys, "simulate")
    assert code == 0
    assert prompts and all(p == "YOU: " for p in prompts)
    assert out.count("BOT: ") >= 2
    assert "Am I speaking with Demo Distributor?" in out
    assert "--- Call summary ---" in out

    def check(s):
        d = s.scalar(select(Distributor).where(Distributor.arn == cli.DEMO_ARN))
        assert d.phone == cli.DEMO_PHONE and d.city == "Mumbai"
        call = s.scalar(select(Call).where(Call.distributor_id == d.id))
        assert call.provider == "simulator"
        assert call.status == CallStatus.COMPLETED
        assert call.engine_state.get("finalized") is True
        assert call.turn_count >= 2
        if call.outcome == CallOutcome.LINK_SENT:
            message = s.scalar(select(OutboundMessage).where(OutboundMessage.call_id == call.id))
            assert message is not None and message.link
            assert "link: " in out

    query(check)


def test_simulate_reuses_demo_distributor_and_honours_language(env, capsys, monkeypatch):
    pytest.importorskip("callingbot.agent.engine")
    _scripted_input(monkeypatch, [])
    assert run(capsys, "simulate")[0] == 0
    code, out, _ = run(capsys, "simulate", "--language", "hi-IN")
    assert code == 0
    assert "in hi-IN" in out
    assert "नमस्ते" in out
    assert "(You hung up.)" in out
    count = query(
        lambda s: s.scalar(select(func.count(Distributor.id)).where(Distributor.arn == cli.DEMO_ARN))
    )
    assert count == 1
    statuses = query(lambda s: s.scalars(select(Call.status)).all())
    assert statuses == [CallStatus.COMPLETED, CallStatus.COMPLETED]


def test_simulate_language_override_does_not_change_real_distributor(env, capsys, monkeypatch):
    pytest.importorskip("callingbot.agent.engine")
    run(capsys, "import-distributors", str(SAMPLE))
    _scripted_input(monkeypatch, [])
    code, out, _ = run(capsys, "simulate", "--arn", "ARN-999901", "--language", "hi-IN")
    assert code == 0 and "नमस्ते" in out
    language = query(
        lambda s: s.scalar(select(Distributor.preferred_language).where(Distributor.arn == "ARN-999901"))
    )
    assert language == "en-IN"


def test_simulate_contacts_state_untouched(env, capsys, monkeypatch):
    # A simulated call is not a campaign attempt: campaign contacts are not consumed.
    pytest.importorskip("callingbot.agent.engine")
    run(capsys, "import-distributors", str(SAMPLE), "--campaign", "NFO Launch")
    _scripted_input(monkeypatch, ["Yes speaking"])
    assert run(capsys, "simulate", "--arn", "ARN-999901")[0] == 0
    states = query(lambda s: set(s.scalars(select(CampaignContact.state)).all()))
    attempts = query(lambda s: s.scalar(select(func.sum(CampaignContact.attempts))))
    assert states == {ContactState.PENDING} and attempts == 0

"""Tests for the public routes (/healthz, /r/{token}) and the create_app factory."""

from __future__ import annotations

import pytest
from conftest import IN_WINDOW_UTC
from fastapi.testclient import TestClient
from sqlalchemy import select

from callingbot import db, links
from callingbot.agent.demo_llm import DemoLLM
from callingbot.messaging import Messenger
from callingbot.models import Call, CallStatus, Distributor, EmpanelmentStatus, LinkClick
from callingbot.telephony import SimulatorProvider
from callingbot.web.app import create_app


@pytest.fixture
def app(settings, kb):
    app = create_app(settings, llm=DemoLLM(kb), provider=SimulatorProvider())
    app.state.clock = lambda: IN_WINDOW_UTC
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def distributor_and_call() -> tuple[int, int]:
    with db.new_session() as s:
        d = Distributor(
            arn="ARN-930001", name="Meera Iyer", phone="+919877778888", status=EmpanelmentStatus.LINK_SENT
        )
        s.add(d)
        s.flush()
        call = Call(distributor_id=d.id, provider="simulator", status=CallStatus.COMPLETED)
        s.add(call)
        s.commit()
        return d.id, call.id


def clicks() -> list[LinkClick]:
    with db.new_session() as s:
        return list(s.scalars(select(LinkClick).order_by(LinkClick.id)))


# ------------------------------------------------------------------------------------- /healthz


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_healthz_reports_database_failure(client, monkeypatch):
    def broken():
        raise RuntimeError("database down")

    monkeypatch.setattr(db, "new_session", broken)
    response = client.get("/healthz")
    assert response.status_code == 503 and response.json() == {"status": "unavailable"}


# ------------------------------------------------------------------------------------- /r/{token}


def test_valid_link_redirects_to_empanelment_form_and_records_click(
    client, settings, kb, distributor_and_call
):
    distributor_id, call_id = distributor_and_call
    token = links.make_link_token(settings.secret_key, distributor_id, call_id)
    response = client.get(
        f"/r/{token}", headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) " + "x" * 600}
    )

    assert response.status_code == 302
    assert response.headers["location"] == (
        f"https://partners.sample-mf.example/empanel?arn=ARN-930001&ref=call{call_id}"
    )
    with db.new_session() as s:
        expected = links.empanelment_target_url(kb, s.get(Distributor, distributor_id), call_id)
    assert response.headers["location"] == expected
    (click,) = clicks()
    assert (click.distributor_id, click.call_id) == (distributor_id, call_id)
    assert click.user_agent.startswith("Mozilla/5.0") and len(click.user_agent) == 500


def test_link_without_call_uses_distributor_ref(client, settings, distributor_and_call):
    distributor_id, _ = distributor_and_call
    token = links.make_link_token(settings.secret_key, distributor_id, None)
    response = client.get(f"/r/{token}")
    assert response.status_code == 302 and response.headers["location"].endswith(f"ref=dist{distributor_id}")
    assert clicks()[0].call_id is None


def test_tampered_or_foreign_tokens_are_404(client, settings, distributor_and_call):
    distributor_id, call_id = distributor_and_call
    token = links.make_link_token(settings.secret_key, distributor_id, call_id)
    payload, mac = token.split(".")
    forged_payload = links.make_link_token(settings.secret_key, distributor_id + 1, call_id).split(".")[0]
    for bad in (
        payload + "." + mac[:-1] + ("A" if mac[-1] != "A" else "B"),
        forged_payload + "." + mac,
        links.make_link_token("another-secret", distributor_id, call_id),
        "not-a-token",
        "x" * 300,
    ):
        response = client.get(f"/r/{bad}")
        assert response.status_code == 404, bad
        assert response.headers["content-type"].startswith("text/html")
        assert "This link is not valid" in response.text and "Meera" not in response.text
    assert clicks() == []


def test_token_for_deleted_distributor_is_404(client, settings):
    token = links.make_link_token(settings.secret_key, 4242, None)
    assert client.get(f"/r/{token}").status_code == 404


def test_click_for_a_missing_call_is_still_recorded(client, settings, distributor_and_call):
    distributor_id, _ = distributor_and_call
    token = links.make_link_token(settings.secret_key, distributor_id, 9999)
    response = client.get(f"/r/{token}")
    assert response.status_code == 302 and response.headers["location"].endswith("ref=call9999")
    assert clicks()[0].call_id is None  # no dangling foreign key


@pytest.mark.parametrize(
    "agent",
    [
        "WhatsApp/2.23.20.0 A",
        "facebookexternalhit/1.1 Facebot Twitterbot/1.0",
        "TelegramBot (like TwitterBot)",
    ],
)
def test_link_previews_redirect_but_are_not_counted(client, settings, distributor_and_call, agent):
    distributor_id, call_id = distributor_and_call
    token = links.make_link_token(settings.secret_key, distributor_id, call_id)
    assert client.get(f"/r/{token}", headers={"User-Agent": agent}).status_code == 302
    assert clicks() == []


def test_public_routes_need_no_auth_and_send_security_headers(client, settings, distributor_and_call):
    token = links.make_link_token(settings.secret_key, distributor_and_call[0], None)
    response = client.get(f"/r/{token}")
    assert response.status_code == 302 and response.headers["x-content-type-options"] == "nosniff"
    invalid = client.get("/r/bad")
    assert "Content-Security-Policy" in invalid.headers and invalid.headers["cache-control"] == "no-store"


# ------------------------------------------------------------------------------------- create_app


def test_create_app_wires_state_from_settings(settings):
    app = create_app(settings)
    assert app.state.settings is settings
    assert app.state.llm.model == "demo"  # LLM_PROVIDER=fake
    assert app.state.provider.name == "simulator"
    assert isinstance(app.state.messenger, Messenger)
    assert app.state.kb.amc.default_language == "en-IN"
    client = TestClient(app)
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/simulator.js").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}


def test_create_app_initialises_the_database(settings):
    create_app(settings)
    with db.new_session() as s:
        assert s.scalars(select(Distributor)).all() == []  # tables exist


@pytest.mark.parametrize(
    "overrides,problem",
    [
        ({"admin_password": "change-me"}, "ADMIN_PASSWORD"),
        ({"admin_password": ""}, "ADMIN_PASSWORD"),
        ({"secret_key": "dev-secret-change-me"}, "SECRET_KEY"),
        ({"public_base_url": "http://bot.example.test"}, "PUBLIC_BASE_URL"),
    ],
)
def test_create_app_refuses_unsafe_production_settings(settings, overrides, problem):
    unsafe = settings.model_copy(update={"app_env": "prod", **overrides})
    with pytest.raises(RuntimeError, match=problem):
        create_app(unsafe)


def test_production_app_starts_with_safe_settings_and_hides_api_docs(settings):
    app = create_app(settings.model_copy(update={"app_env": "prod"}))
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_dev_defaults_are_allowed_outside_production(settings):
    dev = settings.model_copy(
        update={"app_env": "dev", "admin_password": "change-me", "public_base_url": "http://x"}
    )
    assert create_app(dev).state.settings.app_env == "dev"

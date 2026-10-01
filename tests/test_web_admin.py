"""Tests for callingbot.web.admin: auth, CSRF, pages, CSV import, campaigns, manual actions, exports."""

from __future__ import annotations

import csv
import html
import io
import re
from datetime import datetime, timedelta

import pytest
from conftest import IN_WINDOW_UTC
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from callingbot import compliance, db
from callingbot.agent.demo_llm import DemoLLM
from callingbot.cli import LEAD_COLUMNS
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
    DNCEntry,
    EmpanelmentStatus,
    MessageChannel,
    MessageStatus,
    OutboundMessage,
    Turn,
    TurnRole,
)
from callingbot.telephony import SimulatorProvider
from callingbot.web.app import create_app

AUTH = ("admin", "test-password")
SAME_ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def provider() -> SimulatorProvider:
    return SimulatorProvider()


@pytest.fixture
def app(settings, kb, provider):
    app = create_app(settings, llm=DemoLLM(kb), provider=provider)
    app.state.clock = lambda: IN_WINDOW_UTC  # Tuesday 11:00 IST: inside the calling window
    return app


@pytest.fixture
def client(app) -> TestClient:
    c = TestClient(app, headers=SAME_ORIGIN)
    c.auth = AUTH
    return c


@pytest.fixture
def anon(app) -> TestClient:
    return TestClient(app)


_n = iter(range(1, 10_000))


def add_distributor(**kw) -> int:
    n = next(_n)
    data = {
        "arn": f"ARN-{700000 + n}",
        "name": f"Distributor {n:03d}",
        "firm_name": f"Firm {n}",
        "phone": f"+9197{n:08d}",
        "email": f"d{n}@example.com",
        "city": "Pune",
        "status": EmpanelmentStatus.NEW,
    }
    data.update(kw)
    with db.new_session() as s:
        d = Distributor(**data)
        s.add(d)
        s.commit()
        return d.id


def get(model, object_id):
    with db.new_session() as s:
        return s.get(model, object_id)


def count(model, *where) -> int:
    with db.new_session() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def text_of(response) -> str:
    body = re.sub(r"<script.*?</script>", " ", response.text, flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())


# ------------------------------------------------------------------------------------- auth


@pytest.mark.parametrize(
    "path", ["/", "/distributors", "/calls", "/api/stats", "/export/leads.csv", "/simulator"]
)
def test_requires_basic_auth(anon, path):
    response = anon.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


@pytest.mark.parametrize("credentials", [("admin", "wrong"), ("root", "test-password"), ("", "")])
def test_wrong_credentials_are_401(anon, credentials):
    assert anon.get("/", auth=credentials).status_code == 401


def test_unauthenticated_post_is_401_not_403(anon):
    # Authentication is checked before the CSRF policy.
    assert anon.post("/campaigns", data={"name": "x"}).status_code == 401
    assert count(Campaign) == 0


def test_non_ascii_password_is_compared_safely(anon):
    assert anon.get("/", auth=("admin", "pässwörd")).status_code == 401


# ------------------------------------------------------------------------------------- CSRF


def test_csrf_rejects_foreign_origin_and_referer(anon):
    for headers in (
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Referer": "https://evil.example/page"},
        {},  # a plain form post with neither header
    ):
        response = anon.post("/campaigns", data={"name": "Forged"}, headers=headers, auth=AUTH)
        assert response.status_code == 403, headers
    assert count(Campaign) == 0


def test_csrf_accepts_same_origin_referer_and_public_host(anon):
    for i, headers in enumerate(
        (
            {"Origin": "http://testserver"},
            {"Referer": "http://testserver/campaigns"},
            {"Origin": "https://bot.example.test"},  # PUBLIC_BASE_URL host (proxy rewrote Host)
        )
    ):
        response = anon.post(
            "/campaigns", data={"name": f"C{i}"}, headers=headers, auth=AUTH, follow_redirects=False
        )
        assert response.status_code == 303, headers
    assert count(Campaign) == 3


def test_csrf_allows_header_less_json_only(anon):
    # curl / scripts: no Origin, but a JSON body (which no cross-site form can send).
    response = anon.post("/campaigns", json={"name": "ignored"}, auth=AUTH, follow_redirects=False)
    assert response.status_code == 303  # passed the CSRF check; the form itself was empty
    assert count(Campaign) == 0


# ------------------------------------------------------------------------------------- dashboard & lists


def test_dashboard_renders_stats_and_runtime_info(client):
    d = add_distributor(name="Asha Mehta")
    with db.new_session() as s:
        s.add(
            Call(
                distributor_id=d,
                provider="simulator",
                status=CallStatus.COMPLETED,
                outcome=CallOutcome.LINK_SENT,
            )
        )
        s.add(Callback(distributor_id=d, scheduled_for=IN_WINDOW_UTC + timedelta(days=1), notes="RM call"))
        s.commit()
    response = client.get("/")
    assert response.status_code == 200
    page = text_of(response)
    assert "LLM: demo" in page and "Telephony: simulator" in page
    assert "Empanelment funnel" in page and "Asha Mehta" in page and "+91******" in page
    assert "Content-Security-Policy" in response.headers and response.headers["x-frame-options"] == "DENY"


def test_distributor_list_masks_phones_filters_and_paginates(client):
    add_distributor(
        arn="ARN-424242",
        name="Kavita Rao",
        phone="+919811112222",
        city="Nagpur",
        status=EmpanelmentStatus.INTERESTED,
    )
    for _ in range(54):
        add_distributor()

    page = text_of(client.get("/distributors"))
    assert "Page 1 of 2" in page and "55 total" in page
    assert "+919811112222" not in page

    found = client.get("/distributors", params={"q": "nagpur"})
    assert "Kavita Rao" in found.text and "+91******2222" in found.text and "Page 1 of 1" in text_of(found)
    by_arn = text_of(client.get("/distributors", params={"q": "424242"}))
    assert "Kavita Rao" in by_arn and "1 total" in by_arn
    assert "Kavita Rao" not in client.get("/distributors", params={"q": "Firm 7"}).text
    assert "Kavita Rao" in client.get("/distributors", params={"status": "interested"}).text
    assert "Kavita Rao" not in client.get("/distributors", params={"status": "new"}).text
    # LIKE wildcards in the search box are literal.
    assert "No distributors match" in client.get("/distributors", params={"q": "%"}).text

    second = client.get("/distributors", params={"page": 2})
    assert second.text.count('href="/distributors/') == 5


def test_distributor_detail_shows_full_contact_and_history(client):
    d = add_distributor(name="Imran Shaikh", phone="+919822223333", notes="Prefers mornings")
    with db.new_session() as s:
        call = Call(
            distributor_id=d, provider="simulator", status=CallStatus.COMPLETED, summary="Wants the kit"
        )
        s.add(call)
        s.flush()
        s.add(
            OutboundMessage(
                distributor_id=d,
                call_id=call.id,
                channel=MessageChannel.SMS,
                destination="+919822223333",
                body="Your link",
                status=MessageStatus.QUEUED,
            )
        )
        s.commit()
    page = client.get(f"/distributors/{d}").text
    assert "+919822223333" in page and "Wants the kit" in page and "Prefers mornings" in page
    assert client.get("/distributors/9999").status_code == 404


def test_html_is_autoescaped(client):
    d = add_distributor(name="<script>alert(1)</script>")
    page = client.get(f"/distributors/{d}").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page


# ------------------------------------------------------------------------------------- manual actions


def test_manual_status_change_is_audited(client):
    d = add_distributor()
    response = client.post(
        f"/distributors/{d}/status", data={"status": "empanelled", "reason": "Ops confirmed"}
    )
    assert response.status_code == 200 and "Status changed from new to empanelled" in text_of(response)
    assert get(Distributor, d).status == EmpanelmentStatus.EMPANELLED
    with db.new_session() as s:
        event = s.scalars(select(AuditEvent).where(AuditEvent.kind == "manual_status_change")).one()
    assert event.distributor_id == d
    assert event.detail == {"old": "new", "new": "empanelled", "reason": "Ops confirmed", "by": "admin"}

    # The flash is shown once.
    assert "Status changed" not in client.get(f"/distributors/{d}").text


def test_manual_status_change_validates_and_never_reverses_an_opt_out(client):
    d = add_distributor()
    assert "Unknown status" in text_of(client.post(f"/distributors/{d}/status", data={"status": "vip"}))

    client.post(f"/distributors/{d}/status", data={"status": "do_not_call"})
    distributor = get(Distributor, d)
    assert distributor.do_not_call and distributor.status == EmpanelmentStatus.DO_NOT_CALL
    with db.new_session() as s:
        assert compliance.is_dnc(s, distributor.phone)

    response = client.post(f"/distributors/{d}/status", data={"status": "interested"})
    assert "Opt-outs cannot be reversed" in text_of(response)
    assert get(Distributor, d).status == EmpanelmentStatus.DO_NOT_CALL


def test_add_to_dnc_lists_every_number_and_cancels_callbacks(client):
    d = add_distributor(phone="+919833334444", alt_phone="+919833335555")
    with db.new_session() as s:
        s.add(Callback(distributor_id=d, scheduled_for=IN_WINDOW_UTC))
        s.commit()
    response = client.post(f"/distributors/{d}/dnc", data={"reason": "Asked by email"})
    assert "do-not-call list" in text_of(response) and "1 pending callback(s) cancelled" in text_of(response)

    with db.new_session() as s:
        entries = s.scalars(select(DNCEntry).order_by(DNCEntry.phone)).all()
        assert [e.phone for e in entries] == ["+919833334444", "+919833335555"]
        assert {e.source for e in entries} == {"manual"} and entries[0].reason == "Asked by email"
        assert s.scalars(select(Callback)).one().status == CallbackStatus.CANCELED
    distributor = get(Distributor, d)
    assert distributor.do_not_call and distributor.status == EmpanelmentStatus.DO_NOT_CALL


def test_notes_are_saved(client):
    d = add_distributor()
    client.post(f"/distributors/{d}/notes", data={"notes": "  Met at the Pune IFA meet.  "})
    assert get(Distributor, d).notes == "Met at the Pune IFA meet."
    client.post(f"/distributors/{d}/notes", data={"notes": ""})
    assert get(Distributor, d).notes is None


# ------------------------------------------------------------------------------------- CSV import

CSV_TEXT = (
    "ARN,Name,Firm Name,Mobile,Email,City\n"
    "ARN-810001,Sunita Patil,Patil Investments,98765 43210,sunita@example.com,Pune\n"
    "810002,Rahul Jain,,+91 98765 43211,,Indore\n"
    "ARN-810003,No Phone,,12345,,Delhi\n"
)


def test_csv_upload_imports_and_adds_to_campaign(client):
    client.post("/campaigns", data={"name": "NFO Launch"})
    campaign_id = get_campaign_id("NFO Launch")
    assert "NFO Launch" in client.get("/import").text

    response = client.post(
        "/import",
        files={"file": ("amfi.csv", CSV_TEXT.encode("utf-8"), "text/csv")},
        data={"source": "amfi_2026_10", "campaign_id": str(campaign_id), "update_existing": "on"},
    )
    assert response.status_code == 200
    page = text_of(response)
    assert "2 Created" in page and "1 Errors" in page and "no valid Indian mobile number" in page
    assert "Added 2 eligible distributor(s) to campaign NFO Launch" in page

    with db.new_session() as s:
        imported = s.scalars(select(Distributor).order_by(Distributor.arn)).all()
        assert [(d.arn, d.phone, d.source) for d in imported] == [
            ("ARN-810001", "+919876543210", "amfi_2026_10"),
            ("ARN-810002", "+919876543211", "amfi_2026_10"),
        ]
        assert s.scalar(select(func.count(CampaignContact.id))) == 2


def test_csv_upload_errors(client):
    assert client.post("/import", data={"source": "x"}).status_code == 400
    bad = client.post("/import", files={"file": ("x.csv", b"\xff\xfe\x00bad", "text/csv")})
    assert bad.status_code == 400 and "not UTF-8" in text_of(bad)
    unknown = client.post(
        "/import", files={"file": ("a.csv", CSV_TEXT.encode(), "text/csv")}, data={"campaign_id": "999"}
    )
    assert unknown.status_code == 400 and count(Distributor) == 0


def test_csv_upload_without_update_skips_existing(client):
    add_distributor(arn="ARN-810001", name="Old Name", phone="+919876543210")
    response = client.post("/import", files={"file": ("a.csv", CSV_TEXT.encode(), "text/csv")})
    assert "1 Skipped" in text_of(response)
    with db.new_session() as s:
        assert s.scalar(select(Distributor.name).where(Distributor.arn == "ARN-810001")) == "Old Name"


# ------------------------------------------------------------------------------------- campaigns


def get_campaign_id(name: str) -> int:
    with db.new_session() as s:
        return s.scalar(select(Campaign.id).where(Campaign.name == name))


def test_campaign_create_add_start_and_dial_now(client, app, provider):
    first = add_distributor()
    second = add_distributor()
    add_distributor(do_not_call=True, status=EmpanelmentStatus.DO_NOT_CALL)
    add_distributor(status=EmpanelmentStatus.EMPANELLED)

    response = client.post("/campaigns", data={"name": "NFO Launch", "description": "October NFO"})
    assert "Created campaign" in text_of(response)
    assert "already exists" in text_of(client.post("/campaigns", data={"name": "nfo launch"}))
    campaign_id = get_campaign_id("NFO Launch")

    assert "Added 2 eligible distributor(s)" in text_of(client.post(f"/campaigns/{campaign_id}/add-eligible"))
    assert "is active (2 pending contact(s))" in text_of(client.post(f"/campaigns/{campaign_id}/start"))
    campaign = get(Campaign, campaign_id)
    assert campaign.status == CampaignStatus.ACTIVE and campaign.started_at == IN_WINDOW_UTC

    page = text_of(client.post(f"/campaigns/{campaign_id}/dial-now"))
    assert "placed 2, skipped 0, failed 0" in page
    assert sorted(p["call_id"] for p in provider.placed) == [1, 2]
    with db.new_session() as s:
        calls = s.scalars(select(Call).order_by(Call.id)).all()
        assert [c.distributor_id for c in calls] == [first, second]
        assert {c.provider for c in calls} == {"simulator"} and {c.status for c in calls} == {
            CallStatus.INITIATED
        }
        assert {c.state for c in s.scalars(select(CampaignContact))} == {ContactState.IN_PROGRESS}


def test_dial_now_respects_the_calling_window_and_campaign_state(client, app, provider):
    add_distributor()
    client.post("/campaigns", data={"name": "Window"})
    campaign_id = get_campaign_id("Window")
    client.post(f"/campaigns/{campaign_id}/add-eligible")

    assert "not active" in text_of(client.post(f"/campaigns/{campaign_id}/dial-now"))
    client.post(f"/campaigns/{campaign_id}/start")
    app.state.clock = lambda: datetime(2026, 10, 11, 6, 0)  # a Sunday
    assert "Outside the calling window (non_calling_day)" in text_of(
        client.post(f"/campaigns/{campaign_id}/dial-now")
    )
    assert provider.placed == []


def test_campaign_pause_complete_and_unknown_action(client):
    client.post("/campaigns", data={"name": "Lifecycle"})
    campaign_id = get_campaign_id("Lifecycle")
    assert "not active" in text_of(client.post(f"/campaigns/{campaign_id}/pause"))
    client.post(f"/campaigns/{campaign_id}/start")
    assert "is paused" in text_of(client.post(f"/campaigns/{campaign_id}/pause"))
    assert get(Campaign, campaign_id).status == CampaignStatus.PAUSED
    client.post(f"/campaigns/{campaign_id}/complete")
    campaign = get(Campaign, campaign_id)
    assert campaign.status == CampaignStatus.COMPLETED and campaign.completed_at == IN_WINDOW_UTC
    assert "is completed" in text_of(client.post(f"/campaigns/{campaign_id}/add-eligible"))
    assert client.post(f"/campaigns/{campaign_id}/explode").status_code == 404
    assert client.post("/campaigns/999/start").status_code == 404
    assert "Give the campaign a name" in text_of(client.post("/campaigns", data={"name": "  "}))


# ------------------------------------------------------------------------------------- calls


def make_call_with_transcript() -> int:
    d = add_distributor(name="Farah Khan")
    with db.new_session() as s:
        call = Call(
            distributor_id=d,
            provider="twilio",
            status=CallStatus.COMPLETED,
            outcome=CallOutcome.INTERESTED,
            summary="Interested, wants RM visit",
            turn_count=1,
            recording_url="https://api.twilio.com/rec/RE1",
        )
        s.add(call)
        s.flush()
        s.add_all(
            [
                Turn(
                    call_id=call.id, role=TurnRole.BOT, text="Hello from Asha", meta={"scripted": "greeting"}
                ),
                Turn(call_id=call.id, role=TurnRole.DISTRIBUTOR, text="What returns can I expect?"),
                Turn(
                    call_id=call.id,
                    role=TurnRole.BOT,
                    text="I can't comment on returns.",
                    flagged=True,
                    meta={
                        "violations": ["guaranteed_returns"],
                        "original": "You will surely get 15% returns",
                    },
                ),
            ]
        )
        s.commit()
        return call.id


def test_calls_list_and_detail_show_transcript_and_flags(client):
    call_id = make_call_with_transcript()
    listing = client.get("/calls")
    assert "1 flagged" in text_of(listing) and "Farah Khan" in listing.text
    assert "Farah Khan" in client.get("/calls", params={"outcome": "interested"}).text
    assert "Farah Khan" not in client.get("/calls", params={"status": "failed"}).text

    page = client.get(f"/calls/{call_id}")
    text = text_of(page)
    assert 'class="bubble bot flagged"' in page.text and 'class="bubble distributor"' in page.text
    assert "guaranteed_returns" in text and "You will surely get 15% returns" in text
    assert "Interested, wants RM visit" in text and 'href="https://api.twilio.com/rec/RE1"' in page.text
    assert client.get("/calls/999").status_code == 404


def test_api_call_and_stats_json(client):
    call_id = make_call_with_transcript()
    data = client.get(f"/api/calls/{call_id}").json()
    assert data["outcome"] == "interested" and data["distributor"]["phone"].startswith("+91******")
    assert [t["role"] for t in data["turns"]] == ["bot", "distributor", "bot"]
    assert data["turns"][2]["flagged"] is True and data["turns"][2]["meta"]["violations"] == [
        "guaranteed_returns"
    ]

    client.post("/campaigns", data={"name": "Stats"})
    stats = client.get("/api/stats").json()
    assert stats["overall"]["calls_total"] == 1 and stats["overall"]["calls_by_outcome"]["interested"] == 1
    assert [c["name"] for c in stats["campaigns"]] == ["Stats"]
    assert stats["campaigns"][0]["stats"]["contacts_by_state"]["pending"] == 0


# ------------------------------------------------------------------------------------- callbacks & outbox


def test_callbacks_pending_first_and_mark_done(client):
    d = add_distributor(name="Callback Person")
    with db.new_session() as s:
        done = Callback(
            distributor_id=d,
            scheduled_for=IN_WINDOW_UTC - timedelta(days=2),
            status=CallbackStatus.DONE,
            notes="old one",
        )
        pending = Callback(distributor_id=d, scheduled_for=IN_WINDOW_UTC + timedelta(days=1), notes="new one")
        s.add_all([done, pending])
        s.commit()
        pending_id = pending.id
    page = client.get("/callbacks").text
    assert page.index("new one") < page.index("old one")

    response = client.post(f"/callbacks/{pending_id}/done", data={"next": "//evil.example"})
    assert response.url.path == "/callbacks"  # unsafe "next" ignored
    assert get(Callback, pending_id).status == CallbackStatus.DONE
    assert "already done" in text_of(client.post(f"/callbacks/{pending_id}/done"))


def test_outbox_masks_destinations_and_marks_sent(client):
    d = add_distributor()
    with db.new_session() as s:
        sms = OutboundMessage(
            distributor_id=d,
            channel=MessageChannel.SMS,
            destination="+919844445555",
            body="Link: https://x",
            link="https://x",
            status=MessageStatus.QUEUED,
        )
        mail = OutboundMessage(
            distributor_id=d,
            channel=MessageChannel.EMAIL,
            destination="ravi.kumar@example.com",
            body="Link",
            status=MessageStatus.QUEUED,
        )
        s.add_all([sms, mail])
        s.commit()
        sms_id = sms.id
    page = client.get("/messages").text
    assert "+91******5555" in page and "+919844445555" not in page and "ravi.kumar@example.com" not in page

    client.post(f"/messages/{sms_id}/mark-sent")
    message = get(OutboundMessage, sms_id)
    assert message.status == MessageStatus.SENT and message.sent_at == IN_WINDOW_UTC
    with db.new_session() as s:
        event = s.scalars(select(AuditEvent).where(AuditEvent.kind == "message_marked_sent")).one()
    assert event.detail["message_id"] == sms_id and event.detail["previous_status"] == "queued"
    assert "already sent" in text_of(client.post(f"/messages/{sms_id}/mark-sent"))
    assert "+91******5555" not in client.get("/messages", params={"status": "queued"}).text


# ------------------------------------------------------------------------------------- leads export


def test_leads_csv_export_matches_cli_columns(client):
    lead = add_distributor(
        name='=HYPERLINK("http://evil")', status=EmpanelmentStatus.LINK_SENT, phone="+919855556666"
    )
    add_distributor(status=EmpanelmentStatus.NEW)
    with db.new_session() as s:
        s.add(
            Call(
                distributor_id=lead,
                provider="simulator",
                status=CallStatus.COMPLETED,
                outcome=CallOutcome.LINK_SENT,
                summary="Send kit",
                ended_at=IN_WINDOW_UTC,
            )
        )
        s.commit()

    response = client.get("/export/leads.csv")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    assert 'filename="leads-20261013.csv"' in response.headers["content-disposition"]
    assert response.content.startswith("﻿".encode())
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert tuple(rows[0].keys()) == LEAD_COLUMNS and len(rows) == 1
    row = rows[0]
    assert row["name"].startswith("'=") and row["phone"] == "+919855556666" and row["status"] == "link_sent"
    assert row["last_call_outcome"] == "link_sent" and row["last_call_at"] == "2026-10-13T11:00:00+05:30"


def test_leads_csv_export_by_campaign(client):
    lead = add_distributor(status=EmpanelmentStatus.INTERESTED)
    add_distributor(status=EmpanelmentStatus.INTERESTED)
    client.post("/campaigns", data={"name": "Scoped"})
    campaign_id = get_campaign_id("Scoped")
    with db.new_session() as s:
        s.add(CampaignContact(campaign_id=campaign_id, distributor_id=lead, state=ContactState.DONE))
        s.commit()
    rows = list(
        csv.DictReader(
            io.StringIO(client.get(f"/export/leads.csv?campaign_id={campaign_id}").text.lstrip("﻿"))
        )
    )
    assert len(rows) == 1
    assert client.get("/export/leads.csv?campaign_id=999").status_code == 404

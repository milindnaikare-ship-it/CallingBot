"""Tests for callingbot.messaging: providers (mocked HTTP / fake SMTP), Messenger persistence, wiring."""

from __future__ import annotations

import base64
import json
import smtplib
from datetime import datetime
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from callingbot.messaging import MessageSender, Messenger, OutgoingMessage, SendResult, build_messenger
from callingbot.messaging.email_smtp import DEFAULT_SUBJECT, SMTPEmailSender
from callingbot.messaging.outbox import OutboxSender
from callingbot.messaging.sms_twilio import TwilioSMSSender
from callingbot.messaging.whatsapp_meta import GRAPH_API_VERSION, MetaWhatsAppSender
from callingbot.models import Call, MessageChannel, MessageStatus, OutboundMessage

FIXED_NOW = datetime(2026, 10, 13, 5, 31)


class Recorder:
    """httpx.MockTransport handler that records requests and replays a canned response."""

    def __init__(self, status: int = 200, payload: dict | None = None, exc: Exception | None = None):
        self.status, self.payload, self.exc = status, payload or {}, exc
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        return httpx.Response(self.status, json=self.payload)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def _sms(to: str = "+919876543210", body: str = "Your empanelment link: https://bot.example.test/r/abc"):
    return OutgoingMessage(channel=MessageChannel.SMS, to=to, body=body)


def _basic_auth(request: httpx.Request) -> tuple[str, str]:
    scheme, _, encoded = request.headers["Authorization"].partition(" ")
    assert scheme == "Basic"
    user, _, password = base64.b64decode(encoded).decode().partition(":")
    return user, password


# --------------------------------------------------------------------------------------------
# Outbox
# --------------------------------------------------------------------------------------------


def test_outbox_sender_only_queues():
    sender = OutboxSender(MessageChannel.EMAIL)
    assert sender.channel == MessageChannel.EMAIL
    assert sender.send(_sms()) == SendResult(ok=True, queued_only=True)


# --------------------------------------------------------------------------------------------
# Twilio SMS
# --------------------------------------------------------------------------------------------


def test_twilio_sms_request_shape_and_success(settings):
    settings.twilio_sms_from = "SAMPLEMF"
    rec = Recorder(201, {"sid": "SM123", "status": "queued"})
    result = TwilioSMSSender(settings, rec.client()).send(_sms())

    assert result == SendResult(ok=True, provider_message_id="SM123")
    (req,) = rec.requests
    assert req.method == "POST"
    assert str(req.url) == (
        "https://api.twilio.com/2010-04-01/Accounts/AC00000000000000000000000000000000/Messages.json"
    )
    assert _basic_auth(req) == ("AC00000000000000000000000000000000", "test-auth-token")
    form = parse_qs(req.content.decode())
    assert form == {
        "To": ["+919876543210"],
        "From": ["SAMPLEMF"],
        "Body": ["Your empanelment link: https://bot.example.test/r/abc"],
    }


def test_twilio_sms_falls_back_to_voice_number(settings):
    rec = Recorder(201, {"sid": "SM1"})
    TwilioSMSSender(settings, rec.client()).send(_sms())
    assert parse_qs(rec.requests[0].content.decode())["From"] == ["+911400000000"]


def test_twilio_sms_http_400(settings):
    rec = Recorder(400, {"code": 21211, "message": "The 'To' number is not a valid phone number."})
    result = TwilioSMSSender(settings, rec.client()).send(_sms(to="+91123"))
    assert not result.ok and result.provider_message_id is None
    assert result.error == "HTTP 400 (code 21211): The 'To' number is not a valid phone number."


def test_twilio_sms_http_error_without_json_body(settings):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(503, text="<html>down</html>"))
    )
    result = TwilioSMSSender(settings, client).send(_sms())
    assert not result.ok and result.error.startswith("HTTP 503")


def test_twilio_sms_network_error(settings):
    rec = Recorder(exc=httpx.ConnectError("connection refused"))
    result = TwilioSMSSender(settings, rec.client()).send(_sms())
    assert not result.ok
    assert result.error.startswith("network error: ConnectError")
    assert "test-auth-token" not in result.error


def test_twilio_sms_not_configured(settings):
    settings.twilio_auth_token = None
    rec = Recorder(201, {"sid": "SM1"})
    result = TwilioSMSSender(settings, rec.client()).send(_sms())
    assert not result.ok and "not configured" in result.error
    assert rec.requests == []


# --------------------------------------------------------------------------------------------
# Meta WhatsApp
# --------------------------------------------------------------------------------------------


@pytest.fixture
def wa_settings(settings):
    settings.whatsapp_provider = "meta"
    settings.meta_whatsapp_token = "wa-token"
    settings.meta_whatsapp_phone_number_id = "1098765"
    settings.meta_whatsapp_template_name = "empanelment_link"
    settings.meta_whatsapp_template_language = "en"
    return settings


def _wa(template_vars: dict[str, str] | None = None) -> OutgoingMessage:
    return OutgoingMessage(
        channel=MessageChannel.WHATSAPP,
        to="+91 98765 43210",
        body="Hi Ravi, here is your empanelment link",
        template_vars=template_vars
        if template_vars is not None
        else {"name": "Ravi", "link": "https://x.test/r/t"},
    )


def test_whatsapp_request_shape_and_success(wa_settings):
    rec = Recorder(200, {"messaging_product": "whatsapp", "messages": [{"id": "wamid.HBgM"}]})
    result = MetaWhatsAppSender(wa_settings, rec.client()).send(_wa())

    assert result == SendResult(ok=True, provider_message_id="wamid.HBgM")
    (req,) = rec.requests
    assert str(req.url) == f"https://graph.facebook.com/{GRAPH_API_VERSION}/1098765/messages"
    assert GRAPH_API_VERSION == "v20.0"
    assert req.headers["Authorization"] == "Bearer wa-token"
    assert json.loads(req.content) == {
        "messaging_product": "whatsapp",
        "to": "919876543210",
        "type": "template",
        "template": {
            "name": "empanelment_link",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Ravi"},
                        {"type": "text", "text": "https://x.test/r/t"},
                    ],
                }
            ],
        },
    }


def test_whatsapp_parameters_keep_insertion_order_and_strip_newlines(wa_settings):
    rec = Recorder(200, {"messages": [{"id": "wamid.1"}]})
    MetaWhatsAppSender(wa_settings, rec.client()).send(_wa({"b": "second\nline", "a": "first"}))
    params = json.loads(rec.requests[0].content)["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["second line", "first"]


def test_whatsapp_without_template_vars_omits_components(wa_settings):
    rec = Recorder(200, {"messages": [{"id": "wamid.2"}]})
    MetaWhatsAppSender(wa_settings, rec.client()).send(_wa({}))
    assert "components" not in json.loads(rec.requests[0].content)["template"]


def test_whatsapp_http_400(wa_settings):
    rec = Recorder(
        400, {"error": {"message": "(#131030) Recipient phone number not in allowed list", "code": 131030}}
    )
    result = MetaWhatsAppSender(wa_settings, rec.client()).send(_wa())
    assert not result.ok
    assert result.error == "HTTP 400 (code 131030): (#131030) Recipient phone number not in allowed list"


def test_whatsapp_network_error(wa_settings):
    rec = Recorder(exc=httpx.ReadTimeout("timed out"))
    result = MetaWhatsAppSender(wa_settings, rec.client()).send(_wa())
    assert not result.ok and result.error.startswith("network error: ReadTimeout")


def test_whatsapp_success_without_message_id(wa_settings):
    rec = Recorder(200, {"unexpected": True})
    assert MetaWhatsAppSender(wa_settings, rec.client()).send(_wa()) == SendResult(ok=True)


# --------------------------------------------------------------------------------------------
# SMTP email
# --------------------------------------------------------------------------------------------


class FakeSMTP:
    """Stands in for smtplib.SMTP; records every interaction on the class."""

    instances: list[FakeSMTP] = []
    fail_on: str | None = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent = []
        self.login_args = None
        FakeSMTP.instances.append(self)
        if FakeSMTP.fail_on == "connect":
            raise ConnectionRefusedError(111, "Connection refused")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append("login")
        self.login_args = (user, password)
        if FakeSMTP.fail_on == "login":
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed")

    def send_message(self, msg):
        self.calls.append("send")
        self.sent.append(msg)


@pytest.fixture
def smtp_settings(settings):
    FakeSMTP.instances = []
    FakeSMTP.fail_on = None
    settings.email_provider = "smtp"
    settings.smtp_host = "smtp.example.test"
    settings.smtp_port = 587
    settings.smtp_from = "partners@sample-mf.example"
    settings.smtp_username = "mailer"
    settings.smtp_password = "mail-pass"
    return settings


def _email(subject: str | None = "Empanelment with Sample MF", to: str = "ravi@example.com"):
    return OutgoingMessage(
        channel=MessageChannel.EMAIL, to=to, body="Please complete empanelment.", subject=subject
    )


def test_smtp_success_with_starttls_and_login(smtp_settings):
    result = SMTPEmailSender(smtp_settings, smtp_factory=FakeSMTP).send(_email())

    assert (
        result.ok
        and result.provider_message_id
        and result.provider_message_id.endswith("@sample-mf.example>")
    )
    (smtp,) = FakeSMTP.instances
    assert (smtp.host, smtp.port) == ("smtp.example.test", 587)
    assert smtp.calls == ["starttls", "login", "send", "quit"]
    assert smtp.login_args == ("mailer", "mail-pass")
    (msg,) = smtp.sent
    assert msg["From"] == "partners@sample-mf.example"
    assert msg["To"] == "ravi@example.com"
    assert msg["Subject"] == "Empanelment with Sample MF"
    assert msg.get_content().strip() == "Please complete empanelment."


def test_smtp_default_subject_no_tls_no_login(smtp_settings):
    smtp_settings.smtp_starttls = False
    smtp_settings.smtp_username = None
    result = SMTPEmailSender(smtp_settings, smtp_factory=FakeSMTP).send(_email(subject=None))
    assert result.ok
    (smtp,) = FakeSMTP.instances
    assert smtp.calls == ["send", "quit"]
    assert smtp.sent[0]["Subject"] == DEFAULT_SUBJECT


@pytest.mark.parametrize(
    "fail_on,error_prefix", [("login", "SMTPAuthenticationError"), ("connect", "ConnectionRefused")]
)
def test_smtp_errors_are_returned_not_raised(smtp_settings, fail_on, error_prefix):
    FakeSMTP.fail_on = fail_on
    result = SMTPEmailSender(smtp_settings, smtp_factory=FakeSMTP).send(_email())
    assert not result.ok and result.error.startswith(error_prefix)


def test_smtp_rejects_header_injection(smtp_settings):
    result = SMTPEmailSender(smtp_settings, smtp_factory=FakeSMTP).send(
        _email(to="ravi@example.com\r\nBcc: everyone@example.com")
    )
    assert not result.ok
    assert all(not s.sent for s in FakeSMTP.instances)


def test_smtp_not_configured(smtp_settings):
    smtp_settings.smtp_host = None
    result = SMTPEmailSender(smtp_settings, smtp_factory=FakeSMTP).send(_email())
    assert not result.ok and "not configured" in result.error
    assert FakeSMTP.instances == []


# --------------------------------------------------------------------------------------------
# Messenger
# --------------------------------------------------------------------------------------------


class StubSender(MessageSender):
    def __init__(self, channel: MessageChannel, result: SendResult | Exception):
        self.channel = channel
        self.result = result
        self.received: list[OutgoingMessage] = []

    def send(self, message: OutgoingMessage) -> SendResult:
        self.received.append(message)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _messenger(sender: MessageSender) -> Messenger:
    return Messenger({sender.channel: sender}, now=lambda: FIXED_NOW)


def test_messenger_outbox_persists_queued_row(session, make_distributor):
    d = make_distributor()
    call = Call(distributor_id=d.id, provider="simulator")
    session.add(call)
    session.flush()
    messenger = Messenger({MessageChannel.SMS: OutboxSender(MessageChannel.SMS)})

    row = messenger.send(
        session,
        channel=MessageChannel.SMS,
        to=d.phone,
        body="Link: https://bot.example.test/r/tok",
        link="https://bot.example.test/r/tok",
        distributor_id=d.id,
        call_id=call.id,
    )

    assert row.id is not None  # flushed
    assert row.status == MessageStatus.QUEUED
    assert row.sent_at is None and row.error is None and row.provider_message_id is None
    assert (row.distributor_id, row.call_id, row.channel) == (d.id, call.id, MessageChannel.SMS)
    assert (row.destination, row.link) == (d.phone, "https://bot.example.test/r/tok")
    assert session.scalars(select(OutboundMessage)).all() == [row]


def test_messenger_success_marks_sent_with_clock_and_provider_id(session):
    sender = StubSender(MessageChannel.WHATSAPP, SendResult(ok=True, provider_message_id="wamid.9"))
    row = _messenger(sender).send(
        session,
        channel=MessageChannel.WHATSAPP,
        to="+919876543210",
        body="hello",
        template_vars={"name": "Ravi", "link": "https://x"},
    )
    assert row.status == MessageStatus.SENT
    assert row.sent_at == FIXED_NOW
    assert row.provider_message_id == "wamid.9"
    (msg,) = sender.received
    assert msg.template_vars == {"name": "Ravi", "link": "https://x"}
    assert msg.to == "+919876543210" and msg.body == "hello"


def test_messenger_provider_failure_marks_failed(session):
    sender = StubSender(MessageChannel.SMS, SendResult(ok=False, error="HTTP 400: bad number"))
    row = _messenger(sender).send(session, channel=MessageChannel.SMS, to="+91123", body="x")
    assert row.status == MessageStatus.FAILED
    assert row.error == "HTTP 400: bad number"
    assert row.sent_at is None


def test_messenger_sender_exception_is_recorded_not_raised(session):
    sender = StubSender(MessageChannel.EMAIL, RuntimeError("adapter bug"))
    row = _messenger(sender).send(session, channel=MessageChannel.EMAIL, to="a@b.test", body="x", subject="s")
    assert row.status == MessageStatus.FAILED
    assert row.error == "sender error: RuntimeError: adapter bug"
    assert sender.received[0].subject == "s"


def test_messenger_unknown_channel_records_failed_row(session):
    messenger = Messenger({MessageChannel.SMS: OutboxSender(MessageChannel.SMS)})
    row = messenger.send(session, channel=MessageChannel.EMAIL, to="a@b.test", body="x")
    assert row.status == MessageStatus.FAILED
    assert "no sender configured" in row.error and "email" in row.error
    assert row.id is not None


def test_messenger_accepts_channel_string_but_rejects_invalid(session):
    messenger = Messenger({MessageChannel.SMS: OutboxSender(MessageChannel.SMS)})
    assert messenger.send(session, channel="sms", to="+919876543210", body="x").channel == MessageChannel.SMS
    with pytest.raises(ValueError):
        messenger.send(session, channel="fax", to="+919876543210", body="x")


def test_messenger_end_to_end_with_twilio_http_error(session, settings):
    rec = Recorder(400, {"code": 21610, "message": "Attempt to send to unsubscribed recipient"})
    messenger = Messenger({MessageChannel.SMS: TwilioSMSSender(settings, rec.client())})
    row = messenger.send(session, channel=MessageChannel.SMS, to="+919876543210", body="x")
    assert row.status == MessageStatus.FAILED
    assert row.error == "HTTP 400 (code 21610): Attempt to send to unsubscribed recipient"


def test_messenger_does_not_commit(session):
    messenger = Messenger({MessageChannel.SMS: OutboxSender(MessageChannel.SMS)})
    messenger.send(session, channel=MessageChannel.SMS, to="+919876543210", body="x")
    session.rollback()
    assert session.scalars(select(OutboundMessage)).all() == []


# --------------------------------------------------------------------------------------------
# build_messenger
# --------------------------------------------------------------------------------------------


def test_build_messenger_defaults_to_outbox_everywhere(settings):
    messenger = build_messenger(settings)
    assert set(messenger.senders) == set(MessageChannel)
    for channel, sender in messenger.senders.items():
        assert isinstance(sender, OutboxSender) and sender.channel == channel


def test_build_messenger_selects_real_providers(smtp_settings, wa_settings):
    settings = smtp_settings  # same Settings object, all three providers configured
    settings.sms_provider = "twilio"
    rec = Recorder(201, {"sid": "SM9"})
    messenger = build_messenger(settings, http_client=rec.client())
    assert isinstance(messenger.senders[MessageChannel.SMS], TwilioSMSSender)
    assert isinstance(messenger.senders[MessageChannel.WHATSAPP], MetaWhatsAppSender)
    assert isinstance(messenger.senders[MessageChannel.EMAIL], SMTPEmailSender)
    assert messenger.senders[MessageChannel.SMS].send(_sms()).provider_message_id == "SM9"


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"sms_provider": "twilio", "twilio_auth_token": None}, ["TWILIO_AUTH_TOKEN"]),
        (
            {"sms_provider": "twilio", "twilio_from_number": None, "twilio_sms_from": None},
            ["TWILIO_SMS_FROM"],
        ),
        (
            {"whatsapp_provider": "meta"},
            ["META_WHATSAPP_TOKEN", "META_WHATSAPP_PHONE_NUMBER_ID", "META_WHATSAPP_TEMPLATE_NAME"],
        ),
        ({"email_provider": "smtp"}, ["SMTP_HOST", "SMTP_FROM"]),
        (
            {"email_provider": "smtp", "smtp_host": "h", "smtp_from": "f@x.test", "smtp_username": "u"},
            ["SMTP_PASSWORD"],
        ),
    ],
)
def test_build_messenger_fails_fast_on_missing_credentials(settings, overrides, expected):
    for key, value in overrides.items():
        setattr(settings, key, value)
    with pytest.raises(ValueError) as excinfo:
        build_messenger(settings)
    for name in expected:
        assert name in str(excinfo.value)


def test_build_messenger_reports_all_missing_settings_at_once(settings):
    settings.whatsapp_provider = "meta"
    settings.email_provider = "smtp"
    with pytest.raises(ValueError) as excinfo:
        build_messenger(settings)
    message = str(excinfo.value)
    assert "META_WHATSAPP_TOKEN" in message and "SMTP_HOST" in message

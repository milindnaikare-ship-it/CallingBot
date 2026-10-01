# Architecture

AI voice bot that phones mutual fund distributors (AMFI ARN holders) on behalf of an AMC to
(1) create awareness of an upcoming NFO and (2) get them empanelled with the AMC.

## 1. System overview

```
            ┌────────────────────────── Admin (browser) ──────────────────────────┐
            │  Dashboard · Distributors · Campaigns · Calls/transcripts · Simulator │
            └───────────────────────────────┬──────────────────────────────────────┘
                                            │ HTTPS (HTTP Basic auth)
┌──────────────┐  CSV import   ┌────────────▼─────────────┐   Messages API   ┌───────────────┐
│ AMFI/CRM list├──────────────►│  FastAPI app (web/)      ├─────────────────►│ Claude (LLM)  │
└──────────────┘               │  ├─ telephony webhooks   │                  └───────────────┘
                               │  ├─ conversation engine  │   SMS/WA/Email   ┌───────────────┐
┌──────────────┐  place_call   │  ├─ tools (link, cb, DNC)├─────────────────►│ Messaging     │
│ Dialer (CLI) ├──────────────►│  └─ call lifecycle       │                  └───────────────┘
└──────┬───────┘               └────────────▲─────────────┘
       │ REST                               │ webhooks (answer / turn / status)
┌──────▼──────────────────────────────────────┴──────┐
│ Telephony provider (Twilio / Exotel / Simulator)    │──── PSTN ────► Distributor's phone
│ TTS (speaks bot text) + STT (transcribes reply)     │
└─────────────────────────────────────────────────────┘
```

**Turn-based voice loop (Phase 1).** The provider speaks our text (TTS), listens (STT) and
posts the transcript to our webhook; we answer with the next utterance. This is simple and
robust; a streaming (real-time, barge-in) pipeline is planned for Phase 3.

### Call sequence

1. Dialer picks due contacts of an ACTIVE campaign inside the calling window, skips DNC,
   creates a `Call` row and asks the provider to `place_call(to_number, call_id)`.
2. Provider → `POST /telephony/{p}/answer/{call_id}` when answered.
   Engine returns the **pre-approved greeting** (no LLM): virtual-assistant disclosure, AMC
   name, recording notice, identity check.
3. Provider → `POST /telephony/{p}/turn/{call_id}` with the transcript of each reply.
   Engine appends it to the Claude conversation, runs tools, screens the reply for
   compliance, returns the next utterance + action (`gather` / `hangup` / `transfer`).
4. Provider → `POST /telephony/{p}/status/{call_id}` on lifecycle events. On a terminal status
   the lifecycle service finalises the call: outcome, distributor funnel status, retry schedule.

## 2. Package layout and ownership

```
callingbot/
  settings.py        env config (pydantic-settings)                 [foundation]
  knowledge.py       YAML knowledge base models + loader            [foundation]
  db.py, models.py   SQLAlchemy engine + ORM                        [foundation]
  timeutil.py        naive-UTC helpers                              [foundation]
  phone.py           Indian mobile normalisation                    [foundation]
  funnel.py          funnel status precedence + outcome mapping     [foundation]
  compliance.py      calling window, DNC, utterance screen, opt-out [compliance]
  links.py           signed tracking links                          [compliance]
  messaging/         outbox, Twilio SMS, Meta WhatsApp, SMTP        [compliance]
  agent/llm.py       Claude client + test doubles                   [foundation]
  agent/prompts.py   system prompt + per-call context               [engine]
  agent/tools.py     tool schemas + handlers                        [engine]
  agent/engine.py    ConversationEngine                             [engine]
  agent/demo_llm.py  offline rule-based LLM for demos               [engine]
  telephony/         base (foundation), twilio, exotel, simulator   [telephony]
  web/telephony_routes.py  provider webhooks                        [telephony]
  services/distributors.py CSV import, ARN/EUIN validation          [services]
  services/lifecycle.py    call creation, status updates, finalise  [services]
  services/dialer.py       campaign dialling                        [services]
  services/reporting.py    stats                                    [services]
  cli.py                   command line                             [services]
  web/app.py, deps.py, admin.py, simulator_routes.py, templates/    [web]
```

## 3. Conventions

* Python 3.11, SQLAlchemy 2.x typed ORM, FastAPI, pydantic v2.
* **Datetimes in the DB are naive UTC** (`timeutil.utcnow()`); policy checks convert to IST.
* **Phones are E.164** (`+91XXXXXXXXXX`) via `phone.normalize_indian_mobile`.
* **ARN canonical form** `ARN-<digits>` (e.g. `ARN-123456`); EUIN `E` + 6 digits.
* Business content comes only from `config/*.yaml` (`knowledge.get_knowledge()`).
* Funnel status changes go through `funnel.advance_status()` (never assign `distributor.status`
  directly except manual admin edits).
* Audit compliance-relevant events with `models.audit(session, kind, ...)`. Event kinds used:
  `disclosure_played`, `dnc_added`, `opt_out`, `compliance_flag`, `llm_error`, `llm_refusal`,
  `link_sent`, `callback_scheduled`, `transfer`, `dial_skipped`, `dial_failed`, `call_finalized`.
* Inject `now_utc` / `now` callables for testability — no hidden `datetime.now()` in logic.
* No network calls in tests: providers are exercised with `httpx.MockTransport` or fakes.

## 4. Module contracts (binding)

Signatures below are the interface between modules. Implement them exactly; add private
helpers freely.

### 4.1 `callingbot.compliance`

```python
@dataclass(frozen=True)
class WindowDecision:
    allowed: bool
    reason: str | None            # None | "non_calling_day" | "holiday" | "before_window" | "after_window"
    next_allowed_utc: datetime | None   # naive UTC start of next window when not allowed

def check_calling_window(policy: CampaignPolicy, now_utc: datetime, tz: str = "Asia/Kolkata") -> WindowDecision
def next_window_start(policy: CampaignPolicy, after_utc: datetime, tz: str = "Asia/Kolkata") -> datetime
    # earliest naive-UTC instant >= after_utc that is inside the calling window
def is_dnc(session: Session, phone: str) -> bool
    # True if phone is in DNCEntry or any Distributor with that phone has do_not_call
def add_to_dnc(session: Session, phone: str, *, reason: str, source: str) -> DNCEntry
    # idempotent; marks matching distributors via funnel.advance_status(d, DO_NOT_CALL); audits "dnc_added"

@dataclass(frozen=True)
class ScreenResult:
    ok: bool
    violations: list[str]   # rule ids, e.g. "guaranteed_returns", "return_projection", "risk_free", "advice"

def screen_bot_utterance(text: str) -> ScreenResult
def safe_reply(language: str) -> str     # neutral replacement when a reply is blocked (en-IN / hi-IN)
def detect_opt_out(text: str) -> bool    # deterministic EN/Hindi/Hinglish "don't call me again" detector
```

### 4.2 `callingbot.links`

```python
def make_link_token(secret: str, distributor_id: int, call_id: int | None) -> str   # url-safe, HMAC-SHA256 signed
def parse_link_token(secret: str, token: str) -> tuple[int, int | None] | None     # None if tampered
def tracked_link(settings: Settings, distributor_id: int, call_id: int | None) -> str   # f"{settings.base_url}/r/{token}"
def empanelment_target_url(kb: KnowledgeBase, distributor: Distributor, call_id: int | None) -> str
    # fills {arn} and {ref} (ref = f"call{call_id}" or f"dist{distributor_id}") in kb.amc.empanelment_url_template
```

### 4.3 `callingbot.messaging`

```python
# messaging/service.py
class Messenger:
    def __init__(self, senders: dict[MessageChannel, MessageSender]): ...
    def send(self, session: Session, *, channel: MessageChannel, to: str, body: str,
             subject: str | None = None, link: str | None = None,
             distributor_id: int | None = None, call_id: int | None = None,
             template_vars: dict[str, str] | None = None) -> OutboundMessage
        # always persists an OutboundMessage row (QUEUED for outbox, SENT, or FAILED); never raises
        # for provider errors; flushes but does not commit.
def build_messenger(settings: Settings) -> Messenger
# messaging/outbox.py: OutboxSender(channel)        -> SendResult(ok=True, queued_only=True)
# messaging/sms_twilio.py: TwilioSMSSender(settings, http_client: httpx.Client | None = None)
# messaging/whatsapp_meta.py: MetaWhatsAppSender(settings, http_client=None)   # template message
# messaging/email_smtp.py: SMTPEmailSender(settings, smtp_factory=smtplib.SMTP)
```

### 4.4 `callingbot.agent`

```python
# agent/prompts.py
def build_system_prompt(kb: KnowledgeBase) -> str
    # deterministic (byte-identical for the same kb) - no timestamps or per-call data (prompt caching)
def build_call_context(kb: KnowledgeBase, distributor: Distributor, call: Call, now_local: datetime) -> str
    # first user message: today's date/weekday/time (IST), NFO phase (before/during/after), distributor
    # name/firm/city/ARN/status, prior call summaries, language.

# agent/tools.py
TOOL_DEFINITIONS: list[dict]     # Messages API tool dicts, "strict": True, additionalProperties False
@dataclass
class ToolContext:
    session: Session; call: Call; distributor: Distributor; kb: KnowledgeBase
    settings: Settings; messenger: Messenger; now_utc: datetime
@dataclass
class ToolOutcome:
    content: str                 # JSON string returned to the model as tool_result content
    is_error: bool = False
    end_call: bool = False       # hang up after speaking this turn's reply
    transfer_to: str | None = None
def execute_tool(ctx: ToolContext, name: str, tool_input: dict) -> ToolOutcome   # never raises

# agent/engine.py
class ConversationEngine:
    def __init__(self, *, session: Session, kb: KnowledgeBase, settings: Settings,
                 llm: LLMClient, messenger: Messenger, now: Callable[[], datetime] = utcnow): ...
    def start(self, call: Call, *, answered_by: str | None = None) -> VoiceResponse
        # greeting (or voicemail handling); commits
    def handle_input(self, call: Call, speech_text: str | None, *, confidence: float | None = None) -> VoiceResponse
        # one conversational turn; commits

# agent/demo_llm.py
class DemoLLM(LLMClient)          # offline keyword-driven bot using the same tools; model = "demo"
```

**Tool names (fixed):** `verify_arn`, `update_distributor_details`, `send_empanelment_link`,
`schedule_callback`, `set_language`, `transfer_to_human`, `log_request`, `opt_out`, `record_outcome`,
`end_call`. `log_request` creates a `Callback` row whose `kind` (`FollowUpKind`) tells the partner
services team what to do (RM request, commission query, collateral, email issue, empanelment help).

**Approved call script.** When `config/script.yaml` exists, the system prompt renders its steps and
standard responses and the bot follows them; `KnowledgeBase.approved_texts()` (script, FAQ answers,
commission response, disclaimers) is passed to `compliance.screen_bot_utterance(..., approved=...)`
so Compliance-approved sentences - including approved statistics - pass the screen verbatim while
any altered figure is still blocked.

### 4.5 `callingbot.telephony`

```python
# telephony/__init__.py
def get_provider(name: str, settings: Settings) -> TelephonyProvider   # "twilio" | "exotel" | "simulator"
# telephony/twilio.py:    TwilioProvider(settings, http_client: httpx.Client | None = None)
# telephony/exotel.py:    ExotelProvider(settings, http_client: httpx.Client | None = None)
# telephony/simulator.py: SimulatorProvider()
# web/telephony_routes.py: router = APIRouter(prefix="/telephony")
```

### 4.6 `callingbot.services`

```python
# services/distributors.py
def normalize_arn(raw: str | None) -> str | None           # "arn 12345" / "12345" / "ARN-12345" -> "ARN-12345"
def normalize_euin(raw: str | None) -> str | None          # "e123456" -> "E123456"; invalid -> None
@dataclass
class ImportReport:
    created: int = 0; updated: int = 0; skipped: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)   # (row number, reason)
def import_distributors_csv(session: Session, source_file: TextIO | Path | str, *, source: str,
                            update_existing: bool = True) -> ImportReport     # flushes, caller commits

# services/lifecycle.py
def create_call(session: Session, *, distributor: Distributor, provider: str,
                campaign: Campaign | None = None, contact: CampaignContact | None = None,
                language: str | None = None) -> Call                        # flushes (call.id available)
def apply_status_update(session: Session, call: Call, update: CallStatusUpdate, *,
                        kb: KnowledgeBase, now_utc: datetime) -> None
    # idempotent; never moves a terminal call back to non-terminal; on first transition to a
    # terminal status calls finalize_call
def finalize_call(session: Session, call: Call, *, kb: KnowledgeBase, now_utc: datetime) -> None
    # idempotent (engine_state["finalized"]); default outcome; funnel status; contact retry/DONE; audit

# services/dialer.py
@dataclass
class DialReport:
    placed: int = 0; skipped: int = 0; failed: int = 0
    window: WindowDecision | None = None; messages: list[str] = field(default_factory=list)
def add_distributors_to_campaign(session, campaign: Campaign, distributor_ids: Iterable[int] | None = None) -> int
def select_due_contacts(session, campaign: Campaign, *, now_utc: datetime, limit: int) -> list[CampaignContact]
def dial_due_contacts(session, *, campaign: Campaign, provider: TelephonyProvider, kb: KnowledgeBase,
                      settings: Settings, now_utc: datetime, max_new_calls: int | None = None) -> DialReport
def reap_stale_calls(session, *, kb: KnowledgeBase, settings: Settings, now_utc: datetime) -> int

# services/reporting.py
def campaign_stats(session, campaign_id: int | None = None) -> dict
```

### 4.7 `callingbot.web`

```python
# web/app.py
def create_app(settings: Settings | None = None, *, llm: LLMClient | None = None,
               messenger: Messenger | None = None, provider: TelephonyProvider | None = None) -> FastAPI
    # stores settings/kb/llm/messenger/provider on app.state; init_db() on startup
# web/deps.py - FastAPI dependencies: db_session, get_app_settings, get_kb, get_llm, get_messenger,
#               get_provider, require_admin (HTTP Basic, secrets.compare_digest), make_engine
```

## 5. Engine behaviour (summary)

* `llm_messages` is **append-only**. Message 0 is the call-context user message, message 1 the
  greeting as an assistant text block. Each distributor utterance is appended as a user message;
  each Claude response is appended verbatim; tool results go back as one user message.
* Silence: deterministic re-prompt (no LLM) up to `no_input_reprompts`, then polite hang-up.
* Every bot utterance passes `compliance.screen_bot_utterance`; blocked text is replaced with
  `compliance.safe_reply()`, the turn is flagged and audited, and an operator note telling the
  model its reply was not spoken is attached to the next user message.
* Opt-out safety net: if `detect_opt_out()` fires and the model did not call `opt_out` or
  `schedule_callback` that turn, the engine performs the opt-out itself and ends the call.
* Limits: `max_call_turns` / `max_call_seconds` → operator note to wrap up, then forced close.
* LLM failure → apology, RM callback created, hang up; `call.error` recorded.

## 6. Security

* Admin UI and APIs: HTTP Basic (`ADMIN_USERNAME`/`ADMIN_PASSWORD`), constant-time compare.
  Put the app behind HTTPS (reverse proxy) — never expose plain HTTP in production.
* Twilio webhooks: `X-Twilio-Signature` HMAC-SHA1 validation against `PUBLIC_BASE_URL`.
* Tracking links are HMAC-signed; they carry ids only, no personal data.
* Secrets only via environment; `.env` is git-ignored.
* PII minimisation: phone numbers are masked in logs; the bot never asks for PAN, bank,
  Aadhaar or OTP.

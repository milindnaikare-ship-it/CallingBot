# CallingBot

An AI voice assistant that phones **mutual fund distributors** (AMFI ARN holders) for an Indian
AMC, to:

1. **Tell them about an upcoming NFO**, using only Compliance-approved facts, and
2. **Get them empanelled** with the AMC. It sends a tracked empanelment link, books a callback
   with a relationship manager (RM), or transfers the call to a human.

It handles the routine first conversation, so your RMs can spend their time on distributors who
want to talk. Every call is logged with a transcript, an outcome and an audit trail.

> [!WARNING]
> **`data/sample_distributors.csv` is fictitious demo data.** The names, ARNs, e-mails and phone
> numbers are made up. The phone numbers have a valid Indian mobile format, so some of them may
> belong to real people. **Never import this file into a deployment that uses a real telephony
> provider, and never dial it.** Use it only with `TELEPHONY_PROVIDER=simulator`.

> [!IMPORTANT]
> The regulatory notes in this repository are an engineering checklist, **not legal advice**.
> Your Compliance and Legal teams must approve the scripts, the calling policy and the data
> handling before any real distributor is called. Start with [docs/COMPLIANCE.md](docs/COMPLIANCE.md).

---

## Contents

- [What it does](#what-it-does)
- [How a call works](#how-a-call-works)
- [Quickstart (offline, about 5 minutes)](#quickstart-offline-about-5-minutes)
- [Using Claude instead of the demo bot](#using-claude-instead-of-the-demo-bot)
- [Going live with Twilio (pilot)](#going-live-with-twilio-pilot)
- [Telephony providers](#telephony-providers)
- [Configuration](#configuration)
- [Command line reference](#command-line-reference)
- [Admin dashboard](#admin-dashboard)
- [Project layout](#project-layout)
- [Running the tests](#running-the-tests)
- [Documentation](#documentation)

---

## What it does

**Conversation**
- Natural, turn-based voice conversation in **English and Hindi** (more Indian languages can be
  switched on in `config/amc.yaml` once their scripts are approved). The distributor can switch
  language mid-call. The shipped POC configuration enables **English only**; switch Hindi on by
  adding an `hi-IN` entry (voice, speech language, approved greeting) to `languages` in
  `config/amc.yaml`.
- A **pre-approved greeting** that is never AI-generated. It says the caller is a virtual
  assistant, names the AMC, announces that the call may be recorded and confirms who it is
  speaking to.
- Answers come **only from the approved knowledge files**: AMC profile, NFO facts from the
  SID/KIM, and FAQs in `config/*.yaml`. Anything else becomes an RM callback.
- Actions the bot can take on a call: verify the ARN, update contact details, **send the
  empanelment link** (SMS, WhatsApp or e-mail), **schedule a callback**, **transfer to a human
  RM**, record the outcome, and **opt the distributor out**.

**Compliance guardrails** (details in [docs/COMPLIANCE.md](docs/COMPLIANCE.md))
- Calling window: by default Monday to Saturday, 10:00 to 19:00 IST, with a holiday list.
- Internal do-not-call (DNC) list. Opt-out requests are detected and honoured immediately.
- **Every sentence the bot is about to speak is screened.** It blocks guaranteed or assured
  returns, return projections, past-performance claims, "risk-free" language, investment advice,
  commission figures and inducements. Blocked text is replaced with a safe line and flagged
  for review.
- The **SEBI mandatory warning** ("Mutual Fund investments are subject to market risks, read
  all scheme related documents carefully.") is spoken before a connected call ends.
- Limits on call attempts per distributor, with a wait between retries.
- The bot **never asks for PAN, bank details, Aadhaar or OTPs**, and distributors' phone
  numbers and e-mail addresses are not sent to the AI model.
- An **append-only audit log** of disclosures, opt-outs, compliance flags, links sent,
  transfers and errors.

**Campaigns and reporting**
- Import distributor lists from an AMFI export or a CRM (CSV). Column names are matched
  flexibly, and phone numbers, ARNs, EUINs and dates are checked and cleaned up.
- Campaigns: create, add eligible distributors, start, pause. A separate **dialer** process
  places the calls.
- Funnel tracking per distributor: new, contacted, interested, link sent, callback, empanelled,
  not interested, wrong number, do not call.
- **Tracked empanelment links** (`/r/<token>`) record clicks without putting personal data in
  the URL.
- Stats, leads export (CSV), callbacks list and message outbox in the admin dashboard.

**Two ways to try it without phoning anyone**
- A **web simulator** at `/simulator`, where you play the distributor in your browser.
- **`callingbot simulate`**, the same thing in a terminal.

## How a call works

```
Dialer ──► Telephony provider ──► distributor's phone
               │  ▲
     transcript│  │text to speak
               ▼  │
          CallingBot app ──► Claude (decides the reply, may use tools)
               │
               ├── compliance screen on every reply
               └── database: transcript, outcome, audit log
```

1. The **dialer** picks distributors in an active campaign who are due a call, checks the
   calling window and the DNC list, and asks the telephony provider to ring them.
2. When the call is answered, the bot plays the **approved greeting** and confirms identity.
   If voicemail answers, the bot hangs up by default.
3. The provider turns the distributor's speech into text (STT) and sends it to the app.
   **Claude** chooses the next reply using only the approved knowledge, and may use a tool
   (for example, send the empanelment link). The reply is **compliance-screened** and the
   provider speaks it (TTS).
4. Before a connected call ends, the bot speaks the **SEBI mandatory warning**.
5. The app records the outcome, updates the distributor's funnel status and schedules a retry
   if nobody answered.

Phase 1 (this build) is **turn-based**: the bot listens, then replies. Real-time streaming
voice with interruption ("barge-in") and Indian-language speech vendors is planned for Phase 3.
See [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md).

## Quickstart (offline, about 5 minutes)

You need **Python 3.11 or newer**. This runs entirely on your machine. It uses the offline demo
bot (`LLM_PROVIDER=fake`, no API key) and the simulator (`TELEPHONY_PROVIDER=simulator`, no
phone calls).

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                                     # defaults are safe and offline

callingbot init-db                                       # creates the SQLite database
callingbot check-config                                  # validates .env and config/*.yaml

# Load the FICTITIOUS demo distributors into a campaign called "NFO Launch".
callingbot import-distributors data/sample_distributors.csv --campaign "NFO Launch"

# Talk to the bot in your terminal. You play the distributor.
LLM_PROVIDER=fake callingbot simulate

# Start the web app.
callingbot serve
```

Then open:

- **http://localhost:8000**: the admin dashboard. Log in with `ADMIN_USERNAME` /
  `ADMIN_PASSWORD` from `.env`, which default to `admin` / `change-me`.
- **http://localhost:8000/simulator**: the web simulator.

The importer reports what it created, updated and rejected. The sample file deliberately
includes a row with no valid mobile number, a duplicate ARN and an expired ARN, so you can see
the validation working.

`make install`, `make run`, `make simulate` and `make test` are shortcuts for the same steps.
Run `make help` for the full list.

## Using Claude instead of the demo bot

The demo bot follows simple keyword rules. The real conversation uses **Claude** through the
Anthropic API.

1. Get an API key from the Anthropic Console.
2. Set `LLM_PROVIDER=anthropic` in `.env`.
3. Make the key available as `ANTHROPIC_API_KEY`. The Anthropic SDK reads it from the
   **process environment**, not from the app's settings. So either `export
   ANTHROPIC_API_KEY=...` in your shell, or put it in `.env` and use the `make` targets (they
   pass the key from `.env` through). Docker Compose also passes `.env` into the containers.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
LLM_PROVIDER=anthropic callingbot simulate
```

Model settings:

| Setting | Default | Notes |
|---|---|---|
| `LLM_MODEL` | `claude-sonnet-5-5` | Chosen for the POC (fast, cost-effective for live voice). `claude-opus-5-5` is the alternative for deeper reasoning at higher latency and cost. |
| `LLM_EFFORT` | `low` | How long the model thinks before it replies. `low` keeps voice turns fast. |
| `LLM_ENABLE_FALLBACKS` | `true` | If the API declines a request for safety reasons, the server retries it automatically on a fallback model (Claude API only). |

Sending conversation text to an API hosted outside India is a **data residency question for
InfoSec and Legal**. See [docs/COMPLIANCE.md](docs/COMPLIANCE.md#8-data-residency-and-cross-border-processing).

## Going live with Twilio (pilot)

Twilio works today and is a good fit for **internal testing and small pilots**. Calls to India
from an international Twilio number may not meet Indian telemarketing rules for live outreach
(DLT registration, number series, caller ID). **Check with Compliance before calling any real
distributor.** For production in India, plan on an Indian DLT-registered provider (see
[Telephony providers](#telephony-providers)).

1. **Get approvals first.** Compliance must sign off `config/*.yaml`, and the pilot list must be
   distributors who know about the pilot. Work through the
   [pre-go-live sign-off](docs/COMPLIANCE.md#pre-go-live-sign-off).
2. **Twilio account.** Buy or verify a caller number, and enable **India** under Voice
   Geographic Permissions. Trial accounts can only call numbers you have verified. No webhook
   setup is needed in the Twilio console, because the app sends its webhook URLs with every
   call. More detail in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#6-twilio-console-setup).
3. **Public URL.** Twilio must be able to reach the app. For a laptop test:
   ```bash
   ngrok http 8000          # gives you https://<something>.ngrok-free.app
   ```
4. **Edit `.env`:**
   ```bash
   PUBLIC_BASE_URL=https://<something>.ngrok-free.app
   TELEPHONY_PROVIDER=twilio
   TWILIO_ACCOUNT_SID=AC...
   TWILIO_AUTH_TOKEN=...
   TWILIO_FROM_NUMBER=+1...            # your Twilio number, E.164
   LLM_PROVIDER=anthropic              # and export ANTHROPIC_API_KEY
   RM_TRANSFER_NUMBER=+91...           # optional: RM desk for warm transfers
   ADMIN_PASSWORD=<a strong password>
   SECRET_KEY=<a long random value>
   ```
5. **Check the configuration:** `callingbot check-config`.
6. **Load the pilot list and start the campaign.** For your first test, use a CSV that
   contains **only your own number**.
   ```bash
   callingbot import-distributors pilot.csv --source pilot_2026_10 --campaign "NFO Pilot"
   callingbot campaign start "NFO Pilot"
   ```
   `callingbot campaign add NAME` adds **every eligible distributor in the database**. Do not
   use it for a pilot if the database also contains other lists.
7. **Run the app and the dialer** as two separate processes:
   ```bash
   callingbot serve                                        # terminal 1
   callingbot run-dialer --campaign "NFO Pilot" --once     # terminal 2: a single dialling pass
   callingbot run-dialer --campaign "NFO Pilot"            # or keep dialling every --interval seconds
   ```
8. **Watch the dashboard**: calls, transcripts, flags and callbacks. Pause at any time with
   `callingbot campaign pause "NFO Pilot"`.

## Telephony providers

| Provider | Status | Use it for |
|---|---|---|
| `simulator` | Complete | Demos, training, script review, automated tests. No phone calls are made. |
| `twilio` | Complete (turn-based voice) | Internal testing and small pilots. Check Indian telemarketing rules before using international numbers for live outreach. |
| `exotel` | Outbound dialling and call status are implemented. The conversation itself needs Exotel's Voicebot (audio streaming), planned for Phase 3. | Indian DLT-registered numbers, once Phase 3 is done. |
| Ozonetel, Knowlarity, Tata Tele Business, Airtel IQ, Plivo, ... | Not built | Can be added as adapters that implement `callingbot/telephony/base.py`. |

## Configuration

| Where | What | Who edits it |
|---|---|---|
| `.env` (from `.env.example`) | Deployment settings and secrets: database, public URL, admin login, LLM, telephony and messaging providers, call limits. Every variable is listed in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#2-environment-variables). | Developer / IT |
| `config/amc.yaml` | Who is calling: AMC name, bot name, helpline, empanelment link template, steps and documents, value propositions, languages and **greetings**. | Distribution + Compliance |
| `config/nfo.yaml` | The **only** scheme facts the bot may state (from the SID/KIM): dates, category, objective, benchmark, fund managers, minimums, exit load, riskometer, approved highlights, commission wording, mandatory disclaimer. | Product + Compliance |
| `config/faq.yaml` | Approved answers to common distributor questions. | Distribution + Compliance |
| `config/campaign.yaml` | Calling days and hours (IST), holidays, maximum attempts, retry waits, concurrency and pacing, voicemail policy. | Distribution + Compliance |

The YAML files ship with **placeholder content marked `TODO`**. Replace every TODO with
approved text before go-live. `callingbot check-config` validates the files. Restart the app
and the dialer after you change them.

## Command line reference

| Command | What it does |
|---|---|
| `callingbot init-db` | Creates the database tables (safe to run again). |
| `callingbot check-config` | Validates `.env` settings and `config/*.yaml`, and reports problems. |
| `callingbot import-distributors PATH [--source NAME] [--campaign NAME] [--no-update]` | Imports a distributor CSV. `--source` labels where the list came from. `--campaign` also adds the imported distributors to that campaign. `--no-update` leaves distributors already in the database (same ARN, or same mobile number for rows without an ARN) unchanged. |
| `callingbot campaign create NAME [--description TEXT]` | Creates a campaign (in draft). |
| `callingbot campaign add NAME` | Adds **all eligible distributors** to the campaign. Opted-out, already empanelled and wrong-number distributors are never added. |
| `callingbot campaign start NAME` / `pause NAME` / `list` | Starts or pauses dialling, or lists campaigns. |
| `callingbot run-dialer --campaign NAME [--once] [--interval SECONDS]` | Dials due contacts of an active campaign. `--once` does one pass and exits (useful with cron). |
| `callingbot simulate [--arn ARN \| --phone NUMBER] [--language CODE]` | Simulated conversation in the terminal, as a given distributor and language. |
| `callingbot test-call (--phone NUMBER \| --arn ARN) [--ignore-window] [--language CODE]` | Phone one imported distributor now, outside any campaign - for closed POC testing (see [docs/TWILIO_SETUP.md](docs/TWILIO_SETUP.md)). |
| `callingbot serve [--host HOST] [--port PORT] [--reload]` | Runs the web app (`uvicorn --factory callingbot.web.app:create_app`). |
| `callingbot stats [--campaign NAME]` | Prints funnel and call statistics. |
| `callingbot export-leads PATH [--campaign NAME]` | Exports leads (distributors with a positive outcome, such as interested, link sent or callback requested) to a CSV for the RM team. |

**Distributor CSV columns.** Header matching ignores case, spaces, punctuation and apostrophes,
so an AMFI export works as-is:

| Field | Accepted headers |
|---|---|
| ARN (optional, validated when present; rows without one are matched by mobile number) | `ARN`, `ARN Code`, `ARN No`, `ARN Number`, `AMFI Registration Number` |
| Name (required) | `Name`, `ARN Holder's Name`, `ARN Holder Name`, `Distributor Name`, `Contact Person` |
| Phone (required) | `Phone`, `Mobile`, `Mobile No`, `Mobile Number`, `Contact Number`, `Telephone (O)`, `Telephone (R)`. The first valid Indian mobile becomes the phone, and the next different one becomes the alternate phone. |
| Alternate phone | `Alt Phone`, `Alternate Phone`, `Alternate Mobile` |
| Firm | `Firm`, `Firm Name`, `Company`, `Entity Name` |
| Email | `Email`, `Email ID`, `E-mail` |
| City / State / PIN | `City`; `State`; `Pin`, `Pincode`, `Pin Code` |
| EUIN | `EUIN` |
| ARN valid till | `ARN Valid Till`, `Valid Till`, `ARN Expiry`. Dates may be written as `31-Mar-2027`, `2027-03-31`, `31/03/2027` or `31-03-2027`. |
| Preferred language | `Language`, `Preferred Language`. `Hindi`/`hi`/`hi-IN` and `English`/`en` are recognised; anything else is left blank. |

## Admin dashboard

Open `/` and log in (HTTP Basic auth). The dashboard has:

- **Stats**: calls, connects, outcomes and the empanelment funnel.
- **Distributors**: funnel status, details and call history.
- **Campaigns**: create, add eligible distributors, start or pause, and dial now.
- **Calls**: full transcripts. Lines changed by the compliance screen are flagged.
- **Callbacks** for RMs, and the **outbox** of messages (links queued or sent).
- **CSV import** and **leads export**.

Tracked empanelment links (`/r/<token>`) record clicks, then redirect to the AMC's
empanelment form.

## Project layout

```
callingbot/
  settings.py          environment configuration
  knowledge.py         loads config/*.yaml (approved content)
  models.py, db.py     database models (SQLite by default, PostgreSQL in production)
  compliance.py        calling window, DNC, utterance screen, opt-out detection
  links.py             signed tracking links
  funnel.py, phone.py, timeutil.py
  agent/               Claude client, system prompt, tools, conversation engine, offline demo bot
  telephony/           provider adapters: simulator, Twilio, Exotel
  messaging/           SMS / WhatsApp / e-mail senders and the outbox
  services/            CSV import, call lifecycle, dialer, reporting
  web/                 FastAPI app: admin dashboard, simulator, telephony webhooks
  cli.py               the "callingbot" command
config/                approved business content (YAML)
data/                  sample_distributors.csv (FICTITIOUS, never dial)
docs/                  architecture, compliance, call flow, deployment, plan, scope questionnaire
tests/                 pytest suite (no network: providers are faked)
```

## Running the tests

```bash
pytest -q                 # or: make test
ruff check .              # lint
ruff format --check .     # formatting (CI runs all three: make lint)
```

The tests never touch the network or real providers. Telephony and messaging are exercised with
`httpx.MockTransport` or fakes, and the LLM with scripted test doubles. CI runs on every push
and pull request (`.github/workflows/ci.yml`).

## Documentation

| Document | For | What's in it |
|---|---|---|
| [docs/SCOPE_QUESTIONNAIRE.md](docs/SCOPE_QUESTIONNAIRE.md) | AMC business team | The inputs we need from you, and what the MVP assumes until we have them. |
| [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md) | Everyone | Phases, deliverables, acceptance criteria, risks, how we work together. |
| [docs/COMPLIANCE.md](docs/COMPLIANCE.md) | Compliance, Legal, InfoSec | India regulatory checklist and how the bot handles each item; pre-go-live sign-off. |
| [docs/CALL_FLOW.md](docs/CALL_FLOW.md) | Distribution, Compliance | Conversation design, sample dialogues (English and Hindi), objection handling. |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | IT / developers | Environment variables, Docker, Postgres, HTTPS, Twilio setup, scaling, backups, security. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Developers | System design and the binding module contracts. |

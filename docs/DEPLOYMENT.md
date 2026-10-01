# Deployment and operations

How to configure, run, secure and operate CallingBot. For a first local run, see the
[README quickstart](../README.md#quickstart-offline-about-5-minutes).

---

## 1. Processes

CallingBot runs as **two processes** that share one database:

| Process | Command | Role |
|---|---|---|
| **Web app** | `callingbot serve`, which runs `uvicorn --factory callingbot.web.app:create_app` | Admin dashboard (`/`), web simulator (`/simulator`), telephony webhooks (`/telephony/...`), tracked links (`/r/<token>`). Creates the database tables on startup. |
| **Dialer** | `callingbot run-dialer --campaign NAME` | Places calls for an **active** campaign, inside the calling window, respecting DNC, attempts, backoff, concurrency and pacing. Also tidies up calls whose final status never arrived. |

Run **exactly one dialer per campaign**. The app can run as several replicas once you use
PostgreSQL (see [Scaling](#8-scaling-notes)).

## 2. Environment variables

Settings are read from environment variables, and from a `.env` file in the working directory
for local runs. Names are case-insensitive; by convention they are upper case.
`.env.example` contains every variable with safe defaults.

> [!NOTE]
> The **code default** for `LLM_PROVIDER` is `anthropic`. `.env.example` sets it to `fake`
> so a fresh checkout runs offline. In production set `LLM_PROVIDER=anthropic` explicitly.

### Application

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `dev` | Environment name: `dev`, `test` or `prod`. Use `prod` in production. |
| `DATABASE_URL` | `sqlite:///./callingbot.db` | SQLAlchemy URL. SQLite for development; PostgreSQL in production: `postgresql+psycopg://USER:PASSWORD@HOST:5432/callingbot`. The Docker image defaults to `sqlite:////app/var/callingbot.db`. |
| `CONFIG_DIR` | `config` | Folder holding `amc.yaml`, `nfo.yaml`, `faq.yaml` and `campaign.yaml`. |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Public **HTTPS** address the telephony provider can reach, without a trailing slash. Used for webhook URLs, Twilio signature checks and tracked links. |
| `SECRET_KEY` | `dev-secret-change-me` | Signs tracked empanelment links. **Must be changed in production.** Changing it later invalidates links already sent. |
| `ADMIN_USERNAME` | `admin` | Admin dashboard user (HTTP Basic). |
| `ADMIN_PASSWORD` | `change-me` | Admin dashboard password. **Must be changed.** |
| `TIMEZONE` | `Asia/Kolkata` | Business timezone for display and calling-window checks. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

### LLM (Claude)

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` (Claude) or `fake` (offline rule-based demo bot, no API key). |
| `LLM_MODEL` | `claude-sonnet-5-5` | Claude model id. `claude-opus-5-5` is the alternative for deeper reasoning at higher latency and cost. |
| `LLM_EFFORT` | `low` | `low`, `medium`, `high`, `xhigh` or `max`. Higher means more thinking and more latency. `low` suits live voice. |
| `LLM_MAX_TOKENS` | `4096` | Maximum output tokens per reply, including thinking. |
| `LLM_TIMEOUT_SECONDS` | `20.0` | Timeout per API request. |
| `LLM_MAX_RETRIES` | `2` | SDK retries on transient errors. |
| `LLM_ENABLE_FALLBACKS` | `true` | Server-side refusal fallback on the Claude API. Set `false` if routing through Bedrock, Vertex AI or Foundry. |
| `ANTHROPIC_API_KEY` | none | **Read by the Anthropic SDK from the process environment, not from `.env`.** Export it in the shell, or use the `make` targets (they pass the key from `.env` through) or Docker Compose (`env_file`). |

### Telephony

| Variable | Default | Description |
|---|---|---|
| `TELEPHONY_PROVIDER` | `simulator` | `simulator`, `twilio` or `exotel`. |
| `TWILIO_ACCOUNT_SID` | none | Twilio Account SID (required for `twilio`; also used for Twilio SMS). |
| `TWILIO_AUTH_TOKEN` | none | Twilio Auth Token (required for `twilio`). Also the key used to verify webhook signatures. |
| `TWILIO_FROM_NUMBER` | none | Caller ID in E.164 (required for `twilio`). |
| `TWILIO_VALIDATE_SIGNATURE` | `true` | Reject Twilio webhooks without a valid `X-Twilio-Signature`. Keep `true`. |
| `TWILIO_MACHINE_DETECTION` | `false` | Answering-machine detection, so the bot can hang up on voicemail. Adds a few seconds of silence after pick-up. |
| `TWILIO_RECORD_CALLS` | `false` | Record whole calls at Twilio. Agree storage and retention with Compliance first. |
| `EXOTEL_ACCOUNT_SID` | none | Exotel account SID (required for `exotel`). |
| `EXOTEL_API_KEY` | none | Exotel API key (required for `exotel`). |
| `EXOTEL_API_TOKEN` | none | Exotel API token (required for `exotel`). |
| `EXOTEL_SUBDOMAIN` | `api.exotel.com` | Exotel API host for your account's region. |
| `EXOTEL_CALLER_ID` | none | ExoPhone / DLT-registered virtual number used as caller ID (required for `exotel`). |
| `EXOTEL_APP_ID` | none | Exotel flow (App Bazaar applet) the call is connected to (required for `exotel`). |

### Call behaviour

| Variable | Default | Description |
|---|---|---|
| `MAX_CALL_TURNS` | `24` | Distributor replies handled before the bot wraps up. |
| `MAX_CALL_SECONDS` | `420` | Maximum call length; wrap-up, then forced close. |
| `NO_INPUT_REPROMPTS` | `1` | Re-prompts after silence before a polite hang-up. |
| `GATHER_TIMEOUT_SECONDS` | `6` | Seconds to wait for the distributor to start speaking. |
| `RM_TRANSFER_NUMBER` | none | E.164 number of the RM desk for warm transfers. Optional. |

### Messaging

| Variable | Default | Description |
|---|---|---|
| `SMS_PROVIDER` | `outbox` | `outbox` (store only, for manual sending) or `twilio`. |
| `WHATSAPP_PROVIDER` | `outbox` | `outbox`, `twilio` (Twilio WhatsApp / Sandbox, free-form text inside a 24-hour session) or `meta` (WhatsApp Business Cloud API, template messages). |
| `TWILIO_WHATSAPP_FROM` | none | Twilio WhatsApp sender, e.g. the sandbox `whatsapp:+14155238886`. |
| `EMAIL_PROVIDER` | `outbox` | `outbox` or `smtp`. |
| `SMTP_HOST` | none | SMTP server. |
| `SMTP_PORT` | `587` | SMTP port (STARTTLS submission port). |
| `SMTP_USERNAME` | none | SMTP user. |
| `SMTP_PASSWORD` | none | SMTP password. |
| `SMTP_FROM` | none | From address, e.g. `Your AMC Partners <partners@your-amc.example>`. |
| `SMTP_STARTTLS` | `true` | Use STARTTLS. |
| `TWILIO_SMS_FROM` | none | Sender for Twilio SMS. Indian SMS also needs a DLT header and template (see [COMPLIANCE.md](COMPLIANCE.md)). |
| `META_WHATSAPP_TOKEN` | none | Meta Cloud API access token. |
| `META_WHATSAPP_PHONE_NUMBER_ID` | none | WhatsApp Business phone number id. |
| `META_WHATSAPP_TEMPLATE_NAME` | none | Name of the approved template that carries the link. |
| `META_WHATSAPP_TEMPLATE_LANGUAGE` | `en` | Template language code. |

### Docker Compose only (not read by the app)

| Variable | Default | Description |
|---|---|---|
| `CAMPAIGN_NAME` | `NFO Launch` | Campaign the `dialer` service dials. |
| `POSTGRES_PASSWORD` | `change-me-postgres` | Password of the bundled Postgres container. **Change it.** |

Empty values are not the same as unset: `TWILIO_ACCOUNT_SID=` sets an empty string, and an empty
number fails validation. Comment out a line you don't use. Run `callingbot check-config` after
every change.

## 3. Docker and docker-compose

The `Dockerfile` builds one image, based on `python:3.11-slim` with the Postgres driver, that runs
as a non-root user. By default it starts the web app on port 8000.

```bash
docker build -t callingbot .
docker run --env-file .env -p 8000:8000 -v callingbot-data:/app/var callingbot   # SQLite in a volume
```

`docker-compose.yml` runs the full stack: **app**, **dialer** and **postgres:16** with a
persistent volume.

```bash
cp .env.example .env          # edit: passwords, LLM, telephony, PUBLIC_BASE_URL, CAMPAIGN_NAME
docker compose up -d postgres app
docker compose exec app callingbot check-config
docker compose exec app callingbot campaign create "NFO Launch"
docker compose cp my_distributors.csv app:/tmp/list.csv    # a real, scrubbed list (never the sample!)
docker compose exec app callingbot import-distributors /tmp/list.csv --source crm_2026_10 --campaign "NFO Launch"
docker compose exec app callingbot campaign start "NFO Launch"
docker compose up -d dialer
docker compose logs -f app dialer
```

- Compose sets `DATABASE_URL` to the bundled Postgres for both services, overriding `.env`.
- The app port is bound to `127.0.0.1:8000`, so publish it through the HTTPS reverse proxy.
- `config/*.yaml` is baked into the image. **Rebuild** (`docker compose up -d --build`) after
  changing the YAML, or mount the folder read-only (`./config:/app/config:ro`) and restart.
- To stop dialling at once: `docker compose stop dialer`, or pause the campaign.

## 4. PostgreSQL

SQLite suits a single laptop. Use PostgreSQL for pilots and production.

```bash
pip install ".[postgres]"     # installs the psycopg driver
export DATABASE_URL="postgresql+psycopg://callingbot:PASSWORD@db.internal:5432/callingbot"
callingbot init-db            # creates tables (the app also does this on startup)
```

Tables are created with `create_all`. That is fine for the MVP, but it does not alter existing
tables when the schema changes. Database migrations (Alembic) are planned before the schema is
changed in production.

## 5. HTTPS, reverse proxy and the public URL

The telephony provider must reach `PUBLIC_BASE_URL` over the internet, and the admin dashboard
uses HTTP Basic auth. So **always serve over HTTPS** and never expose plain HTTP.

**Development:** use a tunnel.

```bash
ngrok http 8000
# set PUBLIC_BASE_URL=https://<id>.ngrok-free.app in .env, then restart the app AND the dialer
```

Free ngrok URLs change every time ngrok restarts. Update `PUBLIC_BASE_URL` each time, or reserve
a static domain.

**Production:** terminate TLS at a reverse proxy (Nginx, Caddy, a cloud load balancer) and
forward to the app on port 8000. Example Caddyfile:

```
bot.your-amc.example {
    # Admin UI: allow only the office/VPN range
    @admin not path /telephony/* /r/*
    handle @admin {
        @blocked not remote_ip 10.0.0.0/8 203.0.113.0/24
        respond @blocked 403
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        reverse_proxy 127.0.0.1:8000
    }
}
```

- `PUBLIC_BASE_URL` must be the exact external URL (`https://bot.your-amc.example`). Twilio
  signatures are computed on that URL, not on the internal address.
- `/telephony/*` must be reachable by the provider. `/r/*` must be reachable by distributors.
  Everything else (dashboard, simulator) can be limited to the office or VPN.
- Exotel webhooks are not signed. Restrict `/telephony/exotel/*` to Exotel's published egress
  IP addresses at the proxy.

## 6. Twilio console setup

1. Create a Twilio account and upgrade it from trial for real pilots. Trial accounts can only
   call **verified** numbers, and they play a trial notice at the start of each call.
2. Get a caller number. Buy a Twilio number, or verify an existing number as caller ID. Put it in
   `TWILIO_FROM_NUMBER` (E.164).
3. **Voice Geographic Permissions:** enable **India** so the account may call Indian numbers.
4. Copy the **Account SID** and **Auth Token** into `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN`.
5. **No webhook configuration is needed** on the number. The app passes its answer, turn and
   status URLs (built from `PUBLIC_BASE_URL`) with every call it places.
6. Leave `TWILIO_VALIDATE_SIGNATURE=true`. If webhooks are rejected with 403, `PUBLIC_BASE_URL`
   almost certainly doesn't match the URL Twilio called (scheme, host, port or trailing slash).
7. For an RM transfer, set `RM_TRANSFER_NUMBER`. The account must also be allowed to call that
   number.

> [!WARNING]
> A Twilio number from outside India reaches distributors as an international call. That may not
> satisfy Indian telemarketing rules (DLT registration, number series, caller ID display) for live
> outreach. Use Twilio for internal tests and Compliance-approved pilots only. See
> [COMPLIANCE.md](COMPLIANCE.md#2-trai-tcccpr-and-dlt-telemarketing).

**Exotel:** outbound dialling and status callbacks are implemented. Set the `EXOTEL_*` variables
and build a flow in Exotel's App Bazaar. The conversational part needs Exotel's Voicebot (audio
streaming), planned for Phase 3, so Exotel cannot yet hold a conversation.

## 7. Running the dialer

```bash
callingbot campaign start "NFO Launch"
callingbot run-dialer --campaign "NFO Launch"               # loops; pause between passes = --interval SECONDS
callingbot run-dialer --campaign "NFO Launch" --once        # one pass, then exit (cron / systemd timer)
```

- The dialer places calls only for an **active** campaign, only inside the calling window in
  `config/campaign.yaml`, and never to DNC numbers or to distributors who are opted out,
  already empanelled or marked wrong number.
- Pacing and concurrency come from `max_concurrent_calls` and `calls_per_minute` in
  `campaign.yaml`. Keep them within your provider's limits and your RM team's capacity to handle
  transfers and callbacks.
- Pausing the campaign (`callingbot campaign pause NAME` or the dashboard) stops new calls.
  Calls already in progress finish normally.
- Under systemd, run the dialer as its own service with `Restart=always`, next to the app
  service.

## 8. Scaling notes

- **Each conversational turn is a stateless webhook.** All call state (conversation history,
  engine state) is in the database. With PostgreSQL you can run several app replicas behind the
  load balancer. SQLite supports a single app process only.
- **Latency is dominated by the LLM.** Keep `LLM_EFFORT=low`, keep the knowledge YAML concise,
  and measure the time from the end of speech to the start of the reply in the pilot. The system
  prompt and tools are prompt-cached between turns.
- **Concurrency limits:** campaign pacing, the telephony account's concurrent-call limit, and the
  Anthropic API rate limits for your organisation. Raise them together.
- **Real-time streaming voice** (barge-in, Indian-language STT/TTS vendors, Exotel Voicebot)
  is Phase 3, with its own scaling work. See [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md).

## 9. Backups

- **PostgreSQL:** take a nightly `pg_dump` (or use managed snapshots) to encrypted storage in an
  approved region, and test a restore at least once before go-live. Keep backups only as long as
  the agreed retention period. Backups contain personal data.
- **SQLite:** `sqlite3 callingbot.db ".backup callingbot-$(date +%F).db"` while the app runs. Do
  not just copy the file during writes.
- **Configuration:** `config/*.yaml` is in git. Tag the commit that Compliance approved for each
  campaign.
- **Recordings** (if enabled) stay with the telephony provider. Apply the provider's retention
  settings to match your policy.

## 10. Monitoring and logging

- Logs go to stdout and stderr. Collect them with your platform: Docker logs, journald or
  CloudWatch. Set `LOG_LEVEL=INFO` in production. Phone numbers are masked in logs. Avoid `DEBUG`
  in production.
- **Daily checks:** `callingbot stats --campaign NAME` or the dashboard. Review **flagged
  transcripts** (compliance screen hits), `llm_error` and `llm_refusal` audit events,
  `dial_failed` events, and pending callbacks.
- **Alerts worth setting up:** app not responding, dialer process down, a spike in
  `dial_failed` or `llm_error`, the Twilio debugger reporting webhook errors (11200-series), and
  Anthropic API errors or rate limiting.
- **Provider consoles:** Twilio Monitor / Debugger for call errors. Anthropic Console for usage
  and spend.

## 11. Security checklist

- [ ] `ADMIN_PASSWORD` changed to a strong, unique password; `ADMIN_USERNAME` not `admin`.
- [ ] `SECRET_KEY` set to a long random value (`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`).
- [ ] HTTPS only; no plain-HTTP listener reachable from the internet.
- [ ] Admin dashboard and simulator restricted by IP allow-list or VPN; only `/telephony/*` and `/r/*` public.
- [ ] `TWILIO_VALIDATE_SIGNATURE=true`; Exotel webhooks restricted by IP at the proxy.
- [ ] Secrets only in the environment or a secrets manager; `.env` never committed or baked into images.
- [ ] Postgres not exposed to the internet; strong password; encrypted at rest and in backups.
- [ ] `APP_ENV=prod`, `LOG_LEVEL=INFO`.
- [ ] The container runs as non-root (the default image does).
- [ ] Dependencies updated regularly; VAPT done before go-live (see [COMPLIANCE.md](COMPLIANCE.md#pre-go-live-sign-off)).
- [ ] Access to transcripts and exports limited to staff who need it; exported lead CSVs handled as personal data.

# Twilio setup for the closed POC

This guide gets the bot calling **your own test phone** through Twilio, with Claude running the
conversation. It assumes you run the app on your laptop and expose it to Twilio with ngrok.

> Closed-environment testing only. Live outreach to distributors in India needs a DLT-registered
> 140-series number through an Indian telecom provider. See docs/COMPLIANCE.md. Moving to that later
> means changing the provider and caller-ID settings. The bot itself does not change.

## Can I use my personal number as the caller ID?

You can try, but it will very likely fail. Use a Twilio number instead.

* Twilio lets you call **from** either a number bought on Twilio or a number you have verified
  under *Verified Caller IDs*. So a personal mobile can be added as a caller ID.
* However, Twilio carries the call into India over an international route. Indian operators now
  block incoming international calls that show an Indian (+91) caller ID. This is the DoT
  anti-spoofing system introduced in 2024. A Twilio call showing your personal +91 number is
  therefore likely to be blocked or never ring.
* **Recommended for the POC:** use the Twilio number you received at sign-up (often a US
  number) as `TWILIO_FROM_NUMBER`. The call will show up on your phone as an international number,
  which is fine for testing.
* **At go-live:** switch to your DLT-registered 140-series number through an Indian provider. Only
  the `TELEPHONY_PROVIDER` / caller-ID settings change.

Your personal number is still useful as the number **being called**. Put it in the test data as
the distributor's mobile (the dummy data already does this).

## 1. Collect your Twilio credentials

In the Twilio Console home page (*Account Info*), copy:

| Console field | `.env` variable |
|---|---|
| Account SID (starts with `AC`) | `TWILIO_ACCOUNT_SID` |
| Auth Token | `TWILIO_AUTH_TOKEN` |
| Your Twilio phone number (E.164, e.g. `+1415...`) | `TWILIO_FROM_NUMBER` |

If you have no number yet, go to *Phone Numbers → Manage → Buy a number* and pick one with
**Voice** capability. A trial account can usually claim one free number.

## 2. Allow calls to India

*Voice → Settings → Geo permissions* → tick **India** → Save. Without this Twilio rejects
the call (error 21215 / "geo permissions").

## 3. Verify the test phone (trial accounts)

Trial accounts can only call **verified** numbers. Go to *Phone Numbers → Manage → Verified
Caller IDs → Add a new Caller ID*. Enter the test mobile in `+91XXXXXXXXXX` format and complete
the verification call/SMS.

Trial-account behaviour to expect:
* Each call starts with a short Twilio announcement that it comes from a trial account. You must
  **press any key** on the phone before the bot speaks.
* Calls to unverified numbers fail with error 21219.
* Upgrading the account (adding billing) removes both restrictions.

## 4. Get a public URL with ngrok

Twilio must reach the app's webhooks over the internet.

```bash
# install from https://ngrok.com/download, then:
ngrok http 8000
```

Copy the `https://....ngrok-free.app` forwarding URL into `PUBLIC_BASE_URL`. It must match
**exactly**, with no trailing slash. Twilio's request signatures are checked against it, and a
mismatch shows up as HTTP 403 in the ngrok console. The free ngrok URL changes every restart,
so update `.env` and restart the app each time.

## 5. Configure `.env`

```dotenv
# Claude
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-5-5
LLM_EFFORT=low
ANTHROPIC_API_KEY=sk-ant-...

# Twilio
TELEPHONY_PROVIDER=twilio
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+1...
TWILIO_MACHINE_DETECTION=false   # answering-machine detection adds a few seconds of silence; off for the POC
TWILIO_RECORD_CALLS=false

PUBLIC_BASE_URL=https://xxxx.ngrok-free.app
ADMIN_USERNAME=admin
ADMIN_PASSWORD=choose-a-password
```

## 6. Load the test distributor and start the app

```bash
pip install -e ".[dev]"
callingbot init-db
callingbot check-config                       # validates the YAML content and your settings
callingbot import-distributors Dummy_data.xlsx --source poc
callingbot serve                              # terminal 1 - dashboard at http://localhost:8000
```

Before dialling, test the conversation without a phone. Use the browser simulator at
http://localhost:8000/simulator or run `callingbot simulate`.

## 7. Place a test call

```bash
# terminal 2
callingbot test-call --phone +919769116626 --ignore-window
```

`test-call` dials one imported distributor immediately, without a campaign. It still respects
the do-not-call list. `--ignore-window` lets you test outside the 10:00–19:00 IST calling window.
That is fine for your own phone, but never use it for real distributors. Afterwards, open the
dashboard (*Calls*) to see the transcript, outcome and any links sent.

## Messages (empanelment link)

By default, messages are stored in the **outbox** (*Messages* page) and nothing is actually sent.
To receive the link on your phone during the POC:

* **WhatsApp (easiest for testing):** join the Twilio WhatsApp Sandbox from your phone
  (*Messaging → Try it out → Send a WhatsApp message*). Then set `WHATSAPP_PROVIDER=twilio` and
  `TWILIO_WHATSAPP_FROM=whatsapp:+14155238886`, or whatever sandbox number the console shows.
* **SMS:** commercial SMS to Indian numbers needs DLT registration of the sender and template.
  Expect delivery problems until that is done.
* **Email:** set `EMAIL_PROVIDER=smtp` and the `SMTP_*` settings.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `TelephonyError ... 21219` | Trial account calling an unverified number - verify it (step 3) |
| `TelephonyError ... 21215` / geo permission | India not enabled (step 2) |
| Phone never rings | International route blocked or caller ID rejected - use the Twilio number as caller ID |
| Call connects, then "an application error has occurred" | Twilio could not reach the webhook: app not running, wrong `PUBLIC_BASE_URL`, or ngrok restarted |
| 403 on `/telephony/twilio/...` in the ngrok console | `PUBLIC_BASE_URL` does not exactly match the ngrok URL |
| Bot doesn't understand replies | Speak after the bot finishes; check the transcript on the *Calls* page. Accents and noise affect Twilio speech recognition |
| Long pauses before replies | Normal for turn-based voice (speech recognition, then Claude, then speech). Phase 3 adds streaming voice |

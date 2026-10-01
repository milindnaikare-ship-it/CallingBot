# Development plan

The plan for taking CallingBot from this MVP to a production outreach channel for the AMC's
distribution team. Durations are **rough estimates in calendar weeks**. They assume the scope
inputs arrive on time, and they depend heavily on telecom and regulatory approvals that are
outside the development team's control.

```
Phase 0  Discovery & scope        ██                       1-2 wks
Phase 1  MVP (this build)           ████                   3-4 wks  (done, pending AMC content)
Phase 2  Pilot                          ██████             4-6 wks  (calendar driven by DLT/telecom approvals)
Phase 3  Real-time voice & scale              ████████     6-10 wks (can start during Phase 2)
Phase 4  Integrations                         ██████       4-8 wks  (can run in parallel with Phase 3)
Phase 5  Optimisation                                ───►  ongoing
```

---

## How we work together

| Who | Responsibilities |
|---|---|
| **AMC: Head of Distribution / Sales (business owner)** | Owns the objectives and KPIs; answers the [scope questionnaire](SCOPE_QUESTIONNAIRE.md); decides priorities; nominates the pilot distributors and RMs. |
| **AMC: Compliance / Legal** | Approves scripts and YAML content, the calling policy and the data-handling model; signs the [pre-go-live checklist](COMPLIANCE.md#pre-go-live-sign-off). |
| **AMC: InfoSec / IT** | Hosting, network access, secrets, VAPT, data residency decisions. |
| **AMC: Distribution Ops** | Empanelment process, documents, SLAs, the link/portal; updates "empanelled" status. |
| **Developer** | Builds and tests the software, deploys it, tunes prompts, reports on progress and call quality. |

**Rhythm**

- **Weekly review (45 min):** demo of what was built, KPIs and transcript highlights (from the
  pilot onwards), open decisions, next week's plan.
- **Decision log:** every scope or compliance decision is written down (a shared sheet or an
  issue) with an owner and a date.
- **Change control for content:** the AMC sends changes to `config/*.yaml` with Compliance
  approval attached. The developer applies them, tags the release and confirms in the simulator.
- **Acceptance:** each phase ends with a sign-off against its acceptance criteria below.

---

## Phase 0: Discovery and scope (1-2 weeks)

**Goal:** agree what success looks like and collect everything the bot needs to say and do.

**Deliverables**
- Completed [SCOPE_QUESTIONNAIRE.md](SCOPE_QUESTIONNAIRE.md): AMC profile, NFO details (SID/KIM),
  distributor universe, KPIs, languages, empanelment process, RM handoff, telephony, messaging,
  compliance owners, hosting, timeline.
- Agreed KPIs and targets (connect rate, conversation completion, link-sent rate, empanelment
  conversion, opt-out rate, compliance flag rate).
- Decision on the telephony route for the pilot and for production. Applications started for DLT
  Principal Entity registration and numbers, if not already in place.
- Compliance's position on the open questions in [COMPLIANCE.md](COMPLIANCE.md) (number series,
  disclaimer placement, AI-composed wording, data residency).

**Acceptance criteria**
- Questionnaire answered or each open item has an owner and a date.
- KPI definitions signed off by the business owner.

**Dependencies:** AMC stakeholders available; draft SID/KIM available.

## Phase 1: MVP (this build, 3-4 weeks)

**Goal:** a working, compliant, demonstrable bot that can run a small campaign end-to-end.

**Included in this build**
- Turn-based voice conversation: provider TTS/STT plus Claude (`claude-opus-5-5`, effort `low`,
  configurable), with an offline demo bot (`LLM_PROVIDER=fake`).
- English and Hindi, with an in-call language switch; more languages through configuration.
- Pre-approved greeting with virtual-assistant and recording disclosure; approved-knowledge-only
  answers from `config/*.yaml`.
- Tools: verify ARN, update details, send the empanelment link, schedule a callback, set the
  language, transfer to an RM, opt out, record the outcome, end the call.
- Compliance guardrails: calling window and holidays, internal DNC list, deterministic opt-out
  detection with an engine safety net, a compliance screen on every bot sentence, the SEBI
  warning before a connected call ends, attempt limits and backoff, no sensitive data
  collection, PII minimisation towards the LLM, an audit log.
- Telephony: simulator (web page and terminal), Twilio (complete), Exotel (outbound and status).
- Messaging: outbox by default; Twilio SMS, Meta WhatsApp templates and SMTP e-mail adapters.
- Tracked empanelment links with click recording.
- Distributor CSV import (AMFI-export compatible) with validation; campaigns; dialer process;
  stats; leads export.
- Admin dashboard (HTTP Basic): stats, distributors, campaigns, calls with transcripts,
  callbacks, outbox, CSV import, leads export.
- CLI, Docker image, docker-compose (app, dialer, Postgres), CI (lint, format, tests), and the
  documentation set.

**Acceptance criteria**
- Automated test suite and lint pass in CI.
- A full simulated conversation in English and in Hindi reaches each outcome: link sent,
  callback, transfer, not interested, already empanelled, wrong person, opt-out.
- Adversarial simulator tests (guaranteed returns, projections, commission figures, advice,
  inducements) produce no unscreened violations.
- A test call through Twilio to an internal number completes with the greeting, the
  conversation, the warning and the recorded outcome.

**Dependencies:** none beyond Phase 0 content for a meaningful demo. Placeholder content works
for internal demos.

## Phase 2: Pilot (4-6 weeks, driven by approvals)

**Goal:** prove the bot with real distributors, safely, and tune it on real transcripts.

**Deliverables**
- Production telephony account in India with DLT registration, the correct number series
  (140-series or as Compliance confirms) and caller ID. Or a Compliance-approved interim route.
- Compliance-approved `config/*.yaml` (final SID/KIM facts, greetings, disclaimers and
  translations, FAQs, commission wording).
- Approved SMS DLT templates and/or WhatsApp templates, with messaging switched off `outbox`.
- Hosting in the agreed region with PostgreSQL, HTTPS and restricted admin access; backups.
- **Pilot campaign with 50-100 friendly distributors** who have been told about the pilot.
- **Daily transcript QA** (a sample plus every flagged call) using an agreed scorecard:
  accuracy, compliance, tone, outcome correctness.
- Prompt and FAQ tuning from QA findings; KPI report at the end of the pilot.
- NCPR/DLT preference scrubbing built into the import process; bulk DNC upload.
- Retention job for transcripts and recordings as agreed with Legal.

**Acceptance criteria (targets to be agreed in Phase 0)**
- Zero unremediated compliance breaches. Every flagged turn reviewed; no confirmed
  mis-statement of scheme facts.
- 100% of opt-outs honoured (verified by test calls and an audit log review).
- The SEBI warning is present on every connected call that reached the close.
- Agreed thresholds met for connect rate, conversation completion and link-sent rate. Opt-out
  and complaint rates below the agreed ceiling.
- Median bot response time measured and reported, as the input to Phase 3.
- Go / no-go decision for scale-up signed by the business owner and Compliance.

**Dependencies:** DLT and telecom approvals (often the longest lead time), Compliance sign-off,
InfoSec hosting approval, RM availability for callbacks and transfers.

## Phase 3: Real-time voice and scale (6-10 weeks)

**Goal:** natural, low-latency conversations that can be interrupted, at production volume.

**Deliverables**
- **Streaming pipeline:** bidirectional audio over WebSocket, streaming STT, streaming LLM output,
  streaming TTS, with **barge-in** (the distributor can interrupt).
- **Indian-language speech vendors** evaluated on real pilot audio for Hindi, English (Indian
  accents) and Hinglish. More languages (Marathi, Gujarati, Tamil, and others) as scripts are
  approved.
- **Exotel Voicebot** integration (or the chosen Indian provider's streaming interface), so the
  conversation runs on DLT-registered Indian numbers.
- **Latency targets** (to be agreed): for example, a median under about 1 second and a 95th
  percentile under about 2 seconds from the end of the distributor's speech to the start of the
  bot's audio.
- **Horizontal scaling:** several app replicas on PostgreSQL, a dialer with distributed locking,
  connection pooling, load tests at the target concurrency.
- **Monitoring:** latency, error and flag-rate dashboards; alerting; structured logs.
- Database migrations (Alembic).

**Acceptance criteria**
- Latency targets met at target concurrency in a load test and on live pilot calls.
- Barge-in works reliably, with no lost or garbled turns in the QA sample.
- No regression in compliance metrics compared with Phase 2.

**Dependencies:** pilot audio for vendor evaluation; provider streaming access (Exotel Voicebot
or equivalent); InfoSec approval of the speech vendors (data residency).

## Phase 4: Integrations (4-8 weeks)

**Goal:** fit the bot into the AMC's systems, so nothing is re-keyed and RMs act quickly.

**Deliverables**
- **CRM integration:** import distributor lists from the CRM and push call outcomes, summaries
  and callbacks back.
- **Empanelment portal status sync:** automatically mark distributors `empanelled` (and stop
  calling them) when the portal confirms; measure true conversion.
- **WhatsApp follow-ups:** approved template sequences (link reminder, NFO closing reminder) for
  distributors who asked for them. Opt-out honoured across channels.
- **RM tasking:** callbacks and hot leads assigned to the right RM by region, with SLA tracking
  and reminders.
- Single sign-on for the admin dashboard, if required by InfoSec.

**Acceptance criteria**
- Outcomes appear in the CRM within the agreed time, with no duplicates.
- Empanelment status matches the portal for a reconciliation sample.
- Callbacks assigned and tracked against the SLA.

**Dependencies:** CRM and portal API access and documentation; WhatsApp BSP account and approved
templates; regional RM mapping.

## Phase 5: Optimisation (ongoing)

**Goal:** steadily improve conversion and quality, without compromising compliance.

**Deliverables**
- **A/B testing of approved script variants** (openers, value propositions, CTA order), each
  variant approved by Compliance.
- **QA sampling programme:** a fixed weekly sample plus all flagged calls, with scorecards and
  trends.
- **Conversion analytics:** a funnel from dialled to connected to engaged to link sent to link
  clicked to empanelled, by segment, region, language, time of day and attempt number.
- Best-time-to-call and retry-policy tuning, within the permitted window.
- Model and cost review: compare models and effort settings on quality, latency and cost.

**Acceptance criteria:** agreed quarterly KPI improvements; compliance metrics stay at Phase 2
levels or better.

---

## Risk register

| # | Risk | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
| 1 | **Regulatory breach** (telemarketing rules, SEBI advertising code, DPDP) | Medium | High | Compliance sign-off on all content and policy; calling window, DNC and opt-out enforced in code; [COMPLIANCE.md](COMPLIANCE.md) checklist; audit log; pilot before scale. | Compliance + Developer |
| 2 | **Telephony approvals delayed** (DLT registration, number series, provider onboarding) | High | High | Start applications in Phase 0; use the simulator and Twilio for internal testing meanwhile; keep the provider layer pluggable (Exotel or others). | AMC Compliance / IT |
| 3 | **Latency makes the conversation feel unnatural** | Medium | Medium | Effort `low`, prompt caching, concise knowledge; measure in the pilot; Phase 3 streaming and barge-in; evaluate a faster model if needed. | Developer |
| 4 | **Hallucination**: the bot states something not approved | Medium | High | Approved-knowledge-only instructions; deterministic compliance screen on every sentence; fixed approved text for sensitive topics (commission, disclaimer); RM callback for unknowns; transcript QA and flag review. | Developer + Compliance |
| 5 | **Data privacy incident or cross-border concern** | Low-Medium | High | No phone or e-mail sent to the LLM; minimal data imported; HTTPS and restricted admin; residency decision by InfoSec and Legal; retention job; processor contracts. | InfoSec + Legal |
| 6 | **Distributor annoyance / brand damage** (too many calls, wrong timing, robotic feel) | Medium | Medium | Business-hours window; attempt limits and backoff; instant opt-out; AI disclosure and an easy route to a human; pilot with friendly distributors first; monitor opt-out and complaint rates. | Distribution |
| 7 | **Speech recognition errors** (accents, noise, Hinglish) | Medium | Medium | Domain speech hints; read-back of key details; ask to repeat; Phase 3 Indian-language STT vendors. | Developer |
| 8 | **Outdated content** (SID addendum, changed dates) | Low | High | YAML is the single source; change control with Compliance approval; tagged releases. | Product + Compliance |
| 9 | **LLM or provider outage** | Low | Medium | Graceful failure (apology, automatic RM callback, hang-up); retries; alerting; pause the campaign. | Developer |
| 10 | **Calling distributors with expired ARNs or already empanelled** | Medium | Low | ARN validity imported; funnel excludes empanelled distributors; Phase 4 portal sync. | Distribution Ops |

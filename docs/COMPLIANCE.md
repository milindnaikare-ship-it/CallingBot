# Compliance guide (India)

This guide is for the AMC's **Compliance, Legal, InfoSec and Distribution** teams, and for the
developers who build the bot. It lists the Indian rules we believe apply to an AI voice bot that
calls mutual fund distributors about an NFO and about empanelment. For each rule it says what
the software does, what the AMC must do, and what still has to be confirmed.

> [!IMPORTANT]
> **This is an engineering checklist, not legal advice.** Regulations, circulars and their
> interpretation change. Where this document says **"Confirm"**, the AMC's Compliance or Legal
> team must give the answer, and that answer overrides anything written here. We have
> deliberately not quoted circular numbers or dates we could not verify. Please add the exact
> references your team relies on.

**Status legend** (used in the table below)

| Status | Meaning |
|---|---|
| **Built** | Implemented and enforced by the software in this build. |
| **Built, confirm** | Implemented, but the default (wording, timing, threshold) needs Compliance sign-off. |
| **Configure** | The software supports it; the AMC must supply approved content or settings. |
| **AMC action** | Outside the software: registration, contract, policy or process owned by the AMC. |
| **Gap** | Not in the MVP. Planned (see [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)) or handled manually until then. |

---

## 1. Checklist

| # | Requirement | Why | How the bot handles it | Owner | Status |
|---|---|---|---|---|---|
| 1 | Register the AMC as a **Principal Entity** on a telecom operator's DLT platform | TRAI TCCCPR (telemarketing) | Not a software feature. The caller ID used by the bot must belong to the AMC's registered set-up. | Compliance + telecom provider | AMC action |
| 2 | Call through a **registered telemarketer / CPaaS** from the **correct number series** (promotional vs service) | TRAI TCCCPR, DoT numbering | Caller ID is set by `TWILIO_FROM_NUMBER` or `EXOTEL_CALLER_ID`. The Exotel adapter is built for Indian DLT-registered numbers. | Compliance + telecom provider | AMC action, **Confirm** series |
| 3 | **Scrub preferences** (NCPR/DND) and honour recorded consent before dialling | TRAI TCCCPR | Internal DNC list is checked before every dial. NCPR/DLT scrubbing is **not** built in: import only lists already scrubbed by the provider or DLT platform. | Compliance + telecom provider | Gap (scrub upstream) |
| 4 | Call only in **permitted hours and days** | TRAI TCCCPR, AMC policy | The dialer dials only inside the window in `config/campaign.yaml`: by default Mon to Sat, 10:00 to 19:00 IST, skipping listed holidays. Callbacks are offered inside the window. | Compliance | Built, confirm window |
| 5 | **Honour opt-outs** immediately and permanently | TRAI TCCCPR, DPDP Act | Deterministic opt-out detector (English, Hinglish, Hindi) plus an `opt_out` tool. The number goes on the internal DNC list, the distributor is marked do-not-call, the event is audited, and that number is never dialled again. If the model misses an opt-out, the engine opts the distributor out itself. | Built-in | Built |
| 6 | Limit **call attempts** and avoid harassment | TRAI spirit, AMFI code of conduct, reputation | `max_attempts` (default 3) per distributor per campaign, with waits between retries (default 3 h, then 1 day). Final outcomes stop further calls. | Distribution + Compliance | Built, confirm limits |
| 7 | **Identify the caller and purpose** | Fair practice, TRAI | The pre-approved greeting names the assistant and the AMC. The purpose (NFO, empanelment) follows the identity check. | Compliance | Built, confirm wording |
| 8 | **AI disclosure** | Fair practice; avoid deception | The greeting says "virtual assistant". The bot never claims to be human and says so honestly if asked. It offers a human RM. | Compliance | Built, confirm wording |
| 9 | **Recording disclosure** | Notice/consent; DPDP Act | The greeting says the call "may be recorded". Provider recording is **off** by default (`TWILIO_RECORD_CALLS=false`). Transcripts are always stored. | Compliance + Legal | Built, confirm wording and retention |
| 10 | Speak the **SEBI standard warning** clearly | SEBI MF advertising code | The text comes from `mandatory_disclaimer` in `config/nfo.yaml` (with per-language translations). The engine speaks it before a connected call ends. | Compliance | Built, **Confirm** translation and placement |
| 11 | **No guaranteed or assured returns, no projections, no past performance, no "risk-free"** | SEBI MF advertising code | The system prompt forbids these. A deterministic screen checks **every** bot sentence before it is spoken (`guaranteed_returns`, `return_projection`, `past_performance`, `risk_free`). Blocked text is replaced with a safe line, flagged and audited. | Built-in | Built |
| 12 | Scheme content must **match the SID/KIM** | SEBI | The bot may state only facts in `config/nfo.yaml`, copied from the approved SID/KIM. Anything else becomes an RM callback. | Product + Compliance | Configure |
| 13 | **Compliance pre-approval** of scripts | SEBI advertising code, AMC policy | Greetings, disclaimers, FAQs, commission wording and value propositions are fixed approved text in YAML. Claude composes the other sentences from approved facts, inside guardrails. Compliance must accept this model (see section 3). | Compliance | **Confirm** approach |
| 14 | **No investment advice** | SEBI (Investment Advisers) regulations; distributor role | Prompt rule plus the `advice` screen rule. The bot does not recommend schemes, allocations or suitability. | Built-in | Built |
| 15 | **Commission**: no upfront commission, trail only, no unapproved figures | SEBI commission rules | The bot uses only `commission_response` from `nfo.yaml` (non-numeric by default). Any spoken figure is blocked (`commission_figure` rule). | Compliance | Built, confirm wording |
| 16 | **No inducements** (gifts, trips, extra incentives) | SEBI / AMFI code of conduct | Prompt rule plus the `inducement` screen rule. | Built-in | Built |
| 17 | Empanel only distributors with a **valid ARN, KYD and EUIN** where applicable | AMFI / SEBI | `ARN Valid Till` and EUIN are imported and validated. The bot never collects documents; the AMC's empanelment process verifies ARN, KYD and EUIN. | Distribution Ops | AMC action |
| 18 | Handle **RIAs** correctly | SEBI IA regulations (advice vs distribution) | An approved FAQ answer routes RIAs to the right AMC team (RM callback) with no commission pitch. | Compliance | Built, confirm wording |
| 19 | **Never collect sensitive data** (PAN, bank, Aadhaar, OTP) | DPDP minimisation; fraud prevention | The bot is instructed never to ask for them, and to stop a distributor who starts reading them out. Documents are submitted only on the empanelment portal. | Built-in | Built |
| 20 | **Notice and lawful basis** for processing distributors' personal data | DPDP Act 2023 | The FAQ answers "How did you get my number?" and offers removal. Formal notice wording and the legal basis are for Legal. | Legal | **Confirm** |
| 21 | **Purpose limitation and minimisation** | DPDP Act 2023 | Data is used only for this outreach. The LLM receives name, firm, city, ARN and funnel status, but **not** phone numbers or e-mail addresses. Tracking links carry ids only. Logs mask phone numbers. | Built-in + Legal | Built |
| 22 | **Retention and erasure** | DPDP Act 2023 | No automatic deletion in the MVP. Erasure is a manual database task, and the number stays on the DNC list so the opt-out keeps working. | Legal + IT | Gap |
| 23 | **Grievance contact** | DPDP Act 2023 | The bot can give the distributor helpline and e-mail from `amc.yaml`. Legal designates the grievance officer or contact. | Legal | AMC action |
| 24 | **Data residency, cross-border transfer and outsourcing** (LLM API, telephony, hosting) | DPDP Act, SEBI cloud and cybersecurity frameworks | Hosting region is your choice. The LLM and telephony providers are external processors. See section 8. | InfoSec + Legal | **Confirm** |
| 25 | **Cybersecurity**: SEBI framework obligations, VAPT, access control | SEBI cybersecurity framework for regulated entities | HTTPS behind a reverse proxy, admin login, signed Twilio webhooks, signed links, secrets in the environment. VAPT and onboarding into the AMC's security programme are pending. | InfoSec | AMC action |
| 26 | **SMS (DLT templates) and WhatsApp (approved templates)** for links | TRAI DLT; WhatsApp Business policy | Messaging defaults to `outbox` (stored for manual sending). Switch providers on only after the header, templates and sender are approved. | Compliance + Marketing | Configure |
| 27 | **Audit trail** | Evidence for regulators and internal audit | Append-only `audit_events` table plus full transcripts, outbound messages and link clicks. See section 9. | Built-in | Built |

---

## 2. TRAI TCCCPR and DLT (telemarketing)

The Telecom Commercial Communications Customer Preference Regulations (TCCCPR), with their later
amendments, govern commercial calls and SMS in India. They are implemented through
**Distributed Ledger Technology (DLT)** platforms run by the telecom operators. Our understanding
of what applies (**Compliance to confirm each point**):

- **Principal Entity (PE) registration.** The AMC, as the business on whose behalf the
  communication is made, registers on an operator's DLT platform. SMS sender headers and
  content templates are also registered there.
- **Registered telemarketer / CPaaS.** Calls should be placed through a telemarketer or CPaaS
  provider registered for commercial communication, using numbers allotted for that purpose.
  The provider integration (Twilio for pilots; Exotel or another Indian provider for production)
  must be agreed with Compliance.
- **Number series: promotional vs service.** Promotional voice calls in India are expected to
  come from the dedicated **140-series**. Our understanding is that DoT has also introduced a
  **160-series** for service and transactional calls, and that SEBI has asked its regulated
  entities to adopt it for service and transactional calls. **Confirm which category this
  outreach falls into.** A call that invites a distributor to empanel and promotes an NFO looks
  promotional to us, but this is a Compliance decision. The MVP assumes **promotional**.
- **Preferences and consent.** People can register "do not disturb" preferences through the
  National Customer Preference Register (NCPR), for example via 1909. Promotional calls should
  not reach numbers that block the category unless valid consent is recorded on DLT.
  Distributors often use personal mobiles, so the business nature of the call should not be
  assumed to exempt it. **The MVP does not query NCPR or DLT consent.** It relies on scrubbing
  by the provider or DLT platform before import, plus its internal DNC list. Integrating
  scrubbing is a Phase 2 item.
- **Calling hours.** The bot enforces the window in `config/campaign.yaml`. The default,
  Monday to Saturday 10:00 to 19:00 IST with no calls on listed holidays, is intentionally
  narrower than general telemarketing practice. **Compliance to confirm the window and the
  holiday list.**
- **Honour opt-outs.** Any "don't call me again" is final: the number is added to the internal
  DNC list (source `call_opt_out`) and never dialled again by any campaign. Opt-outs
  received through other channels (e-mail, RM, complaints) must be added to the same list. In
  the MVP a developer does this with `compliance.add_to_dnc`; a bulk upload screen would be a
  sensible Phase 2 addition.
- **Consequences of getting this wrong.** Commercial calls from unregistered numbers or
  unregistered telemarketers can lead to the numbers being disconnected, the sender being
  blacklisted across operators, and action against the principal entity. They also do
  reputational harm with distributors and invite regulatory scrutiny. TRAI has been tightening
  enforcement. **Do not run live outreach from unregistered or international numbers without
  explicit Compliance approval.**

## 3. SEBI advertising code for mutual funds

Scripts and sales communication about a scheme are expected to follow SEBI's advertising code
for mutual funds and the AMC's internal approval process. **Compliance to confirm whether an
outbound call script counts as an advertisement or sales literature, and which requirements
apply to audio.**

- **Standard warning.** *"Mutual Fund investments are subject to market risks, read all scheme
  related documents carefully."* The bot speaks it clearly, at the normal speaking pace of the
  TTS voice, before any connected call ends, in the language of the call. The English text is
  `mandatory_disclaimer` in `config/nfo.yaml`. Translations are in
  `mandatory_disclaimer_translations` (Hindi is provided). **Confirm:** (a) whether a translated
  warning is acceptable or the English text must also be spoken; (b) whether it must also be
  spoken earlier in the call; (c) the approved translation for each language you enable.
- **No return assurance or projection.** The bot never says or implies guaranteed, assured,
  fixed or "sure-shot" returns, never projects returns ("12% a year", "doubles in five years"),
  and never calls the scheme safe or risk-free. The system prompt instructs this, and the
  deterministic screen enforces it on every sentence. A blocked sentence is replaced with a
  neutral line offering an RM callback, marked `flagged` in the transcript, and audited as
  `compliance_flag`.
- **An NFO has no performance track record.** The bot says so when asked about returns. It does
  not cite the performance of other schemes, the fund manager's past results or index returns
  (`past_performance` rule).
- **Riskometer.** The bot states the riskometer level from `nfo.yaml` when risk comes up.
- **Content must match the SID/KIM.** `config/nfo.yaml` is the single source of scheme facts:
  name, category, type, objective, benchmark, fund managers, dates, minimums, plans, exit load,
  riskometer and highlights. Copy it from the final SID/KIM and have Compliance sign it off.
  Re-check it if an addendum is issued.
- **Pre-approval of scripts.** These parts are fixed, approved text and are spoken
  word-for-word: the greeting, the disclaimer, the commission response, the FAQ answers and the
  re-prompt and safe-reply lines. Claude composes the other sentences live from the approved
  facts, so they **cannot all be pre-approved word-for-word**. Controls that compensate:
  approved-knowledge-only instructions, the deterministic screen, flagged-turn review, full
  transcripts, and QA sampling in the pilot. **Compliance to confirm this model is acceptable,
  or tell us which topics must be answered only with fixed approved text.**

## 4. SEBI commission rules and the AMFI code of conduct

- **No upfront commission; trail only.** Our understanding is that SEBI requires mutual fund
  commissions to be paid on a trail basis, with no upfront commission. The bot speaks only the
  approved `commission_response` (default: the brokerage structure will be shared in writing by
  the RM after empanelment; no upfront commission; trail based). It never quotes a figure, and
  any figure in a reply is blocked (`commission_figure`). If Compliance approves specific
  numbers later, both the YAML and the screen rule must change together.
- **No inducements.** The bot never offers gifts, trips, contests, extra incentives or "special"
  rates to win empanelment or NFO business (`inducement` rule).
- **ARN validity.** ARNs have an expiry date. The importer reads and stores `ARN Valid Till`. The empanelment team must not empanel a distributor whose ARN has expired.
  **Confirm** whether expired-ARN distributors should be excluded from campaigns. The MVP
  imports them so that ops can see the data.
- **KYD (Know Your Distributor).** KYD compliance is verified in the empanelment process, not
  on the call. The bot only lists the "KYD acknowledgement" among the documents to upload, when
  that is in `amc.yaml`.
- **EUIN.** The EUIN identifies the individual employee or salesperson of a distributor firm.
  It is imported when available. The empanelment process should capture it as AMFI requires.
- **AMFI code of conduct.** Distributors are bound by AMFI's code of conduct for
  intermediaries. The bot's own conduct follows the same spirit: honest, no mis-selling, no
  disparaging other AMCs, no comparisons it cannot support, and it respects the distributor's
  time and choices.
- **RIAs.** A SEBI-registered investment adviser should not be pitched distribution commission.
  The bot uses the approved RIA answer and routes the contact to the right AMC team.

## 5. DPDP Act 2023 (personal data)

Distributors are individuals (or contact persons of firms), so their names, phone numbers,
e-mail addresses, ARNs, voices, call recordings and transcripts are **personal data**.

- **Notice and lawful basis.** Legal should decide the lawful basis for processing (consent or a
  "legitimate use" under the Act) and the notice the AMC gives. The bot's FAQ answer to "How did
  you get my number?" is a placeholder for that notice.
- **Publicly available data.** The Act's exclusion for personal data that the person (or someone
  under a legal obligation) has made publicly available may be relevant, because AMFI publishes
  distributor details. **Whether that exclusion covers this outreach is a question for Legal.**
  Even if it does, we recommend following the principles below.
- **Purpose limitation.** Use the distributor list only for NFO awareness and empanelment.
  Reusing it for unrelated marketing needs its own basis.
- **Minimisation.**
  - The importer reads only the columns listed in the README. Other columns in the CSV are
    ignored.
  - The LLM receives name, firm, city, ARN, funnel status, language and prior call summaries. It
    does **not** receive phone numbers or e-mail addresses; tools look those up server-side.
  - The bot never asks for PAN, bank details, Aadhaar or OTPs. It is instructed to stop a
    distributor who starts reading them out and to point to the secure portal. Anything a distributor volunteers
    can still end up in the transcript. Automatic redaction is a planned improvement.
  - Tracking links carry only internal ids.
- **Retention.** Agree retention periods for call records, transcripts, recordings, outbound
  messages, link clicks and audit events. The MVP keeps everything until it is deleted manually.
  A scheduled retention job is planned.
- **Rights of the data principal.** Access, correction, erasure and grievance redressal. The bot
  honours opt-outs instantly. Corrections can be made by re-importing the corrected row, or by
  the bot's `update_distributor_details` tool during a call.
  Erasure is a manual database task in the MVP. **Confirm with Legal** that keeping the phone
  number on a suppression (DNC) list after erasure is acceptable; without it, a later import
  could call the person again.
- **Grievance contact.** Publish the grievance officer or contact required by the Act. The bot
  can give the distributor helpline and e-mail configured in `amc.yaml`.
- **Processors.** Anthropic (LLM), the telephony provider, and SMS, WhatsApp and e-mail providers
  process personal data on the AMC's behalf. Put appropriate contracts and data processing terms
  in place.
- **DPDP Rules.** The Rules under the Act phase in obligations over time. **Confirm with Legal
  which obligations apply now** and the timeline for the rest.

## 6. Call recording: disclosure and retention

- The approved greeting in every language says the call **may be recorded** for quality and
  compliance purposes. Keep that sentence in any new language you add.
- **Provider recording is off by default.** With `TWILIO_RECORD_CALLS=true`, Twilio records the
  whole call and stores the recording in Twilio's cloud. The app stores only the recording URL.
  Decide where recordings may be stored (residency), who may access them and how long to keep
  them before you switch this on.
- **Transcripts are always stored** (the `turns` table and the raw LLM conversation). They are a
  record of the call even without audio, so they fall under the same retention and access rules.
- **Confirm** whether recording needs explicit consent rather than notice, and what the bot
  should do if a distributor objects to recording. Our suggestion: offer an RM callback and end
  the call.

## 7. AI disclosure

- The greeting introduces the bot by name as **"a virtual assistant calling on behalf of"** the
  AMC.
- If asked "Are you a robot / a real person?", the bot answers truthfully that it is an AI
  virtual assistant and offers a human RM: a live transfer when an RM desk number
  (`RM_TRANSFER_NUMBER`) is configured, otherwise a callback.
- The bot never claims to be human, never invents a human name, title or location, and never
  denies being automated.
- **Confirm** the disclosure wording with Compliance. Keep an eye on developing guidance on AI in
  customer communication from SEBI and other authorities.

## 8. Data residency and cross-border processing

Where each piece of data goes:

| Component | Data it sees | Where it runs |
|---|---|---|
| CallingBot app + database | Everything (distributor list, transcripts, outcomes, audit) | Wherever you host it. Choose an Indian cloud region or your own data centre. |
| Claude (Anthropic API) | Call context (name, firm, city, ARN, status, prior summaries) and the live conversation text. **No phone numbers or e-mails.** | Anthropic's infrastructure, which may be outside India. **Confirm** with Anthropic and InfoSec. |
| Telephony provider (Twilio / Exotel / other) | Phone numbers, audio, speech-to-text, recordings if enabled | Twilio is a US-based provider; Exotel is an Indian provider. Check each contract for processing locations. |
| Messaging providers | Phone number or e-mail and the message text | Per provider. |

Questions for **InfoSec and Legal**:

1. **DPDP Act.** Cross-border transfer of personal data is generally permitted except to
   countries the government restricts. Sector regulators can impose stricter rules.
   **Confirm** the position for this data.
2. **SEBI frameworks.** SEBI's cybersecurity and cyber-resilience framework for regulated
   entities, and its framework for adopting cloud services, contain expectations on data
   localisation, outsourcing and vendor risk. **Confirm** whether sending conversation text to
   an LLM API outside India is acceptable for this use, and how this system is classified in
   the AMC's IT inventory.
3. **Mitigations available today.** No phone or e-mail is sent to the LLM. Hosting can be in
   India. Messaging can stay in outbox mode. Recording can stay off.
4. **Options if residency is required.** Claude is also offered through major cloud platforms.
   Check whether one is available in an Indian region and meets your requirements. That would
   need a small adapter change, and `LLM_ENABLE_FALLBACKS=false`. Also ask Anthropic about
   enterprise data-handling terms, such as retention commitments.

## 9. Audit trail

Evidence the system keeps for Compliance and internal audit:

- **`audit_events`** (append-only in the application) with these event kinds:
  `disclosure_played`, `dnc_added`, `opt_out`, `compliance_flag`, `llm_error`, `llm_refusal`,
  `link_sent`, `callback_scheduled`, `request_logged` (follow-up for the partner team), `transfer`,
  `dial_skipped`, `dial_failed`, `call_finalized`, `webhook_error`, `test_call` (POC call outside a
  campaign), `manual_status_change` and `message_marked_sent` (admin actions, with the admin's
  username).
- **Transcripts** (`turns`). Every bot and distributor line, with `flagged` set where the
  compliance screen replaced the bot's text.
- **Calls**: who was called, when, by which provider, status, duration, outcome, interest level,
  RM summary, recording URL and errors.
- **Outbound messages** (outbox): every link or follow-up, to whom, by which channel and with
  what status, including messages that were only queued.
- **Link clicks** on tracked empanelment links.
- **DNC entries**, with reason and source.

"Append-only" here means the application never edits or deletes these rows. Database
administrators still could. Protect the database with access control and regular backups, and
agree with InfoSec how long audit data is kept.

---

## Pre-go-live sign-off

Tick each item, with the name of the person approving it and the date, before the first live
call to a real distributor.

**Regulatory and telecom (Compliance)**
- [ ] AMC registered as Principal Entity on DLT; telemarketer/CPaaS agreed and registered.
- [ ] Number series confirmed (promotional 140-series vs service 160-series) and caller ID configured.
- [ ] Distributor list scrubbed against NCPR/DLT preferences before import; process for every re-import agreed.
- [ ] Calling window (`config/campaign.yaml`), holiday list, maximum attempts and retry waits approved.
- [ ] Opt-out handling tested end-to-end (spoken opt-out leads to the number never being redialled).

**Content (Compliance + Product + Distribution)**
- [ ] `config/nfo.yaml` matches the final SID/KIM; every `TODO` removed.
- [ ] `config/amc.yaml` approved: AMC name, SEBI registration, helpline, empanelment link, steps, documents, value propositions.
- [ ] Greetings approved in every enabled language (virtual-assistant and recording disclosures present).
- [ ] SEBI mandatory warning and its translations approved, including placement in the call.
- [ ] `config/faq.yaml` approved; commission wording approved (non-numeric unless Compliance approves figures).
- [ ] Compliance accepts the "approved knowledge + live AI wording + screen + QA sampling" model, and has listed any topics that must use fixed text only.
- [ ] Sample of at least 30 simulator conversations (English and Hindi), including adversarial ones (returns, commission, comparisons, abuse), reviewed and signed off.

**Data protection (Legal)**
- [ ] Lawful basis and notice wording decided; "How did you get my number?" answer updated.
- [ ] Retention periods set for transcripts, recordings, messages and audit data.
- [ ] Erasure and grievance process documented; grievance contact published.
- [ ] Contracts and data processing terms in place with the LLM, telephony and messaging providers.
- [ ] Call recording decision (on or off, storage location, retention).

**Security (InfoSec)**
- [ ] Hosting region and cross-border position for the LLM API approved.
- [ ] HTTPS only; admin access restricted (VPN or IP allow-list); default `ADMIN_PASSWORD` and `SECRET_KEY` changed.
- [ ] VAPT completed and findings closed; system onboarded into the AMC's security monitoring under the applicable SEBI framework.
- [ ] Backups and restore tested.

**Operations (Distribution)**
- [ ] RM mapping, transfer number (`RM_TRANSFER_NUMBER`) and callback SLA agreed; RMs trained on the dashboard.
- [ ] SMS DLT templates or WhatsApp templates approved before switching messaging off `outbox`.
- [ ] Pilot list agreed (friendly distributors who know about the pilot); daily transcript QA owner named.

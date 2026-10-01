# Scope questionnaire

**For:** the AMC's Head of Distribution / Sales, with input from Compliance, Legal, InfoSec,
Distribution Ops and IT.

The MVP already works end-to-end, but with **placeholder content and default assumptions**. This
questionnaire collects what we need to replace those placeholders with your real, approved
details and to plan the pilot. For each question you will see **why it matters** and **what the
MVP assumes today**. If the default is fine, just say "default OK".

Answers go in this document (or a copy of it). Content answers (AMC, NFO, FAQ, calling policy)
end up in `config/*.yaml`; deployment answers end up in `.env`.

---

## Checklist

| Section | Suggested owner | Needed for | Status |
|---|---|---|---|
| [A. AMC profile](#a-amc-profile) | Distribution + Compliance | Pilot | ☐ |
| [B. NFO details (SID/KIM)](#b-nfo-details-sid--kim) | Product + Compliance | Pilot | ☐ |
| [C. Target distributor universe](#c-target-distributor-universe) | Distribution | Pilot | ☐ |
| [D. Objectives and KPIs](#d-objectives-and-kpis) | Head of Distribution | Phase 0 | ☐ |
| [E. Languages](#e-languages) | Distribution + Compliance | Pilot | ☐ |
| [F. Script and tone approvals](#f-script-and-tone-approvals) | Compliance + Distribution | Pilot | ☐ |
| [G. Empanelment process](#g-empanelment-process) | Distribution Ops | Pilot | ☐ |
| [H. RM handoff](#h-rm-handoff) | Distribution (regional heads) | Pilot | ☐ |
| [I. Telephony](#i-telephony) | IT + Compliance | Pilot (start in Phase 0, long lead time) | ☐ |
| [J. Messaging (SMS / WhatsApp / e-mail)](#j-messaging-sms--whatsapp--e-mail) | Marketing + Compliance + IT | Pilot | ☐ |
| [K. Compliance sign-offs](#k-compliance-sign-offs) | Compliance + Legal | Before any live call | ☐ |
| [L. Data hosting and InfoSec](#l-data-hosting-and-infosec) | InfoSec + IT | Pilot | ☐ |
| [M. Integrations](#m-integrations) | IT + Distribution Ops | Phase 4 | ☐ |
| [N. Volumes and timeline](#n-volumes-and-timeline) | Head of Distribution | Phase 0 | ☐ |
| [O. LLM / model preference](#o-llm--model-preference) | IT + InfoSec | Pilot | ☐ |

---

## A. AMC profile

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| A1 | Legal name and brand name of the mutual fund / AMC as they should be spoken. | The greeting must identify the caller correctly. | "Sample Mutual Fund" / "Sample MF" (placeholder). |
| A2 | SEBI registration number of the mutual fund. | May be quoted if a distributor asks whether the caller is genuine. | `MF/000/00/0` (placeholder). |
| A3 | Name of the voice assistant, and a female or male voice. | The bot introduces itself by name; consistency builds trust. | "Asha", female Indian-English and Hindi voice. |
| A4 | Website, distributor helpline and partner e-mail. | Given out when a distributor wants to verify or contact the AMC. | Placeholders in `config/amc.yaml`. |
| A5 | Approved, factual reasons to partner with you (value propositions). | The bot can only say what you approve. | Four generic points: regional RM, partner portal, NFO marketing kit, training webinars. |
| A6 | Anything the bot must **never** say about the AMC (sensitive topics, pending matters). | Added to the instructions and the screen. | Standard rules only (no returns promises, advice, commission figures, inducements, competitor comparisons). |

## B. NFO details (SID / KIM)

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| B1 | Final SID and KIM (or drafts, with the expected approval date). | `config/nfo.yaml` must match them exactly; the bot states only these facts. | Placeholder "Sample Flexi Cap Fund". |
| B2 | Scheme name, SEBI category, scheme type, investment objective, benchmark, fund managers. | Core facts the bot introduces. | Placeholders. |
| B3 | NFO open and close dates; allotment / re-open note. | The bot tailors its message before, during and after the NFO. | 20 Oct to 3 Nov 2026 (placeholder). |
| B4 | Minimum investment, SIP details, plans and options, exit load, riskometer level. | Common distributor questions. | Placeholders; riskometer "Very High". |
| B5 | Approved key highlights (talking points), taken from the SID/KIM or approved marketing material. | The only "selling points" the bot may use. | Two generic highlights. |
| B6 | NFO support for distributors (marketing kit, webinars, investor meets). | A strong reason for distributors to empanel before the NFO. | Kit, fund manager webinar, regional RM support. |
| B7 | Public URLs of the SID and KIM. | The bot refers distributors to them. | Placeholder URLs. |
| B8 | Who notifies us of addenda or date changes, and how quickly? | Outdated facts are a compliance risk. | Manual update by the developer on request. |

## C. Target distributor universe

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| C1 | Source of the list: AMFI export, CRM, RTA data, purchased or other? | Drives the import format and the legal basis for calling (see K and L). | AMFI-export-style CSV (see README for accepted columns). |
| C2 | Approximate size of the list, and of the first campaign. | Sizing of telephony, LLM usage and RM capacity. | Pilot of 50-100; MVP tested on small lists. |
| C3 | Segments to prioritise: individual vs corporate distributors, by AUM band, active vs dormant, new ARNs. | Ordering and messaging. | No segmentation; all eligible distributors in import order. |
| C4 | Geography: states and cities in scope; regional languages. | Language choice and RM mapping. | All India; English and Hindi. |
| C5 | Which distributors are **already empanelled**? Can you share that list (ARN only)? | We must not pitch empanelment to existing partners; they get the NFO message instead. | Detected on the call ("already empanelled"); no upfront exclusion. |
| C6 | Has the list been scrubbed against NCPR/DND preferences, and by whom? | TRAI requirement; the MVP does not scrub (see [COMPLIANCE.md](COMPLIANCE.md)). | Assumed scrubbed **before** import. |
| C7 | Should distributors with an **expired ARN** be excluded? | They cannot receive commission until renewed. | Imported (for visibility); Compliance to decide whether to dial. |
| C8 | Does the list include an internal do-not-call or complaint list we must load first? | Opt-outs from other channels must be honoured. | Internal DNC list starts empty. |

## D. Objectives and KPIs

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| D1 | Primary goal: empanelments, NFO awareness, or both? How are they weighted? | Shapes the script and the call-to-action order. | Both; CTA order: link, then callback, then transfer. |
| D2 | Targets: connect rate, completed conversations, link-sent rate, link clicks, empanelments completed, NFO mobilisation via new partners. | Defines success for the pilot go / no-go. | Tracked: connects, outcomes, link sent, link clicks, callbacks. No targets set. |
| D3 | Acceptable ceilings: opt-out rate, complaint rate, compliance flags. | Guardrails for scaling up. | None set; all events are logged. |
| D4 | How will "empanelled" be confirmed: portal, ops team, CRM? | True conversion measurement. | Manual status update (Phase 4: portal sync). |
| D5 | Reporting: who needs which report, and how often? | Dashboard and export design. | Admin dashboard, `callingbot stats`, leads CSV export. |

## E. Languages

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| E1 | Which languages, in which regions? | Each language needs approved greetings, a disclaimer translation and voice testing. | English (en-IN) and Hindi (hi-IN). |
| E2 | Default language per region, or always start in English? | First impression and connect quality. | The distributor's preferred language from the CSV, otherwise English. |
| E3 | Is Hinglish (mixed Hindi-English) acceptable in the bot's replies? | Natural for many distributors; affects tone. | Not specified; the bot replies in the call language. |
| E4 | Approved translations of the SEBI warning and greeting for each language. | They are spoken verbatim. | Hindi translation provided (needs Compliance approval). |

## F. Script and tone approvals

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| F1 | Who approves the scripts (name and role), and what is the turnaround time? | No live call without approval. | Not assigned. |
| F2 | Tone: formal, warm-professional or conversational? Use "Sir/Madam" or names? | Brand fit. | Warm-professional; addresses the distributor by name. |
| F3 | Approve the greeting, closing and commission wording, and the FAQ answers. | These are spoken word-for-word. | Placeholders in `config/*.yaml`. |
| F4 | Does Compliance accept AI-composed sentences built from approved facts, with an automated screen and QA sampling? If not, which topics must use fixed text only? | Determines how flexible the conversation can be. See [COMPLIANCE.md](COMPLIANCE.md#3-sebi-advertising-code-for-mutual-funds). | AI-composed within guardrails; fixed text for greeting, disclaimer, commission and FAQs. |
| F5 | Additional FAQs distributors ask your RMs today, with approved answers. | Fewer "I'll get an RM to call you" moments. | Ten placeholder FAQs. |
| F6 | Should the bot leave voicemails? If yes, the approved message. | Voicemail content is a broadcast message; it needs approval. | No voicemail; hang up and retry. |

## G. Empanelment process

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| G1 | Empanelment portal or form URL. Can it accept the ARN and a reference in the URL? | The bot sends a tracked link that pre-fills the ARN. | `https://partners.sample-mf.example/empanel?arn={arn}&ref={ref}` (placeholder). |
| G2 | Steps, in order, as the bot should describe them. | Accurate expectations. | Four generic steps in `amc.yaml`. |
| G3 | Documents required, for individual and non-individual distributors. | Commonly asked; the bot lists them (it never collects them). | Generic list (ARN card, PAN copy, cancelled cheque, KYD acknowledgement, GST certificate if applicable, board resolution for non-individuals). |
| G4 | Is there any fee? (We assume none.) | Common question. | "No fee" (placeholder; confirm). |
| G5 | Verification SLA and who verifies (ARN validity, KYD, EUIN). | The bot quotes the SLA; ops owns verification. | "A few working days" (placeholder). |
| G6 | Is a distributor agreement or code-of-conduct acknowledgement part of empanelment? | Mentioned in the steps if so. | Not mentioned. |

## H. RM handoff

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| H1 | Regional RM mapping (by state, city or PIN), with names and contact numbers. | Callbacks and transfers go to the right RM. | A single RM desk; no regional routing (Phase 4). |
| H2 | Live transfer number (desk or hunt group) and its hours. | Warm transfers when a distributor wants a person now. | No transfer number; callbacks instead. |
| H3 | Callback SLA (for example, within one working day) and how RMs get notified. | Interested distributors must not go cold. | Callbacks listed in the dashboard; no notifications (Phase 4). |
| H4 | Who reviews hot leads and flagged calls daily? | Operational ownership. | Not assigned. |

## I. Telephony

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| I1 | Preferred provider: Exotel, Ozonetel, Knowlarity, Tata Tele Business, Airtel IQ, Plivo, Twilio, or an existing contract? | Determines the integration work (Exotel and Twilio adapters exist). | Simulator; Twilio for internal tests. |
| I2 | Is the AMC registered as a **Principal Entity on DLT**? Through which telemarketer / CPaaS? | Mandatory for commercial calls; long lead time. | Assumed not yet. |
| I3 | Number series: 140-series (promotional) or 160-series (service/transactional), as confirmed by Compliance, and the caller ID to display. | Using the wrong series or unregistered numbers risks disconnection and blacklisting. | Assumed promotional (140-series); to be confirmed. |
| I4 | Maximum concurrent calls and calls per minute allowed by the account. | Dialer pacing. | 3 concurrent, 6 per minute (`campaign.yaml`). |
| I5 | Recording policy: record or not, where recordings are stored, retention period, who can listen. | Privacy and compliance. | Not recorded at the provider (transcripts are stored); the greeting says "may be recorded". |
| I6 | Calling days, hours and holiday list. | Enforced by the dialer. | Mon to Sat, 10:00 to 19:00 IST; 2 Oct and 25 Dec 2026 listed. |
| I7 | Maximum attempts per distributor and time between attempts. | Avoids harassment while still reaching people. | 3 attempts; wait 3 hours, then 1 day. |

## J. Messaging (SMS / WhatsApp / e-mail)

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| J1 | SMS: DLT-registered sender header and an approved content template for the empanelment link. | Non-matching SMS are dropped by Indian operators. | Outbox only (stored, not sent). |
| J2 | WhatsApp: Business Solution Provider (BSP) or Meta Cloud API account, approved template(s), display name. | Business-initiated WhatsApp messages need approved templates. | Outbox only; Meta Cloud API adapter ready. |
| J3 | E-mail: sending domain, from address, SMTP or e-mail service, SPF/DKIM/DMARC set up. | Deliverability and authenticity. | Outbox only; SMTP adapter ready. |
| J4 | Which channel should be the default for sending the link? | Distributor preference vs approvals. | The bot asks the distributor which channel they prefer. |
| J5 | Follow-up reminders (for example, before the NFO closes)? | Conversion; needs consent and approved templates. | None in the MVP (Phase 4). |

## K. Compliance sign-offs

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| K1 | Named approvers for: scripts and content, calling policy, data protection, security. | Accountability before go-live. | Not assigned. |
| K2 | Confirmation of the open points in [COMPLIANCE.md](COMPLIANCE.md): number series, disclaimer language and placement, AI-composed wording, recording consent. | These determine the bot's behaviour. | Defaults described in COMPLIANCE.md. |
| K3 | Lawful basis and notice under the DPDP Act; the answer to "How did you get my number?". | Required by law; asked on calls. | Placeholder FAQ answer. |
| K4 | Retention periods for transcripts, recordings, messages and audit logs. | DPDP storage limitation. | Kept until deleted manually. |
| K5 | Grievance officer or contact to give to distributors. | DPDP requirement. | The partner helpline and e-mail from `amc.yaml`. |
| K6 | Internal approval process for changes during a live campaign. | Fast, safe updates. | Change request with approval attached; developer applies it. |

## L. Data hosting and InfoSec

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| L1 | Hosting: AMC data centre or cloud? Which provider and **region** (India)? | Data residency and SEBI expectations for regulated entities. | Any Docker host; Indian region recommended. |
| L2 | Is sending conversation text (no phone numbers or e-mails) to the Claude API acceptable, given SEBI's cybersecurity and cloud frameworks and the DPDP Act? Any conditions? | The LLM is a third-party processor that may be outside India. | Anthropic API with PII minimisation; awaiting InfoSec and Legal decision. |
| L3 | Obligations under SEBI's cybersecurity and cyber-resilience framework that apply to this system (classification, logging, VAPT, incident reporting). | The AMC is a regulated entity. | Not assessed. |
| L4 | VAPT: who performs it, and when? | Required before go-live. | Not done. |
| L5 | Admin access: VPN, IP allow-list, SSO? Who gets access? | Protects transcripts and personal data. | HTTP Basic auth, single admin user; IP restriction recommended. |
| L6 | Secrets management (vault, cloud secrets manager), backup policy, log retention. | Operational security. | Environment variables; manual backups. |

## M. Integrations

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| M1 | CRM in use (Salesforce, LeadSquared, Zoho, in-house...). API available? | Import lists; push outcomes and callbacks back. | CSV import and leads CSV export. |
| M2 | Empanelment system or portal: can it report status by ARN (API or daily file)? | Stop calling empanelled distributors; measure conversion. | Manual status updates. |
| M3 | RTA or partner-portal data that could enrich targeting (for example, existing AUM with you)? | Segmentation and prioritisation. | None. |
| M4 | Single sign-on for the dashboard? | Enterprise access control. | Not supported in the MVP. |

## N. Volumes and timeline

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| N1 | NFO dates and the date by which distributors should be empanelled. | Works backwards to the pilot and go-live dates. | Placeholder NFO 20 Oct to 3 Nov 2026. |
| N2 | Target calls per day and the total campaign size. | Telephony concurrency, LLM rate limits, RM staffing. | 6 calls per minute at most, 3 concurrent. |
| N3 | Pilot start date and duration; go-live target. | Planning DLT and approval lead times. | Pilot after approvals (see [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)). |
| N4 | Budget guidance for telephony, messaging and LLM usage. | Choice of model, effort and provider. | Not set. |

## O. LLM / model preference

| # | Question | Why it matters | MVP default |
|---|---|---|---|
| O1 | Model choice: `claude-opus-5-5` (default) or evaluate `claude-sonnet-5-5` for lower latency and cost? | Trade-off between quality, latency and cost; best decided on pilot transcripts. | `claude-opus-5-5`, effort `low`. |
| O2 | Access route: Anthropic API directly, or Claude through your cloud provider (residency, billing)? | Affects configuration and data residency; refusal fallback is available only on the Claude API. | Anthropic API, refusal fallback on. |
| O3 | Is an offline demo mode (no AI) useful for internal training? | Demos without an API key or cost. | Available (`LLM_PROVIDER=fake`). |
| O4 | Any enterprise data-handling terms required from the LLM vendor (retention, training use, location)? | InfoSec and Legal requirements. | Standard API terms; to be reviewed. |

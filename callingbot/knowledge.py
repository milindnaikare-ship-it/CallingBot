"""Approved business knowledge: AMC profile, NFO facts, FAQs and campaign policy.

Everything the bot is allowed to *say* about the AMC or the scheme comes from these YAML
files under ``config/``. Compliance should review and sign off on them before go-live; the
bot is instructed never to state scheme facts that are not present here.
"""

from __future__ import annotations

from datetime import date, time
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_DISCLAIMER = (
    "Mutual Fund investments are subject to market risks, read all scheme related documents carefully."
)


class LanguageOption(BaseModel):
    code: str  # BCP-47, e.g. "en-IN", "hi-IN"
    name: str  # "English", "Hindi"
    twilio_voice: str = "Polly.Aditi"  # TTS voice name for <Say voice="...">
    twilio_speech_language: str | None = None  # STT language for <Gather language="...">; defaults to code
    greeting: str | None = None  # Pre-approved opening line in this language ({bot_name}, {amc_name}, {name})

    @property
    def stt_language(self) -> str:
        return self.twilio_speech_language or self.code


class AMCProfile(BaseModel):
    name: str  # "Sample Mutual Fund"
    short_name: str  # "Sample MF"
    sebi_registration: str | None = None
    bot_name: str = "Asha"
    website: str | None = None
    distributor_helpline: str | None = None
    distributor_email: str | None = None
    # How the bot says the partner email aloud, e.g. "partners at sample M F dot com". When set, the bot
    # may speak it (the approved script asks it to); otherwise it offers to send the address instead.
    distributor_email_spoken: str | None = None
    # Target of the tracked empanelment link. May contain {arn} (pre-filled when known) and {ref}
    # (our tracking reference). Example: "https://partners.sample-mf.example/empanel?arn={arn}&ref={ref}"
    empanelment_url_template: str
    empanelment_steps: list[str] = Field(default_factory=list)
    empanelment_documents: list[str] = Field(default_factory=list)
    rm_team_description: str = "Our distributor relationship team"
    distributor_value_props: list[str] = Field(default_factory=list)  # approved "why partner with us"
    languages: list[LanguageOption]
    default_language: str = "en-IN"

    @field_validator("empanelment_url_template")
    @classmethod
    def _is_url(cls, v: str) -> str:
        if not v.startswith(("https://", "http://")):
            raise ValueError("empanelment_url_template must be an http(s) URL")
        return v

    @model_validator(mode="after")
    def _default_language_listed(self) -> AMCProfile:
        if self.default_language not in {lang.code for lang in self.languages}:
            raise ValueError(f"default_language {self.default_language!r} is not in languages")
        return self

    def language(self, code: str | None) -> LanguageOption:
        for lang in self.languages:
            if lang.code == code:
                return lang
        return next(lang for lang in self.languages if lang.code == self.default_language)


class NFOInfo(BaseModel):
    """Scheme facts. Optional fields left empty are "to be shared by the team": the bot says so
    instead of guessing (see :meth:`pending_fields`)."""

    scheme_name: str
    category: str  # SEBI category, e.g. "Flexi Cap Fund"
    scheme_type: str | None = None  # "An open-ended dynamic equity scheme investing across large cap, ..."
    investment_objective: str | None = None
    benchmark: str | None = None
    fund_managers: list[str] = Field(default_factory=list)
    nfo_open_date: date
    nfo_close_date: date | None = None
    allotment_or_reopen_note: str | None = None
    min_investment: str | None = None
    sip_details: str | None = None
    plans_and_options: list[str] = Field(default_factory=list)
    exit_load: str | None = None
    riskometer: str | None = None  # "Very High"
    key_highlights: list[str] = Field(default_factory=list)  # approved talking points (no return claims)
    distributor_support: list[str] = Field(default_factory=list)  # marketing kits, webinars, etc.
    # What the bot may say if asked about commission/brokerage. Keep it non-numeric unless Compliance approves.
    commission_response: str = (
        "The brokerage structure for this scheme will be shared with you in writing by our "
        "relationship manager after empanelment."
    )
    sid_url: str | None = None
    kim_url: str | None = None
    mandatory_disclaimer: str = DEFAULT_DISCLAIMER
    # Spoken disclaimer per language code (e.g. "hi-IN"); falls back to mandatory_disclaimer.
    mandatory_disclaimer_translations: dict[str, str] = Field(default_factory=dict)

    def disclaimer(self, language: str | None) -> str:
        return self.mandatory_disclaimer_translations.get(language or "", self.mandatory_disclaimer)

    _PENDING_LABELS: ClassVar[dict[str, str]] = {
        "scheme_type": "scheme type",
        "investment_objective": "investment objective",
        "benchmark": "benchmark",
        "fund_managers": "fund managers",
        "nfo_close_date": "NFO closing date",
        "min_investment": "minimum investment",
        "sip_details": "SIP details",
        "plans_and_options": "plans and options",
        "exit_load": "exit load",
        "riskometer": "riskometer level",
    }

    def pending_fields(self) -> list[str]:
        """Human labels of the scheme facts not yet available (empty in nfo.yaml)."""
        return [label for field, label in self._PENDING_LABELS.items() if not getattr(self, field)]

    @model_validator(mode="after")
    def _dates_ordered(self) -> NFOInfo:
        if self.nfo_close_date is not None and self.nfo_close_date < self.nfo_open_date:
            raise ValueError("nfo_close_date is before nfo_open_date")
        return self


class FAQ(BaseModel):
    question: str
    answer: str
    tags: list[str] = Field(default_factory=list)


class ScriptStep(BaseModel):
    """One step of the AMC's approved call script."""

    id: str
    when: str  # the situation in which this step applies
    say: str  # approved wording
    next: str | None = None  # what to do after it


class CallScript(BaseModel):
    """Approved call script (config/script.yaml). When present, the bot follows its flow and wording;
    without it the bot uses a generic NFO-awareness and empanelment flow."""

    steps: list[ScriptStep] = Field(default_factory=list)
    standard_responses: list[ScriptStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> CallScript:
        ids = [s.id for s in [*self.steps, *self.standard_responses]]
        if len(ids) != len(set(ids)):
            raise ValueError("script step ids must be unique")
        return self

    def approved_texts(self) -> list[str]:
        return [s.say for s in [*self.steps, *self.standard_responses]]


class CampaignPolicy(BaseModel):
    """When and how often the dialer may call. Times are in the business timezone (IST)."""

    calling_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5])  # Mon=0 .. Sun=6
    window_start: time = time(10, 0)
    window_end: time = time(19, 0)
    holidays: list[date] = Field(default_factory=list)
    max_attempts: int = 3
    # Minutes to wait before attempt N+1 after an unanswered attempt N; last value repeats.
    retry_backoff_minutes: list[int] = Field(default_factory=lambda: [180, 1440])
    max_concurrent_calls: int = 3
    calls_per_minute: int = 6
    leave_voicemail: bool = False
    voicemail_message: str | None = None

    @model_validator(mode="after")
    def _window_ordered(self) -> CampaignPolicy:
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        if not self.calling_days:
            raise ValueError("calling_days must not be empty")
        if any(d < 0 or d > 6 for d in self.calling_days):
            raise ValueError("calling_days must be weekday numbers 0 (Mon) .. 6 (Sun)")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        return self


class KnowledgeBase(BaseModel):
    amc: AMCProfile
    nfo: NFOInfo
    faqs: list[FAQ] = Field(default_factory=list)
    campaign: CampaignPolicy = Field(default_factory=CampaignPolicy)
    script: CallScript = Field(default_factory=CallScript)

    def approved_texts(self) -> list[str]:
        """Compliance-approved wording the bot may speak verbatim (script, FAQs, commission response,
        disclaimers). The utterance screen does not flag sentences taken from these."""
        texts = [*self.script.approved_texts(), *(f.answer for f in self.faqs), self.nfo.commission_response]
        texts += [self.nfo.disclaimer(lang.code) for lang in self.amc.languages]
        return texts


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_knowledge(config_dir: Path | str) -> KnowledgeBase:
    """Load and validate ``amc.yaml``, ``nfo.yaml`` and the optional ``faq.yaml``, ``campaign.yaml``
    and ``script.yaml``."""
    config_dir = Path(config_dir)
    faq_path = config_dir / "faq.yaml"
    campaign_path = config_dir / "campaign.yaml"
    script_path = config_dir / "script.yaml"
    return KnowledgeBase(
        amc=AMCProfile(**_read_yaml(config_dir / "amc.yaml")),
        nfo=NFOInfo(**_read_yaml(config_dir / "nfo.yaml")),
        faqs=[FAQ(**f) for f in _read_yaml(faq_path).get("faqs", [])] if faq_path.exists() else [],
        campaign=CampaignPolicy(**_read_yaml(campaign_path)) if campaign_path.exists() else CampaignPolicy(),
        script=CallScript(**_read_yaml(script_path)) if script_path.exists() else CallScript(),
    )


@lru_cache
def get_knowledge() -> KnowledgeBase:
    from callingbot.settings import get_settings

    return load_knowledge(get_settings().config_dir)

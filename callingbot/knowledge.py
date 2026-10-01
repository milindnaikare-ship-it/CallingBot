"""Approved business knowledge: AMC profile, NFO facts, FAQs and campaign policy.

Everything the bot is allowed to *say* about the AMC or the scheme comes from these YAML
files under ``config/``. Compliance should review and sign off on them before go-live; the
bot is instructed never to state scheme facts that are not present here.
"""

from __future__ import annotations

from datetime import date, time
from functools import lru_cache
from pathlib import Path

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
    website: str
    distributor_helpline: str | None = None
    distributor_email: str | None = None
    # Must contain {arn}; may contain {ref} (tracking reference). Example:
    # "https://partners.sample-mf.example/empanel?arn={arn}&ref={ref}"
    empanelment_url_template: str
    empanelment_steps: list[str] = Field(default_factory=list)
    empanelment_documents: list[str] = Field(default_factory=list)
    rm_team_description: str = "Our distributor relationship team"
    distributor_value_props: list[str] = Field(default_factory=list)  # approved "why partner with us"
    languages: list[LanguageOption]
    default_language: str = "en-IN"

    @field_validator("empanelment_url_template")
    @classmethod
    def _needs_arn(cls, v: str) -> str:
        if "{arn}" not in v:
            raise ValueError("empanelment_url_template must contain the {arn} placeholder")
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
    scheme_name: str
    category: str  # SEBI category, e.g. "Flexi Cap Fund"
    scheme_type: str  # "An open-ended dynamic equity scheme investing across large cap, ..."
    investment_objective: str
    benchmark: str
    fund_managers: list[str]
    nfo_open_date: date
    nfo_close_date: date
    allotment_or_reopen_note: str | None = None
    min_investment: str
    sip_details: str | None = None
    plans_and_options: list[str] = Field(default_factory=list)
    exit_load: str
    riskometer: str  # "Very High"
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

    @model_validator(mode="after")
    def _dates_ordered(self) -> NFOInfo:
        if self.nfo_close_date < self.nfo_open_date:
            raise ValueError("nfo_close_date is before nfo_open_date")
        return self


class FAQ(BaseModel):
    question: str
    answer: str
    tags: list[str] = Field(default_factory=list)


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


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_knowledge(config_dir: Path | str) -> KnowledgeBase:
    """Load and validate ``amc.yaml``, ``nfo.yaml``, ``faq.yaml`` and ``campaign.yaml``."""
    config_dir = Path(config_dir)
    faq_path = config_dir / "faq.yaml"
    campaign_path = config_dir / "campaign.yaml"
    return KnowledgeBase(
        amc=AMCProfile(**_read_yaml(config_dir / "amc.yaml")),
        nfo=NFOInfo(**_read_yaml(config_dir / "nfo.yaml")),
        faqs=[FAQ(**f) for f in _read_yaml(faq_path).get("faqs", [])] if faq_path.exists() else [],
        campaign=CampaignPolicy(**_read_yaml(campaign_path)) if campaign_path.exists() else CampaignPolicy(),
    )


@lru_cache
def get_knowledge() -> KnowledgeBase:
    from callingbot.settings import get_settings

    return load_knowledge(get_settings().config_dir)

"""Runtime configuration, read from environment variables (and an optional .env file).

Business content (AMC profile, NFO facts, FAQs, campaign policy) lives in YAML under
``config/`` and is loaded by :mod:`callingbot.knowledge`. This module only holds
deployment settings and secrets.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application -------------------------------------------------------
    app_env: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "sqlite:///./callingbot.db"
    config_dir: Path = Path("config")
    # Public HTTPS base URL that the telephony provider can reach (e.g. an ngrok URL in dev).
    public_base_url: str = "http://localhost:8000"
    # Used to sign empanelment tracking links. MUST be changed in production.
    secret_key: str = "dev-secret-change-me"
    admin_username: str = "admin"
    admin_password: str = "change-me"
    timezone: str = "Asia/Kolkata"
    log_level: str = "INFO"

    # --- LLM (Claude) ------------------------------------------------------
    # "fake" runs a scripted offline bot (no API key needed) - useful for demos and tests.
    llm_provider: Literal["anthropic", "fake"] = "anthropic"
    llm_model: str = "claude-opus-5-5"
    # Effort controls thinking depth and therefore latency. "low" suits live voice turns.
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    # Thinking counts toward max_tokens, so leave headroom above the short spoken reply.
    llm_max_tokens: int = 4096
    llm_timeout_seconds: float = 20.0
    llm_max_retries: int = 2
    # Server-side refusal fallback (Claude API only). Disable when routing via Bedrock/Vertex/Foundry.
    llm_enable_fallbacks: bool = True

    # --- Telephony -----------------------------------------------------------
    telephony_provider: Literal["simulator", "twilio", "exotel"] = "simulator"

    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    twilio_validate_signature: bool = True
    twilio_machine_detection: bool = True
    twilio_record_calls: bool = False

    exotel_account_sid: str | None = None
    exotel_api_key: str | None = None
    exotel_api_token: str | None = None
    exotel_subdomain: str = "api.exotel.com"
    exotel_caller_id: str | None = None  # Your ExoPhone / 140-series virtual number
    exotel_app_id: str | None = None  # Exotel flow (applet) id that hits our webhooks

    # --- Call behaviour --------------------------------------------------------
    max_call_turns: int = 24
    max_call_seconds: int = 420
    no_input_reprompts: int = 1
    gather_timeout_seconds: int = 6
    # Number of a human relationship manager / desk for warm transfers (E.164). Optional.
    rm_transfer_number: str | None = None

    # --- Messaging (empanelment links, follow-ups) -----------------------------
    sms_provider: Literal["outbox", "twilio"] = "outbox"
    whatsapp_provider: Literal["outbox", "meta"] = "outbox"
    email_provider: Literal["outbox", "smtp"] = "outbox"

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True

    twilio_sms_from: str | None = None

    meta_whatsapp_token: str | None = None
    meta_whatsapp_phone_number_id: str | None = None
    meta_whatsapp_template_name: str | None = None
    meta_whatsapp_template_language: str = "en"

    @property
    def base_url(self) -> str:
        return self.public_base_url.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()

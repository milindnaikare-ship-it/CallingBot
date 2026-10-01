"""Telephony provider adapters and the factory that picks one from settings.

``get_provider(settings.telephony_provider, settings)`` is what the app and the dialer use; the
neutral types from :mod:`callingbot.telephony.base` are re-exported so callers import from here.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from callingbot.settings import Settings
from callingbot.telephony.base import (
    CallStatusUpdate,
    PlaceCallResult,
    TelephonyError,
    TelephonyProvider,
    VoiceAction,
    VoiceInput,
    VoiceResponse,
)
from callingbot.telephony.exotel import ExotelProvider
from callingbot.telephony.simulator import SimulatorProvider
from callingbot.telephony.twilio import TwilioProvider, describe_settings, missing_settings

__all__ = [
    "PROVIDER_NAMES",
    "CallStatusUpdate",
    "ExotelProvider",
    "PlaceCallResult",
    "SimulatorProvider",
    "TelephonyError",
    "TelephonyProvider",
    "TwilioProvider",
    "VoiceAction",
    "VoiceInput",
    "VoiceResponse",
    "get_provider",
]

log = logging.getLogger(__name__)

PROVIDER_NAMES: tuple[str, ...] = ("twilio", "exotel", "simulator")

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


def get_provider(name: str, settings: Settings) -> TelephonyProvider:
    """Build the provider called ``name`` ("twilio" | "exotel" | "simulator", case-insensitive).

    Raises ``ValueError`` for an unknown name, or when a real provider is missing credentials (the
    message names every missing setting and its environment variable).
    """
    key = (name or "").strip().lower()
    if key == "simulator":
        return SimulatorProvider()

    provider_cls: type[TwilioProvider] | type[ExotelProvider]
    if key == "twilio":
        provider_cls = TwilioProvider
    elif key == "exotel":
        provider_cls = ExotelProvider
    else:
        raise ValueError(f"Unknown telephony provider {name!r}; expected one of: {', '.join(PROVIDER_NAMES)}")

    missing = missing_settings(settings, provider_cls.required_settings)
    if missing:
        raise ValueError(
            f"Telephony provider {key!r} is missing required settings: {describe_settings(missing)}"
        )
    _warn_if_unreachable(settings, key)
    return provider_cls(settings)


def _warn_if_unreachable(settings: Settings, provider: str) -> None:
    # A real provider must reach our webhooks over the internet; a localhost base URL means every
    # answered call would hit a dead end. Warn rather than fail so local dry runs still start.
    parts = urlsplit(settings.base_url)
    if (parts.hostname or "").lower() in _LOCAL_HOSTS:
        log.warning(
            "PUBLIC_BASE_URL %s is not reachable by %s; set it to a public HTTPS URL (e.g. an ngrok tunnel)",
            settings.base_url,
            provider,
        )
    elif parts.scheme != "https":
        log.warning(
            "PUBLIC_BASE_URL %s is not HTTPS; %s webhooks should use HTTPS", settings.base_url, provider
        )

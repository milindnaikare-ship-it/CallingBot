"""Indian phone number normalisation (E.164, +91)."""

from __future__ import annotations

import re

_NON_DIGITS = re.compile(r"\D+")


def normalize_indian_mobile(raw: str | None) -> str | None:
    """Return ``+91XXXXXXXXXX`` for a valid Indian mobile number, else ``None``.

    Accepts common formats: ``98765 43210``, ``098765-43210``, ``+91 98765 43210``,
    ``919876543210``, ``0091 9876543210``. Indian mobile numbers are 10 digits starting 6-9.
    """
    if not raw:
        return None
    digits = _NON_DIGITS.sub("", str(raw))
    if digits.startswith("0091"):
        digits = digits[4:]
    elif digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789":
        return None
    return f"+91{digits}"


def mask_phone(e164: str | None) -> str:
    """Mask a phone number for logs/UI: ``+91******3210``."""
    if not e164:
        return ""
    return e164[:3] + "*" * max(0, len(e164) - 7) + e164[-4:]

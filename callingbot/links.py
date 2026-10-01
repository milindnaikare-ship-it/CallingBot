"""Signed tracking links for empanelment follow-ups.

The bot never sends the AMC's empanelment URL directly. It sends ``{base_url}/r/{token}``; the
redirect route verifies the token, records a :class:`~callingbot.models.LinkClick` and forwards
to :func:`empanelment_target_url`. That lets us measure clicks per call without third-party
link shorteners.

Token format: ``base64url(payload) + "." + base64url(HMAC-SHA256(secret, payload)[:16])``,
unpadded. The payload carries ids only (``v1:<distributor_id>:<call_id or empty>``) - no
names, phones or ARNs - so a forwarded or logged link leaks no personal data (DPDP data
minimisation). A 128-bit truncated MAC is ample for a link that only grants a redirect.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from urllib.parse import quote

from callingbot.knowledge import KnowledgeBase
from callingbot.models import Distributor
from callingbot.settings import Settings

_VERSION = "1"
_MAC_BYTES = 16
_B64URL = re.compile(r"[A-Za-z0-9_-]+")
_PAYLOAD = re.compile(r"1:([1-9][0-9]{0,17}):([1-9][0-9]{0,17})?")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes | None:
    if not _B64URL.fullmatch(text):
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    # Reject non-canonical encodings (different trailing bits, same bytes) so each id pair has
    # exactly one valid token.
    return raw if _b64encode(raw) == text else None


def _mac(secret: str, payload: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()[:_MAC_BYTES]


def _check_id(name: str, value: object, *, optional: bool) -> None:
    if optional and value is None:
        return
    # bool is an int subclass; True would silently become distributor 1.
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def make_link_token(secret: str, distributor_id: int, call_id: int | None) -> str:
    """Return a URL-safe, HMAC-SHA256 signed token for (distributor_id, call_id)."""
    if not secret:
        raise ValueError("make_link_token: secret must not be empty")
    _check_id("distributor_id", distributor_id, optional=False)
    _check_id("call_id", call_id, optional=True)
    payload = f"{_VERSION}:{distributor_id}:{'' if call_id is None else call_id}".encode("ascii")
    return f"{_b64encode(payload)}.{_b64encode(_mac(secret, payload))}"


def parse_link_token(secret: str, token: str) -> tuple[int, int | None] | None:
    """Verify ``token`` and return ``(distributor_id, call_id)``; ``None`` if invalid. Never raises."""
    if not secret or not isinstance(token, str) or token.count(".") != 1:
        return None
    payload_part, mac_part = token.split(".")
    payload = _b64decode(payload_part)
    mac = _b64decode(mac_part)
    if payload is None or mac is None:
        return None
    if not hmac.compare_digest(mac, _mac(secret, payload)):
        return None
    match = _PAYLOAD.fullmatch(payload.decode("ascii", errors="replace"))
    if match is None:
        return None
    distributor_id, call_id = match.groups()
    return int(distributor_id), (int(call_id) if call_id else None)


def tracked_link(settings: Settings, distributor_id: int, call_id: int | None) -> str:
    """Public short link that redirects (via ``/r/{token}``) to the empanelment form."""
    return f"{settings.base_url}/r/{make_link_token(settings.secret_key, distributor_id, call_id)}"


def empanelment_target_url(kb: KnowledgeBase, distributor: Distributor, call_id: int | None) -> str:
    """Fill ``{arn}`` and ``{ref}`` in ``kb.amc.empanelment_url_template`` (URL-encoded).

    ``ref`` is ``call<id>`` when the link came from a call, else ``dist<id>``, so the AMC's
    empanelment system can attribute completed forms back to the campaign.
    """
    ref = f"call{call_id}" if call_id else f"dist{distributor.id}"
    # str.replace rather than str.format: a template with other literal braces must not break.
    return kb.amc.empanelment_url_template.replace("{arn}", quote(distributor.arn or "", safe="")).replace(
        "{ref}", quote(ref, safe="")
    )

"""Tests for callingbot.links: signed tracking tokens and empanelment target URLs."""

from __future__ import annotations

import base64

import pytest

from callingbot.links import empanelment_target_url, make_link_token, parse_link_token, tracked_link

SECRET = "test-secret"


@pytest.mark.parametrize("distributor_id,call_id", [(1, 1), (42, 9001), (123456789, None), (7, None)])
def test_round_trip(distributor_id, call_id):
    token = make_link_token(SECRET, distributor_id, call_id)
    assert parse_link_token(SECRET, token) == (distributor_id, call_id)


def test_token_is_url_safe_and_unpadded():
    token = make_link_token(SECRET, 98765, 43210)
    assert "=" not in token and "+" not in token and "/" not in token
    payload_part, mac_part = token.split(".")
    assert len(base64.urlsafe_b64decode(mac_part + "==")) == 16  # truncated HMAC-SHA256


def test_token_carries_no_personal_data():
    payload_part = make_link_token(SECRET, 5, 6).split(".")[0]
    assert base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4)) == b"1:5:6"


def test_same_ids_same_token_different_ids_different_token():
    assert make_link_token(SECRET, 1, 2) == make_link_token(SECRET, 1, 2)
    assert make_link_token(SECRET, 1, 2) != make_link_token(SECRET, 1, None)
    assert make_link_token(SECRET, 1, 2) != make_link_token(SECRET, 2, 1)


def test_wrong_secret_rejected():
    token = make_link_token(SECRET, 10, 20)
    assert parse_link_token("another-secret", token) is None
    assert parse_link_token("", token) is None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_tampered_payload_rejected():
    token = make_link_token(SECRET, 10, 20)
    _, mac_part = token.split(".")
    forged = f"{_b64(b'1:11:20')}.{mac_part}"  # someone else's distributor id, old signature
    assert parse_link_token(SECRET, forged) is None


@pytest.mark.parametrize("position", [0, 5, -1])
def test_every_character_flip_rejected(position):
    token = make_link_token(SECRET, 10, 20)
    chars = list(token)
    idx = position if position >= 0 else len(chars) + position
    if chars[idx] == ".":
        idx += 1
    chars[idx] = "A" if chars[idx] != "A" else "B"
    assert parse_link_token(SECRET, "".join(chars)) is None


def test_signature_must_be_canonical():
    # Changing only the padding bits of the last base64 char decodes to the same bytes; such a
    # variant must still be rejected so every id pair has exactly one valid token.
    token = make_link_token(SECRET, 10, 20)
    last = token[-1]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    sibling = alphabet[alphabet.index(last) ^ 1]  # differs in the lowest (padding) bit
    assert parse_link_token(SECRET, token[:-1] + sibling) is None


@pytest.mark.parametrize(
    "garbage",
    ["", ".", "abc", "a.b.c", "!!!.???", "Zm9v", "Zm9v.", ".Zm9v", "Zm9v.Zm9v", "अ.आ", None, 12345],
)
def test_garbage_returns_none_never_raises(garbage):
    assert parse_link_token(SECRET, garbage) is None


def test_validly_signed_but_malformed_payload_rejected():
    import hashlib
    import hmac

    for payload in (b"1:0:5", b"1:-3:", b"2:5:6", b"1:abc:", b"1:5"):
        mac = hmac.new(SECRET.encode(), payload, hashlib.sha256).digest()[:16]
        assert parse_link_token(SECRET, f"{_b64(payload)}.{_b64(mac)}") is None


@pytest.mark.parametrize("bad", [(0, None), (-1, 2), (True, None), (1, 0), (1, "5")])
def test_make_link_token_rejects_invalid_ids(bad):
    with pytest.raises(ValueError):
        make_link_token(SECRET, *bad)


def test_make_link_token_requires_secret():
    with pytest.raises(ValueError):
        make_link_token("", 1, None)


def test_tracked_link(settings):
    link = tracked_link(settings, 15, 99)
    assert link.startswith("https://bot.example.test/r/")
    token = link.rsplit("/", 1)[1]
    assert parse_link_token(settings.secret_key, token) == (15, 99)


def test_tracked_link_strips_trailing_slash(settings):
    settings.public_base_url = "https://bot.example.test/"
    assert tracked_link(settings, 1, None).startswith("https://bot.example.test/r/")


def test_empanelment_target_url_with_call(kb, make_distributor):
    d = make_distributor(arn="ARN-123456")
    assert (
        empanelment_target_url(kb, d, 77)
        == "https://partners.sample-mf.example/empanel?arn=ARN-123456&ref=call77"
    )


def test_empanelment_target_url_without_call_uses_distributor_ref(kb, make_distributor):
    d = make_distributor(arn="ARN-654321")
    assert empanelment_target_url(kb, d, None) == (
        f"https://partners.sample-mf.example/empanel?arn=ARN-654321&ref=dist{d.id}"
    )


def test_empanelment_target_url_encodes_values(kb, make_distributor):
    d = make_distributor(arn="ARN 12/34&x=1")
    url = empanelment_target_url(kb, d, 5)
    assert url == "https://partners.sample-mf.example/empanel?arn=ARN%2012%2F34%26x%3D1&ref=call5"


def test_empanelment_target_url_template_without_ref_and_with_literal_braces(kb, make_distributor):
    kb.amc.empanelment_url_template = "https://example.test/{arn}/form?x={{literal}}"
    d = make_distributor(arn="ARN-1")
    assert empanelment_target_url(kb, d, 3) == "https://example.test/ARN-1/form?x={{literal}}"

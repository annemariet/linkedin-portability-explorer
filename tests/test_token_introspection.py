"""Tests for LinkedIn token introspection and fingerprint rotation."""

from datetime import date
import responses

from linkedin_api.utils.token_fingerprint import (
    detect_token_rotation,
    token_fingerprint,
)
from linkedin_api.utils.token_introspection import introspect_access_token
from linkedin_api.utils.token_lifecycle import TokenExpiryLevel, assess_token_expiry


@responses.activate
def test_introspect_returns_expiry(monkeypatch):
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "cid")
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "csecret")
    responses.add(
        responses.POST,
        "https://www.linkedin.com/oauth/v2/introspectToken",
        json={
            "active": True,
            "status": "active",
            "created_at": 1_700_000_000,
            "expires_at": 1_705_000_000,
            "scope": "r_dma_portability_self_serve",
        },
        status=200,
    )
    intro = introspect_access_token("test-access-token-value-here")
    assert intro is not None
    assert intro.active is True
    assert intro.expires_on == date(2024, 1, 11)


def test_assess_uses_introspection_expires(monkeypatch):
    monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN_ISSUED_AT", raising=False)
    monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN_EXPIRES_AT", raising=False)
    fake = type(
        "I",
        (),
        {
            "active": True,
            "status": "active",
            "expires_on": date(2026, 12, 1),
            "issued_on": date(2026, 10, 1),
            "scope": None,
        },
    )()
    status = assess_token_expiry(
        access_token="x",
        today=date(2026, 10, 9),
        introspection=fake,
    )
    assert status.level == TokenExpiryLevel.OK
    assert status.expires_on == date(2026, 12, 1)


def test_fingerprint_detects_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))
    assert detect_token_rotation("token-a-value-long-enough-here") is False
    assert detect_token_rotation("token-a-value-long-enough-here") is False
    assert detect_token_rotation("token-b-value-long-enough-here") is True
    assert token_fingerprint("token-a-value-long-enough-here") != token_fingerprint(
        "token-b-value-long-enough-here"
    )

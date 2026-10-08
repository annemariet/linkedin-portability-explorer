"""Tests for LinkedIn token expiry estimation."""

from datetime import date

from linkedin_api.utils.token_lifecycle import (
    TokenExpiryLevel,
    assess_token_expiry,
    read_expires_at,
)


class TestTokenLifecycle:
    def test_warn_when_within_window(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN_ISSUED_AT", "2026-09-01")
        status = assess_token_expiry(
            warn_days=14,
            lifetime_days=60,
            today=date(2026, 10, 20),
        )
        assert status.level == TokenExpiryLevel.WARN
        assert status.days_remaining == 11

    def test_expired_when_past_lifetime(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN_ISSUED_AT", "2026-07-01")
        status = assess_token_expiry(
            lifetime_days=60,
            today=date(2026, 10, 8),
        )
        assert status.level == TokenExpiryLevel.EXPIRED

    def test_explicit_expires_at_overrides(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN_EXPIRES_AT", "2026-12-01")
        expires = read_expires_at(date(2026, 1, 1))
        assert expires == date(2026, 12, 1)

    def test_unknown_without_metadata(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN_ISSUED_AT", raising=False)
        monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN_EXPIRES_AT", raising=False)
        status = assess_token_expiry(today=date(2026, 10, 8))
        assert status.level == TokenExpiryLevel.UNKNOWN

"""Tests for LinkedIn token expiry (introspection only)."""

from datetime import date

from linkedin_api.utils.token_lifecycle import (
    TokenExpiryLevel,
    assess_token_expiry,
)


def _intro(**kwargs):
    defaults = {
        "active": True,
        "status": "active",
        "expires_on": None,
        "issued_on": None,
        "scope": None,
    }
    defaults.update(kwargs)
    return type("I", (), defaults)()


class TestTokenLifecycle:
    def test_warn_when_within_window(self):
        status = assess_token_expiry(
            today=date(2026, 10, 20),
            introspection=_intro(expires_on=date(2026, 11, 1)),
            warn_days=14,
        )
        assert status.level == TokenExpiryLevel.WARN

    def test_expired_from_introspection_date(self):
        status = assess_token_expiry(
            today=date(2026, 10, 8),
            introspection=_intro(expires_on=date(2026, 10, 1)),
        )
        assert status.level == TokenExpiryLevel.EXPIRED

    def test_unknown_without_introspection(self):
        status = assess_token_expiry(access_token=None, introspection=None)
        assert status.level == TokenExpiryLevel.UNKNOWN
        assert status.message == ""

    def test_unknown_when_introspection_has_no_expires_at(self):
        status = assess_token_expiry(
            introspection=_intro(expires_on=None),
        )
        assert status.level == TokenExpiryLevel.UNKNOWN

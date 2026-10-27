"""Tests for OAuth callback parsing and state validation."""

import pytest

from linkedin_api.utils.token_oauth import (
    load_oauth_client_config,
    parse_oauth_callback,
)


class TestParseOauthCallback:
    def test_accepts_matching_state(self):
        url = "https://example.test/cb?code=abc123&state=expected-state"
        assert parse_oauth_callback(url, expected_state="expected-state") == "abc123"

    def test_rejects_state_mismatch(self):
        url = "https://example.test/cb?code=abc123&state=wrong"
        with pytest.raises(ValueError, match="state mismatch"):
            parse_oauth_callback(url, expected_state="expected-state")

    def test_rejects_missing_state(self):
        url = "https://example.test/cb?code=abc123"
        with pytest.raises(ValueError, match="missing state"):
            parse_oauth_callback(url, expected_state="expected-state")

    def test_rejects_bare_code(self):
        with pytest.raises(ValueError, match="full redirect URL"):
            parse_oauth_callback("abc123only", expected_state="expected-state")


def test_load_oauth_config_missing_secret_message(monkeypatch):
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "fixture-client-id")
    monkeypatch.delenv("LINKEDIN_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("LINKEDIN_ACCOUNT", "user@example.com")
    with pytest.raises(ValueError) as exc:
        load_oauth_client_config()
    msg = str(exc.value)
    assert "Missing LINKEDIN_CLIENT_SECRET" in msg
    assert "LINKEDIN_CLIENT_ID" not in msg
    assert "security add-generic-password" in msg
    assert '-a "user@example.com"' in msg
    assert "OS keyring" in msg

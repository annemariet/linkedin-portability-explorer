"""Tests for OAuth callback parsing and state validation."""

import pytest

from linkedin_api.utils.token_oauth import parse_oauth_callback


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

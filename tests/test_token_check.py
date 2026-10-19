"""Tests for token secrets, expiry, check CLI, and Gradio startup."""

import logging
from datetime import date
from unittest.mock import patch

import pytest
import responses
from keyring.errors import NoKeyringError

from linkedin_api.utils.token_check import (
    EXIT_ERROR,
    EXIT_EXPIRY_UNKNOWN,
    EXIT_OK,
    EXIT_WARN,
    exit_code_for_report,
    log_startup_token_health,
    main,
)
from linkedin_api.utils.token_expiry import (
    TokenExpiryLevel,
    TokenIntrospection,
    assess_token_expiry,
    introspect_access_token,
)
from linkedin_api.utils.token_secrets import (
    SecretRetrievalIssue,
    detect_token_rotation,
    retrieve_secret,
    token_fingerprint,
)


class TestRetrieveSecret:
    def test_env_wins_over_keyring(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "env-token-value-here")
        with patch("linkedin_api.utils.token_secrets.keyring") as mock_kr:
            mock_kr.get_password.return_value = "keyring-token-value-here"
            result = retrieve_secret(log_failures=False)
        assert result.issue == SecretRetrievalIssue.OK
        assert result.source == "env"

    def test_no_keyring_backend_is_missing_not_keyring_error(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN", raising=False)
        with patch("linkedin_api.utils.token_secrets.keyring") as mock_kr:
            mock_kr.get_password.side_effect = NoKeyringError("no backend")
            result = retrieve_secret(log_failures=False)
        assert result.issue == SecretRetrievalIssue.MISSING

    def test_keyring_runtime_error_is_keyring_error(self, monkeypatch, caplog):
        monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN", raising=False)
        with patch("linkedin_api.utils.token_secrets.keyring") as mock_kr:
            mock_kr.get_password.side_effect = RuntimeError("backend broken")
            with caplog.at_level(logging.ERROR):
                result = retrieve_secret(log_failures=True)
        assert result.issue == SecretRetrievalIssue.KEYRING_ERROR


class TestTokenExpiry:
    def _intro(self, **kwargs):
        defaults = {
            "active": True,
            "status": "active",
            "expires_on": None,
            "issued_on": None,
            "scope": None,
        }
        defaults.update(kwargs)
        return TokenIntrospection(**defaults)

    def test_warn_when_within_window(self):
        status = assess_token_expiry(
            today=date(2026, 10, 20),
            introspection=self._intro(expires_on=date(2026, 11, 1)),
            warn_days=14,
        )
        assert status.level == TokenExpiryLevel.WARN

    def test_unknown_includes_message(self):
        status = assess_token_expiry(introspection=None, access_token=None)
        assert status.level == TokenExpiryLevel.UNKNOWN
        assert "LINKEDIN_CLIENT_ID" in status.message


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
        },
        status=200,
    )
    intro = introspect_access_token("test-access-token-value-here")
    assert intro is not None
    assert intro.expires_on == date(2024, 1, 11)


def test_fingerprint_detects_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))
    assert detect_token_rotation("token-a-value-long-enough-here") is False
    assert detect_token_rotation("token-b-value-long-enough-here") is True
    assert token_fingerprint("a") != token_fingerprint("b")


class TestCliMain:
    def test_cli_missing_token_no_keyring_backend(self, monkeypatch, capsys):
        monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN", raising=False)
        with patch("linkedin_api.utils.token_secrets.keyring") as mock_kr:
            mock_kr.get_password.side_effect = NoKeyringError("no backend")
            with pytest.raises(SystemExit) as exc:
                main([])
        assert exc.value.code == EXIT_ERROR
        out = capsys.readouterr().out
        assert "not found" in out.lower()
        assert "❌" not in out

    def test_cli_unknown_expiry_exit_code(self, monkeypatch, capsys):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "a" * 40)
        monkeypatch.delenv("LINKEDIN_CLIENT_ID", raising=False)
        monkeypatch.delenv("LINKEDIN_CLIENT_SECRET", raising=False)
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == EXIT_EXPIRY_UNKNOWN
        assert "UNKNOWN:" in capsys.readouterr().out

    def test_exit_codes(self):
        from linkedin_api.utils.token_check import TokenHealthLevel, TokenHealthReport
        from linkedin_api.utils.token_secrets import SecretRetrievalIssue

        r = TokenHealthReport(
            level=TokenHealthLevel.WARN,
            retrieval_issue=SecretRetrievalIssue.OK,
            expiry_level=TokenExpiryLevel.WARN,
            api_valid=None,
            messages=("warn",),
        )
        assert exit_code_for_report(r, warn_exit_code=True) == EXIT_WARN
        assert exit_code_for_report(r, warn_exit_code=False) == EXIT_OK


class TestLogStartupTokenHealth:
    def test_introspection_called_once(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "a" * 40)
        monkeypatch.setenv("LINKEDIN_CLIENT_ID", "cid")
        monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "secret")
        calls: list[str] = []

        def fake_introspect(token: str, **_kw):
            calls.append(token)
            return TokenIntrospection(
                active=True,
                status="active",
                expires_on=date(2099, 1, 1),
                issued_on=None,
                scope=None,
            )

        with patch(
            "linkedin_api.utils.token_expiry.introspect_access_token",
            side_effect=fake_introspect,
        ):
            log_startup_token_health(probe_api=False)
        assert len(calls) == 1

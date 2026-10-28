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
    build_token_health_report,
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
    format_missing_app_credentials_message,
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
        assert result.source == "env:LINKEDIN_ACCESS_TOKEN"

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

    def test_keyring_falls_back_to_empty_account(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("LINKEDIN_ACCOUNT", "user@example.com")

        def fake_get_password(service, account):
            if service == "LINKEDIN_CLIENT_SECRET" and account == "user@example.com":
                return None
            if service == "LINKEDIN_CLIENT_SECRET" and account == "":
                return "secret-from-empty-account"
            return None

        with patch("linkedin_api.utils.token_secrets.keyring") as mock_kr:
            mock_kr.get_password.side_effect = fake_get_password
            result = retrieve_secret("LINKEDIN_CLIENT_SECRET", log_failures=False)
        assert result.issue == SecretRetrievalIssue.OK
        assert result.value == "secret-from-empty-account"
        assert 'account=""' in (result.source or "")

    def test_env_shadowing_warning_when_keyring_differs(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "env-token-value-here-xxxxxxxx")
        monkeypatch.setenv("LINKEDIN_ACCOUNT", "user@example.com")
        with patch("linkedin_api.utils.token_secrets.keyring") as mock_kr:
            mock_kr.get_password.side_effect = lambda svc, acct: (
                "keyring-token-value-here-yyyyyyyy"
                if svc == "LINKEDIN_ACCESS_TOKEN" and acct == "user@example.com"
                else None
            )
            result = retrieve_secret(log_failures=False)
        assert result.issue == SecretRetrievalIssue.OK
        assert result.shadowing_warning is not None
        assert "shadowing" in result.shadowing_warning.lower()
        assert "user@example.com" in result.shadowing_warning


class TestFormatMissingCredentials:
    def test_darwin_shows_security_only(self):
        msg = format_missing_app_credentials_message(
            ["LINKEDIN_CLIENT_SECRET"], platform="darwin"
        )
        assert "security add-generic-password" in msg
        assert "python -m keyring" not in msg
        assert "  • " in msg

    def test_linux_shows_keyring_cli_only(self):
        msg = format_missing_app_credentials_message(
            ["LINKEDIN_CLIENT_SECRET"], platform="linux"
        )
        assert "python -m keyring set" in msg
        assert "security add-generic-password" not in msg


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
            introspection=self._intro(
                expires_on=date(2026, 11, 1),
                issued_on=date(2026, 10, 1),
            ),
            warn_days=14,
        )
        assert status.level == TokenExpiryLevel.WARN
        assert "created 2026-10-01" in status.message
        assert "expires 2026-11-01" in status.message

    def test_ok_includes_created_and_expires(self):
        status = assess_token_expiry(
            today=date(2026, 10, 1),
            introspection=self._intro(
                expires_on=date(2026, 12, 1),
                issued_on=date(2026, 6, 1),
            ),
            warn_days=14,
        )
        assert status.level == TokenExpiryLevel.OK
        assert "created 2026-06-01" in status.message
        assert "expires 2026-12-01" in status.message

    def test_expired_status_includes_dates(self):
        status = assess_token_expiry(
            today=date(2026, 10, 1),
            introspection=self._intro(
                active=False,
                status="expired",
                expires_on=date(2026, 9, 1),
                issued_on=date(2026, 3, 1),
            ),
        )
        assert status.level == TokenExpiryLevel.EXPIRED
        assert "created 2026-03-01" in status.message
        assert "expired 2026-09-01" in status.message

    def test_unknown_includes_message(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_CLIENT_ID", raising=False)
        monkeypatch.delenv("LINKEDIN_CLIENT_SECRET", raising=False)
        status = assess_token_expiry(introspection=None, access_token="token-value")
        assert status.level == TokenExpiryLevel.UNKNOWN
        assert "LINKEDIN_CLIENT_ID" in status.message
        assert "LINKEDIN_CLIENT_SECRET" in status.message
        assert "OS keyring" in status.message


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

    def test_cli_prints_token_source_once(self, monkeypatch, capsys):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "a" * 40)
        monkeypatch.setenv("LINKEDIN_CLIENT_ID", "cid")
        monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "secret")
        with patch(
            "linkedin_api.utils.token_expiry.introspect_access_token",
            return_value=TokenIntrospection(
                active=True,
                status="active",
                expires_on=date(2099, 1, 1),
                issued_on=date(2026, 1, 1),
                scope=None,
            ),
        ):
            with pytest.raises(SystemExit) as exc:
                main([])
        assert exc.value.code == EXIT_OK
        out = capsys.readouterr().out
        assert out.count("Access token source:") == 1
        assert "env:LINKEDIN_ACCESS_TOKEN" in out

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


class TestBuildTokenHealthReportLogging:
    def test_build_does_not_log_unknown_expiry(self, monkeypatch, caplog):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "a" * 40)
        monkeypatch.delenv("LINKEDIN_CLIENT_ID", raising=False)
        monkeypatch.delenv("LINKEDIN_CLIENT_SECRET", raising=False)
        with caplog.at_level(logging.WARNING):
            build_token_health_report(probe_api=False)
        assert not any("linkedin_token_health" in r.message for r in caplog.records)

    def test_startup_still_logs_unknown(self, monkeypatch, caplog):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "a" * 40)
        monkeypatch.delenv("LINKEDIN_CLIENT_ID", raising=False)
        monkeypatch.delenv("LINKEDIN_CLIENT_SECRET", raising=False)
        with caplog.at_level(logging.WARNING):
            log_startup_token_health(probe_api=False)
        assert any("linkedin_token_health" in r.message for r in caplog.records)

    def test_report_includes_token_source(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "a" * 40)
        monkeypatch.setenv("LINKEDIN_CLIENT_ID", "cid")
        monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "secret")
        with patch(
            "linkedin_api.utils.token_expiry.introspect_access_token",
            return_value=TokenIntrospection(
                active=True,
                status="active",
                expires_on=date(2099, 1, 1),
                issued_on=date(2026, 1, 1),
                scope=None,
            ),
        ):
            report = build_token_health_report(probe_api=False)
        assert report.token_source == "env:LINKEDIN_ACCESS_TOKEN"
        assert any("Access token source:" in m for m in report.messages)


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

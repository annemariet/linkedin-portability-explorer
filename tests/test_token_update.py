"""Tests for linkedin-update-token OAuth flow and redaction."""

import logging
from unittest.mock import patch

import pytest

from linkedin_api.utils.token_oauth import TokenExchangeResult
from linkedin_api.utils.token_secrets import run_token_apply_hook
from linkedin_api.utils.token_update import run_update

_FIXTURE_CLIENT_ID = "fixture-client-id-for-url-only"
_FIXTURE_CLIENT_SECRET = "fixture-client-secret-never-printed"
_FIXTURE_ACCESS_TOKEN = "fixture-access-token-never-printed"
_FIXTURE_CODE = "fixture-auth-code-value"
_FIXTURE_STATE = "fixture-oauth-state-value"


class TestRunUpdateRedaction:
    def test_secrets_not_in_stdout_or_logs(self, monkeypatch, capsys, caplog):
        monkeypatch.setenv("LINKEDIN_CLIENT_ID", _FIXTURE_CLIENT_ID)
        monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", _FIXTURE_CLIENT_SECRET)
        monkeypatch.setenv("LINKEDIN_ACCOUNT", "test-account")
        monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN", raising=False)

        exchange = TokenExchangeResult(
            access_token=_FIXTURE_ACCESS_TOKEN,
            refresh_token=None,
            expires_in=5_184_000,
        )

        with patch(
            "linkedin_api.utils.token_update.new_oauth_state",
            return_value=_FIXTURE_STATE,
        ):
            with patch(
                "linkedin_api.utils.token_update.exchange_authorization_code",
                return_value=exchange,
            ):
                with patch("linkedin_api.utils.token_update.store_access_token"):
                    with patch(
                        "linkedin_api.utils.token_update.build_token_health_report"
                    ) as mock_report:
                        from linkedin_api.utils.token_check import (
                            TokenHealthLevel,
                            TokenHealthReport,
                        )
                        from linkedin_api.utils.token_secrets import (
                            SecretRetrievalIssue,
                        )

                        mock_report.return_value = TokenHealthReport(
                            level=TokenHealthLevel.OK,
                            retrieval_issue=SecretRetrievalIssue.OK,
                            expiry_level=None,
                            api_valid=None,
                            messages=(
                                "Access token source: env:LINKEDIN_ACCESS_TOKEN.",
                                "Token OK",
                            ),
                            token_source="env:LINKEDIN_ACCESS_TOKEN",
                        )
                        with caplog.at_level(logging.DEBUG):
                            code = run_update(
                                redirect_or_code=(
                                    f"https://example.test/cb?code={_FIXTURE_CODE}"
                                    f"&state={_FIXTURE_STATE}"
                                ),
                            )

        assert code == 0
        out = capsys.readouterr().out
        logs = caplog.text
        assert _FIXTURE_CLIENT_SECRET not in out
        assert _FIXTURE_CLIENT_SECRET not in logs
        assert _FIXTURE_ACCESS_TOKEN not in out
        assert _FIXTURE_ACCESS_TOKEN not in logs
        assert _FIXTURE_CODE not in logs
        assert _FIXTURE_CLIENT_ID in out
        assert _FIXTURE_CLIENT_ID not in logs

    def test_state_mismatch_skips_exchange(self, monkeypatch, capsys):
        monkeypatch.setenv("LINKEDIN_CLIENT_ID", _FIXTURE_CLIENT_ID)
        monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", _FIXTURE_CLIENT_SECRET)

        with patch(
            "linkedin_api.utils.token_update.new_oauth_state",
            return_value=_FIXTURE_STATE,
        ):
            with patch(
                "linkedin_api.utils.token_update.exchange_authorization_code",
            ) as mock_exchange:
                code = run_update(
                    redirect_or_code=(
                        f"https://example.test/cb?code={_FIXTURE_CODE}&state=wrong"
                    ),
                )
        assert code == 1
        mock_exchange.assert_not_called()
        assert "state mismatch" in capsys.readouterr().out.lower()


class TestTokenApplyHook:
    _TOKEN = "fixture-apply-hook-token-never-printed"

    def test_nonzero_exit_without_leaking_token(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_TOKEN_APPLY_CMD", "/bin/false")
        with pytest.raises(RuntimeError, match="exited with code 1"):
            run_token_apply_hook(self._TOKEN)

    def test_timeout_without_leaking_token(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_TOKEN_APPLY_CMD", "/bin/sleep 5")
        monkeypatch.setenv("LINKEDIN_TOKEN_APPLY_TIMEOUT", "0.01")
        with pytest.raises(RuntimeError, match="timed out"):
            run_token_apply_hook(self._TOKEN)


class TestRunUpdateHookValidation:
    def test_invalid_apply_timeout_before_oauth(self, monkeypatch, capsys):
        monkeypatch.setenv("LINKEDIN_CLIENT_ID", _FIXTURE_CLIENT_ID)
        monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", _FIXTURE_CLIENT_SECRET)
        monkeypatch.setenv("LINKEDIN_TOKEN_APPLY_TIMEOUT", "abc")

        with patch("linkedin_api.utils.token_update.exchange_authorization_code") as ex:
            code = run_update(
                redirect_or_code=(
                    f"https://example.test/cb?code={_FIXTURE_CODE}&state=x"
                ),
            )
        out = capsys.readouterr().out
        assert code == 1
        assert "ERROR:" in out
        assert "LINKEDIN_TOKEN_APPLY_TIMEOUT" in out
        ex.assert_not_called()

    def test_hook_oserror_exits_cleanly(self, monkeypatch, capsys):
        monkeypatch.setenv("LINKEDIN_CLIENT_ID", _FIXTURE_CLIENT_ID)
        monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", _FIXTURE_CLIENT_SECRET)
        monkeypatch.setenv("LINKEDIN_TOKEN_APPLY_CMD", "echo;")

        exchange = TokenExchangeResult(
            access_token=_FIXTURE_ACCESS_TOKEN,
            refresh_token=None,
            expires_in=60,
        )
        with patch(
            "linkedin_api.utils.token_update.new_oauth_state",
            return_value=_FIXTURE_STATE,
        ):
            with patch(
                "linkedin_api.utils.token_update.exchange_authorization_code",
                return_value=exchange,
            ):
                with patch("linkedin_api.utils.token_update.store_access_token"):
                    code = run_update(
                        redirect_or_code=(
                            f"https://example.test/cb?code={_FIXTURE_CODE}"
                            f"&state={_FIXTURE_STATE}"
                        ),
                        skip_check=True,
                    )

        out = capsys.readouterr().out
        assert code == 1
        assert "ERROR:" in out
        assert "could not be executed" in out
        assert _FIXTURE_ACCESS_TOKEN not in out

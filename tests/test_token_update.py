"""Tests for linkedin-update-token OAuth flow and redaction."""

import logging
from unittest.mock import patch

from linkedin_api.utils.token_oauth import TokenExchangeResult
from linkedin_api.utils.token_update import run_update

_FIXTURE_CLIENT_ID = "fixture-client-id-for-url-only"
_FIXTURE_CLIENT_SECRET = "fixture-client-secret-never-printed"
_FIXTURE_ACCESS_TOKEN = "fixture-access-token-never-printed"
_FIXTURE_CODE = "fixture-auth-code-value"


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
                    from linkedin_api.utils.token_secrets import SecretRetrievalIssue

                    mock_report.return_value = TokenHealthReport(
                        level=TokenHealthLevel.OK,
                        retrieval_issue=SecretRetrievalIssue.OK,
                        expiry_level=None,
                        api_valid=None,
                        messages=("Token OK",),
                    )
                    with caplog.at_level(logging.DEBUG):
                        code = run_update(
                            redirect_or_code=f"https://example.test/cb?code={_FIXTURE_CODE}",
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

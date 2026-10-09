"""Tests for env-first secret retrieval and Scalingo alerts."""

import logging
from unittest.mock import patch

from linkedin_api.utils.secret_retrieval import (
    SecretRetrievalIssue,
    retrieve_secret,
)


class TestRetrieveSecret:
    def test_env_wins_over_keyring(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "env-token-value-here")
        with patch("linkedin_api.utils.secret_retrieval.keyring") as mock_kr:
            mock_kr.get_password.return_value = "keyring-token-value-here"
            result = retrieve_secret(log_scalingo_alerts=False)
        assert result.issue == SecretRetrievalIssue.OK
        assert result.value == "env-token-value-here"
        assert result.source == "env"
        mock_kr.get_password.assert_not_called()

    def test_keyring_used_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN", raising=False)
        with patch("linkedin_api.utils.secret_retrieval.keyring") as mock_kr:
            mock_kr.get_password.return_value = "keyring-token-value-here"
            result = retrieve_secret(log_scalingo_alerts=False)
        assert result.value == "keyring-token-value-here"
        assert result.source == "keyring"

    def test_env_set_but_empty(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "   ")
        result = retrieve_secret(log_scalingo_alerts=False)
        assert result.issue == SecretRetrievalIssue.ENV_SET_BUT_EMPTY
        assert result.value is None

    def test_scalingo_logs_error_when_env_empty(self, monkeypatch, caplog):
        monkeypatch.setenv("SCALINGO_APP", "my-app")
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "")
        with caplog.at_level(logging.ERROR):
            retrieve_secret(log_scalingo_alerts=True)
        assert "scalingo_secret_retrieval" in caplog.text
        assert "empty" in caplog.text.lower()

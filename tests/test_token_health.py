"""Tests for token health reporting and startup logging."""

from datetime import date
from unittest.mock import patch

from linkedin_api.utils.token_health import log_startup_token_health
from linkedin_api.utils.token_introspection import TokenIntrospection


class TestLogStartupTokenHealth:
    def test_introspection_called_once_on_startup(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "a" * 40)
        calls: list[str] = []

        def fake_introspect(token: str):
            calls.append(token)
            return TokenIntrospection(
                active=True,
                status="active",
                expires_on=date(2099, 1, 1),
                issued_on=None,
                scope=None,
            )

        with patch(
            "linkedin_api.utils.token_lifecycle.introspect_access_token",
            side_effect=fake_introspect,
        ):
            log_startup_token_health(probe_api=False)

        assert len(calls) == 1

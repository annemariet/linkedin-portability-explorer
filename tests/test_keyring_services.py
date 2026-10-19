"""Tests for configurable keyring service names."""

from linkedin_api.utils.keyring_services import (
    keyring_service_candidates,
    primary_keyring_service,
)


class TestKeyringServices:
    def test_default_primary_service(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_KEYRING_SERVICE", raising=False)
        monkeypatch.delenv("LINKEDIN_KEYRING_LEGACY_SERVICES", raising=False)
        assert primary_keyring_service() == "linkedin-portability-explorer"
        assert keyring_service_candidates() == ("linkedin-portability-explorer",)

    def test_override_primary_and_legacy(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_KEYRING_SERVICE", "team-a")
        monkeypatch.setenv("LINKEDIN_KEYRING_LEGACY_SERVICES", "old-b, old-c,team-a")
        assert primary_keyring_service() == "team-a"
        assert keyring_service_candidates() == ("team-a", "old-b", "old-c")

"""Tests for LINKEDIN_KEYRING_SERVICE resolution."""

from linkedin_api.utils.keyring_services import keyring_service_candidates


class TestKeyringServiceCandidates:
    def test_default_service(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_KEYRING_SERVICE", raising=False)
        assert keyring_service_candidates() == ("linkedin-portability-explorer",)

    def test_comma_separated_list(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_KEYRING_SERVICE", "a, b,c")
        assert keyring_service_candidates() == ("a", "b", "c")

"""Tests for llm_config module (import, config parsing, key resolution)."""

import pytest
from unittest.mock import patch

from linkedin_api.llm_config import (
    AnthropicLLMClient,
    _resolve_api_key,
    _resolve_provider_model,
    create_llm,
)


def test_resolve_provider_model_stage_override(monkeypatch):
    """Stage-specific env vars override global when set."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("LLM_SUMMARY_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_SUMMARY_MODEL", "llama3.2:3b")
    monkeypatch.setenv("LLM_REPORT_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_REPORT_MODEL", "claude-3-5-sonnet-20241022")

    p, m = _resolve_provider_model("summary")
    assert p == "ollama"
    assert m == "llama3.2:3b"

    p, m = _resolve_provider_model("report")
    assert p == "anthropic"
    assert m == "claude-3-5-sonnet-20241022"

    p, m = _resolve_provider_model(None)
    assert p == "anthropic"
    assert m == "claude-sonnet-4-5"


def test_get_report_model_id_includes_model_in_cache_key(monkeypatch):
    """Report model id used for cache invalidation when model changes."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-5")
    from linkedin_api.llm_config import get_report_model_id

    assert get_report_model_id() == "anthropic:claude-sonnet-4-5"

    monkeypatch.setenv("LLM_REPORT_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_REPORT_MODEL", "llama3.2:3b")
    assert get_report_model_id() == "ollama:llama3.2:3b"

    assert get_report_model_id("mammouth", "gpt-4o") == "mammouth:gpt-4o"

    from linkedin_api.llm_config import OPENAI_COMPAT_DEFAULT_MODEL

    assert (
        get_report_model_id("mammouth", "gemini-2.5-flash-lite")
        == f"mammouth:{OPENAI_COMPAT_DEFAULT_MODEL}"
    )


def test_get_default_provider_model_maps_openai_to_mammouth(monkeypatch):
    """UI default for openai provider is mammouth; default model is gpt-5.4-nano."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_SUMMARY_MODEL", raising=False)
    from linkedin_api.llm_config import get_default_provider_model

    p, m = get_default_provider_model("summary")
    assert p == "mammouth"
    assert m == "gpt-5.4-nano"


def test_resolve_provider_model_fallback_to_global(monkeypatch):
    """When stage-specific vars unset, summary uses LLM_MODEL then openai default."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    monkeypatch.delenv("LLM_SUMMARY_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_SUMMARY_MODEL", raising=False)

    p, m = _resolve_provider_model("summary")
    assert p == "openai"
    assert m == "gpt-4o-mini"


def test_create_llm_mammouth_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mammouth")
    monkeypatch.setenv("MAMMOUTH_API_KEY", "sk-mammouth")
    from linkedin_api.llm_config import OpenAICompatLLM

    llm = create_llm(quiet=True)
    assert isinstance(llm, OpenAICompatLLM)
    assert "mammouth.ai" in str(getattr(llm._client, "base_url", ""))


def test_create_llm_unknown_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "unknown_provider")
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        create_llm()


def test_module_importable():
    from linkedin_api.llm_config import create_llm  # noqa: F401


class TestResolveApiKey:
    def test_llm_api_key_env_var(self, monkeypatch):
        monkeypatch.delenv("MAMMOUTH_API_KEY", raising=False)
        monkeypatch.setenv("LLM_API_KEY", "sk-test-123")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        key, source = _resolve_api_key(quiet=True)
        assert key == "sk-test-123"
        assert "LLM_API_KEY" in source

    def test_openai_api_key_fallback(self, monkeypatch):
        monkeypatch.delenv("MAMMOUTH_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-456")
        with patch("keyring.get_password", return_value=None):
            key, source = _resolve_api_key(quiet=True)
        assert key == "sk-openai-456"
        assert "OPENAI_API_KEY" in source

    def test_mammouth_api_key_env(self, monkeypatch):
        monkeypatch.setenv("MAMMOUTH_API_KEY", "sk-mammouth")
        key, source = _resolve_api_key(quiet=True)
        assert key == "sk-mammouth"
        assert "MAMMOUTH_API_KEY" in source

    def test_no_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("MAMMOUTH_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with patch("keyring.get_password", return_value=None):
            key, source = _resolve_api_key(quiet=True)
        assert key is None
        assert source is None

    def test_llm_api_key_takes_priority_over_openai(self, monkeypatch):
        monkeypatch.delenv("MAMMOUTH_API_KEY", raising=False)
        monkeypatch.setenv("LLM_API_KEY", "sk-llm")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        key, _ = _resolve_api_key(quiet=True)
        assert key == "sk-llm"


def test_create_llm_anthropic_defaults(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xyz")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    llm = create_llm(quiet=True)
    assert isinstance(llm, AnthropicLLMClient)
    assert llm._model == "claude-sonnet-4-5"
    assert llm._max_tokens == 8192

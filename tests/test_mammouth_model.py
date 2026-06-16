"""Tests for Mammouth chat model resolution."""

from linkedin_api.llm_config import (
    OPENAI_COMPAT_DEFAULT_MODEL,
    resolve_mammouth_chat_model,
)


def test_resolve_mammouth_chat_model_rewrites_gemini_25():
    assert (
        resolve_mammouth_chat_model("gemini-2.5-flash-lite", quiet=True)
        == OPENAI_COMPAT_DEFAULT_MODEL
    )


def test_resolve_mammouth_chat_model_keeps_gpt():
    assert resolve_mammouth_chat_model("gpt-5-nano", quiet=True) == "gpt-5-nano"

"""Regression: explorer summaries use prose prompts, not JSON mode."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from linkedin_api.llm_config import LLMResponse, OpenAICompatLLM
from linkedin_api.summarize_posts import _summarize_one


def test_openai_compat_skips_json_format_when_disabled():
    llm = OpenAICompatLLM(
        model="gpt-5.4-nano",
        api_key="sk-test",
        base_url="https://api.mammouth.ai/v1",
        json_mode=False,
    )
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="TLDR: ok\n"))]
    )
    llm._client = mock_client

    llm.invoke("Summarize this.", system_instruction="Be brief.")

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert "response_format" not in kwargs


@patch("linkedin_api.summarize_posts._summarize_one", return_value=True)
@patch("linkedin_api.summarize_posts.create_llm")
@patch(
    "linkedin_api.summarize_posts.list_posts_for_summary",
    return_value=[{"urn": "urn:li:activity:1", "content": "x" * 60}],
)
def test_summarize_posts_requests_prose_llm(mock_list, mock_create_llm, mock_one):
    from linkedin_api.summarize_posts import summarize_posts

    summarize_posts(quiet=True)
    mock_create_llm.assert_called_once()
    assert mock_create_llm.call_args.kwargs.get("json_mode") is False


@patch("linkedin_api.summarize_posts.create_llm")
@patch("linkedin_api.summarize_posts.load_metadata")
def test_summarize_one_parses_prose_response(mock_meta, mock_create_llm):
    del mock_create_llm
    mock_meta.return_value = {"post_author": "Ada", "post_url": ""}
    llm = MagicMock()
    llm.invoke.return_value = LLMResponse(
        content="AUTHOR: Ada\nTLDR: Test hook.\n- **Point** one.\nTOPICS: test\n"
    )

    post = {"urn": "urn:li:activity:1", "content": "Hello " * 20}
    with patch("linkedin_api.summarize_posts.update_summary_metadata") as mock_update:
        ok = _summarize_one(post, llm, model_id="mammouth:gpt-5.4-nano")

    assert ok is True
    mock_update.assert_called_once()

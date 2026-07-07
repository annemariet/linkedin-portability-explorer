"""Tests for compare_extractors module."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from linkedin_api.compare_extractors import compare_url


@pytest.fixture(autouse=True)
def use_tmp_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))


def _mock_backends(httpx_result, tavily_result):
    """``_BODY_BACKENDS`` is a shared dict object (imported by reference into
    compare_extractors), so patch.dict mutates it in place — patching the
    ``_fetch_html_body``/``_fetch_tavily`` names themselves would not affect
    the function references the dict already holds."""
    return patch.dict(
        "linkedin_api.fetch_linked_content._BODY_BACKENDS",
        {"httpx": httpx_result, "tavily": tavily_result},
    )


class TestCompareUrl:
    def test_runs_both_backends(self):
        httpx_fn = lambda url: ("HTTPX Title", "httpx body", [])  # noqa: E731
        tavily_fn = lambda url: (  # noqa: E731
            "Tavily Title",
            "tavily body",
            ["https://cdn.example.com/img.jpg"],
        )
        with (
            patch(
                "linkedin_api.compare_extractors.resolve_redirect",
                return_value="https://example.com/article",
            ),
            _mock_backends(httpx_fn, tavily_fn),
        ):
            results = compare_url("https://example.com/article")

        assert results["httpx"].title == "HTTPX Title"
        assert results["httpx"].content == "httpx body"
        assert results["httpx"].images == []
        assert results["tavily"].title == "Tavily Title"
        assert results["tavily"].content == "tavily body"
        assert results["tavily"].images == ["https://cdn.example.com/img.jpg"]

    def test_backend_error_is_captured_not_raised(self):
        httpx_fn = lambda url: ("Title", "body", [])  # noqa: E731

        def tavily_fn(url):
            raise ValueError("TAVILY_API_KEY not configured")

        with (
            patch(
                "linkedin_api.compare_extractors.resolve_redirect",
                return_value="https://example.com/article",
            ),
            _mock_backends(httpx_fn, tavily_fn),
        ):
            results = compare_url("https://example.com/article")

        assert results["httpx"].ok is True
        assert results["tavily"].ok is False
        assert "TAVILY_API_KEY" in results["tavily"].error

    def test_metadata_only_type_skips_both_backends(self):
        with patch(
            "linkedin_api.compare_extractors.resolve_redirect",
            return_value="https://www.youtube.com/watch?v=abc123",
        ):
            results = compare_url("https://youtu.be/abc123")

        assert "metadata-only" in results["httpx"].error
        assert "metadata-only" in results["tavily"].error

    def test_writes_output_files(self, tmp_path):
        out_dir = tmp_path / "cmp"
        same_fn = lambda url: ("Title", "body text", [])  # noqa: E731
        with (
            patch(
                "linkedin_api.compare_extractors.resolve_redirect",
                return_value="https://example.com/article",
            ),
            _mock_backends(same_fn, same_fn),
        ):
            compare_url("https://example.com/article", out_dir=out_dir)

        written = {p.name for p in out_dir.glob("*.md")}
        assert any(name.endswith("-httpx.md") for name in written)
        assert any(name.endswith("-tavily.md") for name in written)

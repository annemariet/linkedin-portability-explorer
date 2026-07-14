"""Tests for linked article summarization selection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from linkedin_api.fetch_linked_content import (
    _resource_dir,
    _url_stem,
    save_resource,
)
from linkedin_api.fetch_linked_content import FetchResult
from linkedin_api.summarize_resources import list_resources_for_summary


@pytest.fixture(autouse=True)
def use_tmp_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))


def _write_resource(
    url: str,
    *,
    content: str = "x" * 250,
    cited_by: list[str] | None = None,
    tldr: str = "",
    summary_bullets: list[str] | None = None,
) -> Path:
    stem = _url_stem(url)
    path = _resource_dir() / f"{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": url,
        "resolved_url": url,
        "title": "Article title",
        "content": content,
        "url_type": "article",
        "domain": "example.com",
        "error": "",
        "fetched_at": "2024-01-01T00:00:00+00:00",
        "cited_by": cited_by or [],
        "tldr": tldr,
        "summary_bullets": summary_bullets or [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestListResourcesForSummary:
    def test_matches_cited_by_post_id(self):
        url = "https://example.com/article"
        _write_resource(url, cited_by=["123"])
        out = list_resources_for_summary(urns={"123"})
        assert len(out) == 1
        assert out[0].url == url

    def test_excludes_when_out_of_scope(self):
        url = "https://example.com/out-of-scope"
        _write_resource(url, cited_by=["other-post"])
        out = list_resources_for_summary(urns={"123"})
        assert out == []

    def test_excludes_short_body(self):
        url = "https://example.com/short"
        _write_resource(url, content="too short", cited_by=["123"])
        out = list_resources_for_summary(urns={"123"})
        assert out == []

    def test_excludes_already_summarized(self):
        url = "https://example.com/done"
        _write_resource(
            url,
            cited_by=["123"],
            tldr="Hook.",
            summary_bullets=["Point one."],
        )
        out = list_resources_for_summary(urns={"123"})
        assert out == []

    def test_includes_when_force_resummarize(self):
        url = "https://example.com/redo"
        _write_resource(
            url,
            cited_by=["123"],
            tldr="Hook.",
            summary_bullets=["Point one."],
        )
        out = list_resources_for_summary(urns={"123"}, force=True)
        assert len(out) == 1


class TestFetchCitedByStem:
    def test_save_resource_uses_post_id_stem_not_urn_hash(self):
        url = "https://example.com/jepa"
        bad_urn = "urn:li:comment:(urn:li:ugcPost:123,456)"
        result = FetchResult(
            url=url,
            resolved_url=url,
            title="JEPA",
            content="x" * 300,
            url_type="article",
        )
        save_resource(url, result, citing_post_urns=["123"])
        saved = json.loads((_resource_dir() / f"{_url_stem(url)}.json").read_text())
        assert saved["cited_by"] == ["123"]
        assert bad_urn not in saved["cited_by"]

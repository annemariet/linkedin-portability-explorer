"""Tests for linked article summarization selection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from linkedin_api.enriched_record import EnrichedRecord
from linkedin_api.fetch_linked_content import _resource_dir, _url_stem
from linkedin_api.summarize_resources import (
    _resource_scope_keys,
    list_resources_for_summary,
    summary_scope_for_activities,
)


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


class TestSummaryScope:
    def test_scope_includes_post_id_and_post_urn(self):
        rec = EnrichedRecord(
            post_urn="urn:li:activity:123",
            post_url="https://www.linkedin.com/feed/update/urn:li:activity:123",
            content="",
            urls=[],
            interaction_type="reaction",
            reaction_type="LIKE",
            comment_text="",
            post_id="123",
            activity_id="a",
            timestamp=1,
            created_at="",
        )
        scope = summary_scope_for_activities([rec])
        assert scope == {"123", "urn:li:activity:123"}

    def test_resource_scope_adds_legacy_urn_hash(self):
        urn = "urn:li:activity:123"
        keys = _resource_scope_keys({urn, "123"})
        assert hashlib.sha256(urn.encode()).hexdigest() in keys


class TestListResourcesForSummary:
    def test_matches_cited_by_post_id(self):
        url = "https://example.com/article"
        _write_resource(url, cited_by=["123"])
        out = list_resources_for_summary(urns={"123", "urn:li:activity:123"})
        assert len(out) == 1
        assert out[0].url == url

    def test_matches_legacy_cited_by_urn_hash(self):
        url = "https://example.com/legacy"
        urn = "urn:li:activity:999"
        legacy = hashlib.sha256(urn.encode()).hexdigest()
        _write_resource(url, cited_by=[legacy])
        out = list_resources_for_summary(urns={"999", urn})
        assert len(out) == 1

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

"""Tests for enrich_activities module."""

from unittest.mock import patch

import pytest

from linkedin_api.content_store import (
    has_metadata,
    load_content,
    load_metadata,
    save_content,
)
from linkedin_api.enriched_record import EnrichedRecord
from linkedin_api.enrich_activities import enrich_activities
from linkedin_api.post_extraction import append_missing_resource_urls
from linkedin_api.utils.urls import is_comment_feed_url


class TestAppendMissingResourceUrls:
    def test_no_duplicate_when_short_in_body_matches_resolved_metadata(self):
        """Body has gisk.ar short link; metadata list may store resolved URL after redirect."""
        body = "See https://gisk.ar/41lYlde for more."
        with (
            patch(
                "linkedin_api.post_extraction.resolve_urls_for_metadata",
                return_value=["https://docs.giskard.ai/en/stable/"],
            ),
            patch(
                "linkedin_api.utils.urls.resolve_redirect",
                side_effect=lambda u: (
                    "https://docs.giskard.ai/en/stable/" if "gisk.ar" in u else u
                ),
            ),
        ):
            out = append_missing_resource_urls(
                body, ["https://docs.giskard.ai/en/stable/"]
            )
        assert "## Links" not in out


class TestIsCommentFeedUrl:
    def test_comment_urn_in_url(self):
        assert (
            is_comment_feed_url(
                "https://linkedin.com/feed/update/urn:li:comment:(activity:123,456)"
            )
            is True
        )

    def test_post_urn_in_url(self):
        assert (
            is_comment_feed_url("https://linkedin.com/feed/update/urn:li:activity:123")
            is False
        )


class TestEnrichSavesTimestamps:
    """Verify timestamp and post_created_at flow from activities into content store metadata."""

    @pytest.fixture(autouse=True)
    def use_tmp_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))

    def test_reaction_timestamp_and_post_created_saved_to_metadata(self):
        post_id = "123456"
        urn = f"urn:li:ugcPost:{post_id}"
        url = f"https://www.linkedin.com/feed/update/{urn}"
        ts_ms = 1700000000000
        post_created = "2024-01-15T10:30:00Z"

        save_content(post_id, "x" * 100, post_urn=urn)
        assert not has_metadata(post_id, post_urn=urn)

        activities = [
            EnrichedRecord(
                post_urn=urn,
                post_url=url,
                content="",
                urls=["https://example.com"],
                interaction_type="reaction",
                reaction_type=None,
                comment_text="",
                post_id=post_id,
                activity_id="",
                timestamp=ts_ms,
                created_at="",
                post_created_at=post_created,
            )
        ]
        _, count = enrich_activities(activities)
        assert count == 1

        meta = load_metadata(post_id, post_urn=urn)
        assert meta is not None
        assert meta.get("activity_time_iso") == "2023-11-14T22:13:20+00:00"
        assert meta.get("post_created_at") == post_created

    def test_login_wall_falls_back_to_csv_content_not_generic_blurb(self):
        """When HTTP fails, only ``post`` rows may use CSV body as .md (not reactions)."""
        urn = "urn:li:activity:7445812127325401089"
        url = "https://www.linkedin.com/feed/update/urn:li:activity:7445812127325401089"
        api_text = (
            "Real post body from the API with enough characters to pass the fifty "
            "character minimum for summarization and storage in the content store."
        )

        activities = [
            EnrichedRecord(
                post_urn=urn,
                post_url=url,
                content=api_text,
                urls=["https://example.org/paper"],
                interaction_type="post",
                reaction_type=None,
                comment_text="",
                post_id="7445812127325401089",
                activity_id="abc",
                timestamp=1,
                created_at="",
            )
        ]
        with patch(
            "linkedin_api.enrich_activities.fetch_linkedin_post_html",
            return_value=None,
        ):
            _, count = enrich_activities(activities)
        assert count == 1
        stored = load_content("7445812127325401089", post_urn=urn)
        assert api_text in (stored or "")
        assert "500 million" not in (stored or "")
        assert "https://example.org/paper" in (stored or "")
        meta = load_metadata("7445812127325401089", post_urn=urn)
        assert meta is not None
        assert meta.get("urls") == ["https://example.org/paper"]

    def test_login_wall_does_not_save_csv_body_for_reaction_rows(self):
        urn = "urn:li:activity:999"
        with patch(
            "linkedin_api.enrich_activities.fetch_linkedin_post_html",
            return_value=None,
        ):
            _, count = enrich_activities(
                [
                    EnrichedRecord(
                        post_urn=urn,
                        post_url=f"https://www.linkedin.com/feed/update/{urn}",
                        content="Wrong: this would be post text on a bad mapping",
                        urls=[],
                        interaction_type="reaction",
                        reaction_type="LIKE",
                        comment_text="",
                        post_id="999",
                        activity_id="x",
                        timestamp=1,
                        created_at="",
                    )
                ]
            )
        assert count == 0
        assert load_content("999", post_urn=urn) is None

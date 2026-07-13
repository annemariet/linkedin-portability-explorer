"""Tests for enrich_activities module."""

from unittest.mock import patch

import pytest

from linkedin_api.content_store import (
    has_metadata,
    load_content,
    load_metadata,
    save_content,
    save_metadata,
)
from linkedin_api.enriched_record import EnrichedRecord
from linkedin_api.enrich_activities import _run_enrichment, enrich_activities
from linkedin_api.post_extraction import append_missing_resource_urls
from linkedin_api.utils.urls import is_comment_feed_url


def _run_to_completion(activities):
    gen = _run_enrichment(activities)
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


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
        urn = "urn:li:ugcPost:123456"
        url = "https://www.linkedin.com/feed/update/urn:li:ugcPost:123456"
        ts_ms = 1700000000000
        post_created = "2024-01-15T10:30:00Z"

        save_content(urn, "x" * 100)
        assert not has_metadata(urn)

        activities = [
            EnrichedRecord(
                post_urn=urn,
                post_url=url,
                content="",
                urls=["https://example.com"],
                interaction_type="reaction",
                reaction_type=None,
                comment_text="",
                post_id="",
                activity_id="",
                timestamp=ts_ms,
                created_at="",
                post_created_at=post_created,
            )
        ]
        _, count = enrich_activities(activities)
        assert count == 1

        meta = load_metadata(urn)
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
        stored = load_content(urn)
        assert api_text in (stored or "")
        assert "500 million" not in (stored or "")
        assert "https://example.org/paper" in (stored or "")
        meta = load_metadata(urn)
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
        assert load_content(urn) is None

    def test_login_wall_reaction_with_no_urls_counted_not_dropped(self):
        """A reaction whose HTTP fetch fails and has no API urls to fall
        back on writes nothing (count == 0), but must still land in
        telemetry — not vanish uncounted."""
        urn = "urn:li:activity:998"
        with patch(
            "linkedin_api.enrich_activities.fetch_linkedin_post_html",
            return_value=None,
        ):
            count, telemetry = _run_to_completion(
                [
                    EnrichedRecord(
                        post_urn=urn,
                        post_url=f"https://www.linkedin.com/feed/update/{urn}",
                        content="",
                        urls=[],
                        interaction_type="reaction",
                        reaction_type="LIKE",
                        comment_text="",
                        post_id="998",
                        activity_id="y",
                        timestamp=1,
                        created_at="",
                    )
                ]
            )
        assert count == 0
        assert telemetry.fallback_http_fail_no_content == 1
        assert telemetry.total() == 1

    def test_row_missing_urn_is_counted(self):
        urn = ""
        count, telemetry = _run_to_completion(
            [
                EnrichedRecord(
                    post_urn=urn,
                    post_url="https://www.linkedin.com/feed/update/urn:li:activity:1",
                    content="",
                    urls=[],
                    interaction_type="reaction",
                    reaction_type="LIKE",
                    comment_text="",
                    post_id="1",
                    activity_id="z",
                    timestamp=1,
                    created_at="",
                )
            ]
        )
        assert count == 0
        assert telemetry.skip_missing_urn_or_url == 1
        assert telemetry.total() == 1

    def test_merge_noop_is_counted(self):
        """mode == "merge" but merge_enrichment_activity finds nothing to
        change (its own documented None-return case) — that row must
        still land in telemetry, not vanish uncounted."""
        from linkedin_api.post_extraction import ENRICHMENT_VERSION

        urn = "urn:li:activity:997"
        url = f"https://www.linkedin.com/feed/update/{urn}"
        save_content(urn, "x" * 100)
        save_metadata(
            urn,
            post_url=url,
            enrichment_version=ENRICHMENT_VERSION,
            activities_ids=["already-recorded"],
        )
        rec = EnrichedRecord(
            post_urn=urn,
            post_url=url,
            content="",
            urls=[],
            interaction_type="reaction",
            reaction_type="LIKE",
            comment_text="",
            post_id="997",
            activity_id="new-activity-id",
            timestamp=1,
            created_at="",
        )
        with patch(
            "linkedin_api.enrich_activities.merge_enrichment_activity",
            return_value=None,
        ):
            count, telemetry = _run_to_completion([rec])
        assert count == 0
        assert telemetry.merge_noop == 1
        assert telemetry.total() == 1

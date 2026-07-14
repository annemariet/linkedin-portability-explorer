"""Tests for content_store module -- file-based content storage."""

from unittest.mock import MagicMock, patch

import pytest

from linkedin_api.activity_csv import get_data_dir
from linkedin_api.content_store import (
    content_path,
    download_image_to_store,
    has_content,
    load_content,
    load_metadata,
    list_posts_needing_summary,
    list_summarized_metadata,
    merge_post_identity,
    needs_summary,
    resolve_urls_for_metadata,
    save_content,
    save_metadata,
    update_summary_metadata,
    update_urls_metadata,
)


@pytest.fixture(autouse=True)
def use_tmp_data_dir(monkeypatch, tmp_path):
    """Point the content store at a temp directory for all tests."""
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))


class TestSaveAndLoad:
    def test_roundtrip(self):
        post_id = "123456"
        urn = f"urn:li:ugcPost:{post_id}"
        save_content(post_id, "Hello world", post_urn=urn)
        assert load_content(post_id, post_urn=urn) == "Hello world"

    def test_overwrite(self):
        post_id = "123456"
        urn = f"urn:li:ugcPost:{post_id}"
        save_content(post_id, "v1", post_urn=urn)
        save_content(post_id, "v2", post_urn=urn)
        assert load_content(post_id, post_urn=urn) == "v2"

    def test_unicode_content(self):
        post_id = "999"
        urn = f"urn:li:ugcPost:{post_id}"
        text = "Inscrite ! Merci pour l'info \U0001f44d\U0001f3fb"
        save_content(post_id, text, post_urn=urn)
        assert load_content(post_id, post_urn=urn) == text

    def test_multiline_content(self):
        post_id = "888"
        urn = f"urn:li:ugcPost:{post_id}"
        text = "Line 1\nLine 2\n\nLine 4"
        save_content(post_id, text, post_urn=urn)
        assert load_content(post_id, post_urn=urn) == text

    def test_save_empty_post_id_raises(self):
        with pytest.raises(ValueError):
            save_content("", "some text")

    def test_save_empty_text_raises(self):
        with pytest.raises(ValueError):
            save_content("1", "")


class TestLoadContent:
    def test_missing_post_returns_none(self):
        assert load_content("9999999999999999999") is None

    def test_empty_post_id_returns_none(self):
        assert load_content("") is None


class TestHasContent:
    def test_exists_after_save(self):
        post_id = "777"
        urn = f"urn:li:ugcPost:{post_id}"
        assert has_content(post_id, post_urn=urn) is False
        save_content(post_id, "stored", post_urn=urn)
        assert has_content(post_id, post_urn=urn) is True

    def test_empty_post_id(self):
        assert has_content("") is False


class TestContentPath:
    def test_returns_path(self):
        path = content_path("123")
        assert path.suffix == ".md"
        assert path.name == "123.md"
        assert "content" in str(path)

    def test_different_post_ids_different_paths(self):
        p1 = content_path("111")
        p2 = content_path("222")
        assert p1 != p2


class TestMetadata:
    def test_save_and_load_metadata(self):
        post_id = "456"
        urn = f"urn:li:ugcPost:{post_id}"
        save_content(post_id, "Content here", post_urn=urn)
        save_metadata(
            post_id,
            summary="A summary",
            topics=["AI"],
            urls=["https://example.com"],
            post_urn=urn,
        )
        meta = load_metadata(post_id, post_urn=urn)
        assert meta["summary"] == "A summary"
        assert meta["topics"] == ["AI"]
        assert meta["urls"] == resolve_urls_for_metadata(["https://example.com"])

    def test_update_preserves_urls(self):
        post_id = "789"
        urn = f"urn:li:ugcPost:{post_id}"
        save_content(post_id, "Post content", post_urn=urn)
        save_metadata(
            post_id,
            urls=["https://x.com"],
            post_url="https://linkedin.com/feed/...",
            post_urn=urn,
        )
        update_summary_metadata(
            post_id,
            "Summary",
            ["topic1"],
            ["py"],
            ["Alice"],
            "paper",
            post_urn=urn,
        )
        meta = load_metadata(post_id, post_urn=urn)
        assert meta["summary"] == "Summary"
        assert meta["urls"] == resolve_urls_for_metadata(["https://x.com"])
        assert meta["post_url"] == "https://linkedin.com/feed/..."
        assert "summarized_at" in meta

    def test_schema_fields_and_activities_ids_merge(self):
        post_id = "7437247151593857024"
        urn = f"urn:li:activity:{post_id}"
        save_content(post_id, "x" * 100, post_urn=urn)
        save_metadata(
            post_id,
            post_url="https://www.linkedin.com/posts/example",
            post_urn=urn,
            post_author="Scott Condron",
            post_author_url="https://www.linkedin.com/in/condronscott/",
            activities_ids=["id-reaction-1"],
            urls=["https://github.com/foo/bar"],
        )
        save_metadata(
            post_id,
            activities_ids=["id-comment-2"],
            urls=["https://github.com/foo/bar"],
            post_url="https://www.linkedin.com/posts/example",
            post_urn=urn,
        )
        meta = load_metadata(post_id, post_urn=urn)
        assert meta["post_urn"] == urn
        assert meta["post_id"] == post_id
        assert meta["post_author"] == "Scott Condron"
        assert meta["post_author_url"] == "https://www.linkedin.com/in/condronscott/"
        assert meta["activities_ids"] == ["id-reaction-1", "id-comment-2"]

    def test_merge_post_identity_noop_returns_none(self):
        post_id = "1"
        urn = "urn:li:activity:merge_noop"
        save_content(post_id, "x" * 100, post_urn=urn)
        save_metadata(post_id, summary="S", post_urn=urn, activities_ids=["a"])
        assert (
            merge_post_identity(post_id, post_urn=urn, extra_activity_ids=["a"]) is None
        )


class TestNeedsSummary:
    def test_no_content(self):
        assert needs_summary("9999999999999999999") is False

    def test_content_without_summary(self):
        post_id = "12345"
        urn = f"urn:li:ugcPost:{post_id}"
        save_content(post_id, "x" * 100, post_urn=urn)
        assert needs_summary(post_id, post_urn=urn) is True

    def test_content_with_summary(self):
        post_id = "67890"
        urn = f"urn:li:ugcPost:{post_id}"
        save_content(post_id, "x" * 100, post_urn=urn)
        save_metadata(post_id, summary="Done", post_urn=urn)
        assert needs_summary(post_id, post_urn=urn) is True

    def test_content_with_tldr_only_incomplete(self):
        post_id = "tldr_only"
        urn = f"urn:li:ugcPost:{post_id}"
        save_content(post_id, "x" * 250, post_urn=urn)
        update_summary_metadata(
            post_id,
            summary="",
            topics=[],
            technologies=[],
            people=[],
            category=None,
            post_urn=urn,
            tldr="Hook sentence.",
        )
        assert needs_summary(post_id, post_urn=urn) is True

    def test_short_post_tldr_only_is_complete(self):
        post_id = "short"
        urn = f"urn:li:ugcPost:{post_id}"
        save_content(post_id, "Short post.", post_urn=urn)
        update_summary_metadata(
            post_id,
            summary="",
            topics=[],
            technologies=[],
            people=[],
            category=None,
            post_urn=urn,
            tldr="Short hook.",
        )
        assert needs_summary(post_id, post_urn=urn) is False

    def test_content_with_tldr_and_bullets_complete(self):
        post_id = "complete"
        urn = f"urn:li:ugcPost:{post_id}"
        save_content(post_id, "x" * 100, post_urn=urn)
        update_summary_metadata(
            post_id,
            summary="Hook.\n- Point one.",
            topics=["ai"],
            technologies=[],
            people=[],
            category=None,
            post_urn=urn,
            tldr="Hook.",
            summary_bullets=["Point one."],
        )
        assert needs_summary(post_id, post_urn=urn) is False


class TestListSummarizedMetadata:
    def test_includes_urn_for_content_lookup(self):
        post_id = "listed"
        urn = f"urn:li:ugcPost:{post_id}"
        save_content(post_id, "x" * 100, post_urn=urn)
        save_metadata(post_id, summary="A summary", post_urn=urn)
        metas = list_summarized_metadata()
        assert len(metas) == 1
        assert metas[0]["post_id"] == post_id
        assert metas[0]["urn"] == urn
        assert metas[0]["summary"] == "A summary"


class TestListPostsNeedingSummary:
    def test_filters_by_summary(self):
        save_content("100", "a" * 100, post_urn="urn:li:ugcPost:100")
        save_content("200", "b" * 100, post_urn="urn:li:ugcPost:200")
        update_summary_metadata(
            "200",
            summary="Done",
            topics=[],
            technologies=[],
            people=[],
            category=None,
            post_urn="urn:li:ugcPost:200",
            tldr="Done.",
            summary_bullets=["Detail."],
        )
        posts = list_posts_needing_summary()
        assert len(posts) == 1
        assert posts[0]["post_id"] == "100"
        assert posts[0]["urn"] == "urn:li:ugcPost:100"
        assert posts[0]["content"] == "a" * 100

    def test_scoped_by_urns(self):
        from linkedin_api.content_store import list_posts_for_summary

        save_content("111", "a" * 100, post_urn="urn:li:ugcPost:111")
        save_content("222", "b" * 100, post_urn="urn:li:ugcPost:222")
        scoped = list_posts_for_summary(urns={"111"})
        assert len(scoped) == 1
        assert scoped[0]["post_id"] == "111"
        assert scoped[0]["urn"] == "urn:li:ugcPost:111"


class TestUpdateUrlsMetadata:
    def test_sets_urls_on_new_post(self):
        post_id = "urls_new"
        update_urls_metadata(post_id, ["https://example.com"])
        meta = load_metadata(post_id)
        assert meta is not None
        assert meta["urls"][0].rstrip("/") == "https://example.com"

    def test_preserves_existing_summary(self):
        post_id = "urls_preserve"
        urn = f"urn:li:ugcPost:{post_id}"
        save_content(post_id, "Post text", post_urn=urn)
        save_metadata(post_id, summary="Keep me", topics=["AI"], post_urn=urn)
        update_urls_metadata(post_id, ["https://arxiv.org/abs/123"], post_urn=urn)
        meta = load_metadata(post_id, post_urn=urn)
        assert meta["summary"] == "Keep me"
        assert meta["topics"] == ["AI"]
        assert meta["urls"] == ["https://arxiv.org/abs/123"]

    def test_overwrites_existing_urls(self):
        post_id = "urls_overwrite"
        save_metadata(post_id, urls=["https://old.example.com"])
        update_urls_metadata(post_id, ["https://new.example.com"])
        meta = load_metadata(post_id)
        assert meta["urls"] == ["https://new.example.com"]

    def test_empty_list(self):
        post_id = "urls_empty"
        update_urls_metadata(post_id, [])
        meta = load_metadata(post_id)
        assert meta["urls"] == []


class TestDeduplication:
    def test_same_post_id_one_file(self):
        """Multiple saves for same post_id → one content file."""
        post_id = "7482038400523575296"
        urn = f"urn:li:ugcPost:{post_id}"
        content = "This is the post body."
        save_content(post_id, content, post_urn=urn)
        save_content(post_id, content, post_urn=urn)
        assert load_content(post_id, post_urn=urn) == content
        content_dir = content_path(post_id).parent
        assert len(list(content_dir.glob("*.md"))) == 1


class TestDownloadImageToStore:
    def _mock_response(
        self, content: bytes = b"fake-jpg-bytes", status_code: int = 200
    ):
        resp = MagicMock()
        resp.status_code = status_code
        resp.content = content
        return resp

    def test_downloads_to_content_dir(self):
        with patch("requests.get", return_value=self._mock_response()):
            path = download_image_to_store("https://cdn.example.com/photo.jpg")

        assert path is not None
        assert path.startswith("images/")
        assert (get_data_dir() / "content" / path).exists()

    def test_repeated_call_is_cached_not_refetched(self):
        with patch("requests.get", return_value=self._mock_response()) as mock_get:
            first = download_image_to_store("https://cdn.example.com/photo.jpg")
            second = download_image_to_store("https://cdn.example.com/photo.jpg")

        assert first == second
        mock_get.assert_called_once()

    def test_returns_none_on_http_error(self):
        with patch("requests.get", return_value=self._mock_response(status_code=404)):
            path = download_image_to_store("https://cdn.example.com/missing.jpg")

        assert path is None

    def test_returns_none_on_network_exception(self):
        with patch("requests.get", side_effect=ConnectionError("timeout")):
            path = download_image_to_store("https://cdn.example.com/photo.jpg")

        assert path is None

    def test_returns_none_for_empty_url(self):
        assert download_image_to_store("") is None

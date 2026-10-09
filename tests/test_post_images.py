"""Tests for linkedin_api.post_images (local durable images + CDN metadata)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from linkedin_api.post_images import (
    EXPIRED_CDN_FIXTURE_URL,
    cdn_url_is_expired,
    fetch_cdn_image_bytes,
    fetch_image_bytes_for_vault,
    image_meta_record,
    parse_linkedin_cdn_expires_at_unix,
)


def test_parse_linkedin_cdn_expires_at_unix() -> None:
    ts = parse_linkedin_cdn_expires_at_unix(EXPIRED_CDN_FIXTURE_URL)
    assert ts == 1780668000
    assert cdn_url_is_expired(
        EXPIRED_CDN_FIXTURE_URL,
        now=datetime(2026, 10, 9, tzinfo=UTC),
    )


def test_save_extraction_downloads_local_and_records_cdn_metadata(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))
    from linkedin_api.content_store import load_content, load_metadata
    from linkedin_api.post_extraction import (
        PostExtractionResult,
        save_extraction_to_store,
    )

    cdn = "https://media.licdn.com/dms/image/v2/example/feedshare-shrink_800/0?e=2147483647&v=beta&t=x"
    ext = PostExtractionResult(
        markdown_body="Post body text.",
        html_meta={},
        urls=[],
        mentions=[],
        hashtags=[],
        image_urls=[cdn],
    )

    def _fake_download(url: str) -> str | None:
        assert url == cdn
        images = tmp_path / "content" / "images"
        images.mkdir(parents=True)
        (images / "abc123.jpg").write_bytes(b"jpeg")
        return "images/abc123.jpg"

    with patch(
        "linkedin_api.post_extraction.download_image_to_store",
        side_effect=_fake_download,
    ):
        save_extraction_to_store(
            post_id="12345",
            post_urn="urn:li:activity:1",
            post_url="https://www.linkedin.com/feed/update/urn:li:activity:1",
            ext=ext,
            urls_from_api=[],
            activity_time_iso="2026-01-01T00:00:00Z",
            post_created="2026-01-01T00:00:00Z",
            activities_ids=["act-1"],
        )

    body = load_content("12345", post_urn="urn:li:activity:1")
    assert body is not None
    assert "![](images/abc123.jpg)" in body
    assert "media.licdn.com" not in body
    meta = load_metadata("12345", post_urn="urn:li:activity:1")
    assert meta is not None
    images = meta["images"]
    assert len(images) == 1
    assert images[0]["cdn_url"] == cdn
    assert images[0]["local_path"] == "images/abc123.jpg"
    assert "cdn_expires_at" in images[0]


def test_reenrich_preserves_local_embed_and_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))
    from linkedin_api.content_store import load_content, load_metadata, save_content
    from linkedin_api.post_extraction import (
        PostExtractionResult,
        save_extraction_to_store,
    )

    images = tmp_path / "content" / "images"
    images.mkdir(parents=True)
    (images / "existing.jpg").write_bytes(b"keep")
    save_content(
        "12345",
        "Older body.\n\n![](images/existing.jpg)",
        post_urn="urn:li:activity:1",
    )

    cdn = EXPIRED_CDN_FIXTURE_URL
    ext = PostExtractionResult(
        markdown_body="New extraction body.",
        html_meta={},
        urls=[],
        mentions=[],
        hashtags=[],
        image_urls=[cdn],
    )

    with patch("linkedin_api.post_extraction.download_image_to_store") as mock_dl:
        save_extraction_to_store(
            post_id="12345",
            post_urn="urn:li:activity:1",
            post_url="https://www.linkedin.com/feed/update/urn:li:activity:1",
            ext=ext,
            urls_from_api=[],
            activity_time_iso="2026-01-01T00:00:00Z",
            post_created="2026-01-01T00:00:00Z",
            activities_ids=["act-1"],
        )
        mock_dl.assert_not_called()

    body = load_content("12345", post_urn="urn:li:activity:1")
    assert body is not None
    assert "![](images/existing.jpg)" in body
    assert "media.licdn.com" not in body
    assert (images / "existing.jpg").read_bytes() == b"keep"
    meta = load_metadata("12345", post_urn="urn:li:activity:1")
    assert meta is not None
    assert meta["images"][0]["cdn_url"] == cdn
    assert meta["images"][0].get("local_path") == "images/existing.jpg"


def test_expired_cdn_fixture_skips_fetch_without_network(caplog) -> None:
    with patch("requests.get") as mock_get:
        result = fetch_cdn_image_bytes(EXPIRED_CDN_FIXTURE_URL)
    assert result is None
    mock_get.assert_not_called()
    assert "expired" in caplog.text.lower()


def test_fetch_cdn_image_bytes_logs_403_and_returns_none(caplog) -> None:
    fresh_url = EXPIRED_CDN_FIXTURE_URL.replace("e=1780668000", "e=2147483647")
    with patch("requests.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=403, content=b"")
        result = fetch_cdn_image_bytes(fresh_url)
    assert result is None
    mock_get.assert_called_once()
    assert "403" in caplog.text


def test_export_succeeds_when_expired_cdn_image_skipped(caplog) -> None:
    """Vault helper returns None; callers must not treat that as export failure."""
    rec = image_meta_record(EXPIRED_CDN_FIXTURE_URL)
    with patch("requests.get") as mock_get:
        assert fetch_image_bytes_for_vault(rec) is None
    mock_get.assert_not_called()
    assert "expired" in caplog.text.lower()


def test_vault_export_image_helper_does_not_raise_on_expired_metadata() -> None:
    rec = image_meta_record(EXPIRED_CDN_FIXTURE_URL)
    with patch("linkedin_api.post_images.fetch_cdn_image_bytes", return_value=None):
        assert fetch_image_bytes_for_vault(rec) is None

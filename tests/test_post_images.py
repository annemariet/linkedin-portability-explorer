"""Tests for linkedin_api.post_images (local durable images + CDN metadata)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from linkedin_api.post_images import (
    apply_post_image_sidecar,
    cdn_url_identity,
    cdn_url_is_expired,
    merge_image_meta_lists,
    parse_linkedin_cdn_expires_at_unix,
    resolve_trusted_local_rel,
)

EXPIRED_CDN_FIXTURE_URL = (
    "https://media.licdn.com/dms/image/sync/v2/D4E27AQHREDACTED/articleshare-shrink_480/"
    "B4EZmREDACTED/0/1776442767212?e=1780668000&v=beta&t=REDACTED"
)


def test_parse_linkedin_cdn_expires_at_unix() -> None:
    ts = parse_linkedin_cdn_expires_at_unix(EXPIRED_CDN_FIXTURE_URL)
    assert ts == 1780668000
    assert cdn_url_is_expired(
        EXPIRED_CDN_FIXTURE_URL,
        now=datetime(2026, 10, 9, tzinfo=UTC),
    )


def test_cdn_url_identity_strips_query() -> None:
    a = "https://media.licdn.com/dms/image/v2/x/0?e=1&t=aaa"
    b = "https://media.licdn.com/dms/image/v2/x/0?e=2&t=bbb"
    assert cdn_url_identity(a) == cdn_url_identity(b)


def test_merge_image_meta_by_path_not_query() -> None:
    base = "https://media.licdn.com/dms/image/v2/example/feedshare-shrink_800/0"
    prev = [{"cdn_url": f"{base}?e=1&t=a", "local_path": "images/keep.jpg"}]
    incoming = [{"cdn_url": f"{base}?e=2&t=b"}]
    merged = merge_image_meta_lists(prev, incoming)
    assert len(merged) == 1
    assert merged[0]["local_path"] == "images/keep.jpg"
    assert "e=2" in merged[0]["cdn_url"]


def test_resolve_trusted_local_rel_rejects_traversal(tmp_path) -> None:
    content_root = tmp_path / "content"
    images = content_root / "images"
    images.mkdir(parents=True)
    assert (
        resolve_trusted_local_rel("images/../../../../../etc/hostname", content_root)
        is None
    )


def test_apply_sidecar_ignores_malicious_embed_in_new_body(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))
    content_root = tmp_path / "content"
    images = content_root / "images"
    images.mkdir(parents=True)
    (images / "good.jpg").write_bytes(b"ok")

    cdn = "https://media.licdn.com/dms/image/v2/example/0?e=2147483647&v=beta&t=x"

    def _fake_download(url: str) -> str | None:
        assert url == cdn
        return "images/good.jpg"

    body, meta = apply_post_image_sidecar(
        "Author text\n\n![](images/../../../../../etc/hostname)",
        [cdn],
        existing_body="",
        download=_fake_download,
    )
    assert "etc/hostname" not in body
    assert "images/good.jpg" in body
    assert meta[0]["local_path"] == "images/good.jpg"


def test_apply_sidecar_handles_all_cdn_urls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))
    base = "https://media.licdn.com/dms/image/v2"
    urls = [
        f"{base}/img1/0?e=2147483647&t=1",
        f"{base}/img2/0?e=2147483647&t=2",
        f"{base}/img3/0?e=2147483647&t=3",
    ]
    calls: list[str] = []

    def _fake_download(url: str) -> str | None:
        calls.append(url)
        idx = len(calls)
        images = tmp_path / "content" / "images"
        images.mkdir(parents=True, exist_ok=True)
        name = f"img{idx}.jpg"
        (images / name).write_bytes(b"x")
        return f"images/{name}"

    body, meta = apply_post_image_sidecar(
        "Post",
        urls,
        existing_body="",
        download=_fake_download,
    )
    assert len(calls) == 3
    assert len(meta) == 3
    assert body.count("![](images/") == 3


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


def test_api_fallback_preserves_local_embed_and_skips_v4_bump(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))
    from linkedin_api.content_store import load_content, load_metadata, save_content
    from linkedin_api.enrich_activities import _save_from_api_fallback
    from linkedin_api.enriched_record import EnrichedRecord
    from linkedin_api.enrich_activities import EnrichmentTelemetry

    images = tmp_path / "content" / "images"
    images.mkdir(parents=True)
    (images / "v3img.jpg").write_bytes(b"keep")
    save_content(
        "99",
        "CSV body " + ("x" * 50) + "\n\n![](images/v3img.jpg)",
        post_urn="urn:li:activity:99",
    )
    from linkedin_api.content_store import save_metadata

    save_metadata(
        "99",
        post_urn="urn:li:activity:99",
        enrichment_version=3,
        activities_ids=["old"],
    )

    rec = EnrichedRecord(
        post_urn="urn:li:activity:99",
        post_url="https://www.linkedin.com/feed/update/urn:li:activity:99",
        content="Replacement CSV text " + ("y" * 50),
        urls=[],
        interaction_type="post",
        reaction_type=None,
        comment_text="",
        post_id="99",
        activity_id="new-act",
        timestamp=1,
        created_at="",
    )
    telemetry = EnrichmentTelemetry()
    _save_from_api_fallback(
        rec,
        "99",
        "urn:li:activity:99",
        rec.post_url,
        None,
        telemetry=telemetry,
        reason="http_fail",
    )
    body = load_content("99", post_urn="urn:li:activity:99")
    assert body is not None
    assert "![](images/v3img.jpg)" in body
    meta = load_metadata("99", post_urn="urn:li:activity:99")
    assert meta is not None
    from linkedin_api.post_extraction import ENRICHMENT_VERSION

    assert meta.get("enrichment_version") == ENRICHMENT_VERSION
    assert (images / "v3img.jpg").exists()

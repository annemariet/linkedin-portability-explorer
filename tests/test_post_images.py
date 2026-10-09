"""Tests for CDN-only post image sidecars (no enrich-time download)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from linkedin_api.post_images import (
    EXPIRED_CDN_FIXTURE_URL,
    apply_post_image_sidecar,
    cdn_url_for_log,
    cdn_url_identity,
    cdn_url_is_expired,
    filter_post_image_urls,
    merge_image_meta_lists,
    parse_linkedin_cdn_expires_at_unix,
    resolve_trusted_local_rel,
)

_BASE = "https://media.licdn.com/dms/image/v2/example/feedshare-shrink_800/0"


def test_parse_linkedin_cdn_expires_at_unix() -> None:
    ts = parse_linkedin_cdn_expires_at_unix(EXPIRED_CDN_FIXTURE_URL)
    assert ts == 1780668000
    assert cdn_url_is_expired(
        EXPIRED_CDN_FIXTURE_URL,
        now=datetime(2026, 10, 9, tzinfo=UTC),
    )


def test_cdn_url_for_log_strips_query() -> None:
    url = f"{_BASE}?e=1&v=beta&t=SECRETTOKEN"
    assert "t=" not in cdn_url_for_log(url)
    assert "SECRET" not in cdn_url_for_log(url)


def test_merge_image_meta_by_identity_not_query() -> None:
    prev = [{"cdn_url": f"{_BASE}?e=1&t=a", "identity": cdn_url_identity(_BASE)}]
    incoming = [{"cdn_url": f"{_BASE}?e=2&t=b", "identity": cdn_url_identity(_BASE)}]
    merged = merge_image_meta_lists(prev, incoming)
    assert len(merged) == 1
    assert "e=2" in merged[0]["cdn_url"]


def test_resolve_trusted_local_rel_rejects_traversal(tmp_path) -> None:
    content_root = tmp_path / "content"
    (content_root / "images").mkdir(parents=True)
    assert (
        resolve_trusted_local_rel("images/../../../../../etc/hostname", content_root)
        is None
    )


def test_enrich_embeds_cdn_urls_without_download(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))
    from linkedin_api.post_extraction import (
        PostExtractionResult,
        save_extraction_to_store,
    )

    cdn = f"{_BASE}?e=2147483647&v=beta&t=signed"
    ext = PostExtractionResult(
        markdown_body="Post body.",
        html_meta={},
        urls=[],
        mentions=[],
        hashtags=[],
        image_urls=[cdn],
    )
    with patch("requests.get") as mock_get:
        save_extraction_to_store(
            post_id="1",
            post_urn="urn:li:activity:1",
            post_url="https://www.linkedin.com/feed/update/urn:li:activity:1",
            ext=ext,
            urls_from_api=[],
            activity_time_iso="2026-01-01T00:00:00Z",
            post_created="2026-01-01T00:00:00Z",
            activities_ids=["a"],
        )
        for call in mock_get.call_args_list:
            url = call.args[0] if call.args else call.kwargs.get("url", "")
            if "licdn.com" in str(url) and "/dms/image" in str(url):
                pytest.fail(f"unexpected image GET {url}")

    from linkedin_api.content_store import load_content, load_metadata

    body = load_content("1", post_urn="urn:li:activity:1")
    assert body is not None
    assert cdn in body
    assert "![](https://" in body
    meta = load_metadata("1", post_urn="urn:li:activity:1")
    assert meta is not None
    assert len(meta["images"]) == 1
    assert meta["images"][0]["identity"] == cdn_url_identity(cdn)
    assert meta["images"][0].get("local_path") in (None, "")


def test_multi_image_post_keeps_all_cdn_embeds(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))
    urls = [
        f"{_BASE}/img1/0?e=2147483647&t=1",
        f"{_BASE}/img2/0?e=2147483647&t=2",
        f"{_BASE}/img3/0?e=2147483647&t=3",
    ]
    body, meta = apply_post_image_sidecar(
        "text",
        urls,
        existing_body="",
    )
    assert body.count("![](https://") == 3
    assert len(meta) == 3


def test_resign_does_not_grow_meta_images() -> None:
    base = cdn_url_identity(_BASE)
    prev = [{"cdn_url": f"{_BASE}?e=1&t=a", "identity": base}]
    for t in ("b", "c", "d"):
        prev = merge_image_meta_lists(
            prev,
            [{"cdn_url": f"{_BASE}?e=2&t={t}", "identity": base}],
        )
    assert len(prev) == 1


def test_reenrich_preserves_existing_local_embed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))
    from linkedin_api.content_store import (
        load_content,
        load_metadata,
        save_content,
        save_metadata,
    )
    from linkedin_api.post_extraction import (
        PostExtractionResult,
        save_extraction_to_store,
    )

    images = tmp_path / "content" / "images"
    images.mkdir(parents=True)
    (images / "keep.jpg").write_bytes(b"local")
    save_content("2", "Old.\n\n![](images/keep.jpg)", post_urn="urn:li:activity:2")
    save_metadata(
        "2",
        post_urn="urn:li:activity:2",
        enrichment_version=3,
        images=[{"cdn_url": f"{_BASE}?e=1&t=old", "local_path": "images/keep.jpg"}],
    )
    fresh = f"{_BASE}?e=2147483647&t=new"
    ext = PostExtractionResult(
        markdown_body="New body.",
        html_meta={},
        urls=[],
        mentions=[],
        hashtags=[],
        image_urls=[fresh],
    )
    with patch("requests.get"):
        save_extraction_to_store(
            post_id="2",
            post_urn="urn:li:activity:2",
            post_url="https://www.linkedin.com/feed/update/urn:li:activity:2",
            ext=ext,
            urls_from_api=[],
            activity_time_iso="2026-01-01T00:00:00Z",
            post_created="2026-01-01T00:00:00Z",
            activities_ids=["a"],
        )
    body = load_content("2", post_urn="urn:li:activity:2")
    assert body is not None
    assert "![](images/keep.jpg)" in body
    assert fresh not in body
    meta = load_metadata("2", post_urn="urn:li:activity:2")
    assert meta["images"][0]["local_path"] == "images/keep.jpg"
    assert (images / "keep.jpg").read_bytes() == b"local"


def test_placeholder_skipped() -> None:
    placeholder = "https://static.licdn.com/scds/common/u/images/logos/linkedin/logo-in-win8-tile-80.png"
    real = f"{_BASE}?e=2147483647&t=x"
    assert filter_post_image_urls([placeholder, real]) == [real]


def test_apply_sidecar_strips_malicious_local_embed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))
    cdn = f"{_BASE}?e=2147483647&t=x"
    body, meta = apply_post_image_sidecar(
        "Look ![](images/../../etc/passwd) here",
        [cdn],
        existing_body="",
    )
    assert "passwd" not in body
    assert cdn in body
    assert len(meta) == 1

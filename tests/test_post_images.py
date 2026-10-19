"""Tests for CDN-only post image sidecars (no enrich-time download)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from linkedin_api.post_images import (
    apply_post_image_sidecar,
    cdn_url_identity,
    filter_post_image_urls,
    is_linkedin_generic_placeholder_image,
    is_linkedin_post_image_url,
    legacy_v3_local_rel_for_cdn_url,
    merge_image_meta_lists,
    parse_linkedin_cdn_expires_at_unix,
    resolve_trusted_local_rel,
    strip_linkedin_cdn_image_embeds,
)

EXPIRED_CDN_FIXTURE_URL = (
    "https://media.licdn.com/dms/image/v2/fixture/0?e=1780668000&v=beta&t=fixture"
)

_BASE = "https://media.licdn.com/dms/image/v2/example/feedshare-shrink_800/0"


def test_parse_linkedin_cdn_expires_at_unix() -> None:
    ts = parse_linkedin_cdn_expires_at_unix(EXPIRED_CDN_FIXTURE_URL)
    assert ts == 1780668000


def test_non_linkedin_identity_includes_query() -> None:
    a = "https://cdn.example.com/img?id=1"
    b = "https://cdn.example.com/img?id=2"
    assert cdn_url_identity(a) != cdn_url_identity(b)


def test_linkedin_identity_ignores_signed_query() -> None:
    u1 = f"{_BASE}?e=1&t=a"
    u2 = f"{_BASE}?e=2&t=b"
    assert cdn_url_identity(u1) == cdn_url_identity(u2)


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


def test_resource_urls_not_treated_as_post_images() -> None:
    github = "https://github.com/foo/bar"
    article = "https://example.com/article"
    real = f"{_BASE}?e=2147483647&t=x"
    assert filter_post_image_urls([github, article, real]) == [real]
    assert not is_linkedin_post_image_url(github)


def test_strip_preserves_non_licdn_https_embeds() -> None:
    other = "![](https://cdn.example.com/photo.png)"
    licdn = f"![]({_BASE}?e=1&t=x)"
    body = f"See {other} and {licdn}"
    out = strip_linkedin_cdn_image_embeds(body)
    assert "cdn.example.com/photo.png" in out
    assert "media.licdn.com" not in out


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


def test_legacy_v3_local_only_one_embed_per_identity(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))
    content_root = tmp_path / "content"
    images = content_root / "images"
    images.mkdir(parents=True)
    cdn1 = f"{_BASE}/a/0?e=2147483647&t=1"
    cdn2 = f"{_BASE}/b/0?e=2147483647&t=2"
    for cdn in (cdn1, cdn2):
        h = hashlib.sha256(cdn.encode()).hexdigest()[:24]
        (images / f"{h}.jpg").write_bytes(b"x")
    body, _meta = apply_post_image_sidecar(
        "Post",
        [cdn1, cdn2],
        existing_body="",
        existing_images=[{"cdn_url": cdn1}, {"cdn_url": cdn2}],
    )
    assert body.count("![](images/") == 2
    assert "media.licdn.com" not in body


def test_placeholder_and_profile_logo_skipped() -> None:
    placeholder = "https://static.licdn.com/scds/common/u/images/logos/linkedin/logo-in-win8-tile-80.png"
    logo = (
        "https://media.licdn.com/dms/image/v2/C4E0BAQG/company-logo_200_200/0"
        "?e=2147483647&t=x"
    )
    profile = (
        "https://media.licdn.com/dms/image/v2/D4D03AQG/profile-displayphoto-shrink_100_100/0"
        "?e=2147483647&t=x"
    )
    real = f"{_BASE}?e=2147483647&t=x"
    assert is_linkedin_generic_placeholder_image(logo)
    assert is_linkedin_generic_placeholder_image(profile)
    assert filter_post_image_urls([placeholder, logo, profile, real]) == [real]


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


def _v3_hash_rel(cdn_url: str) -> str:
    h = hashlib.sha256(cdn_url.encode()).hexdigest()[:24]
    return f"images/{h}.jpg"


def _assert_three_image_main_style_state(
    body: str,
    images_meta: list[dict],
    *,
    cdn1: str,
    cdn2: str,
    cdn3: str,
    local1_rel: str,
) -> None:
    assert local1_rel in body
    assert cdn2 in body
    assert cdn3 in body
    assert body.count("![](https://") == 2
    assert body.count("![](images/") == 1
    assert len(images_meta) == 3
    by_ident = {m["identity"]: m for m in images_meta}
    assert by_ident[cdn_url_identity(cdn1)].get("local_path") == local1_rel
    assert not by_ident[cdn_url_identity(cdn2)].get("local_path")
    assert not by_ident[cdn_url_identity(cdn3)].get("local_path")


def test_three_image_v3_only_first_local_survives_reenrich(
    tmp_path, monkeypatch
) -> None:
    """Main-style store: 3 CDN images in meta, only image 1 on disk (v3 hash name)."""
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))
    from linkedin_api.content_store import (
        load_content,
        load_metadata,
        save_content,
        save_metadata,
    )
    from linkedin_api.post_extraction import (
        ENRICHMENT_VERSION,
        PostExtractionResult,
        save_extraction_to_store,
    )

    cdn1 = f"{_BASE}/photo-one/0?e=100&t=a"
    cdn2 = f"{_BASE}/photo-two/0?e=100&t=b"
    cdn3 = f"{_BASE}/photo-three/0?e=100&t=c"
    local1 = _v3_hash_rel(cdn1)
    images = tmp_path / "content" / "images"
    images.mkdir(parents=True)
    (images / Path(local1).name).write_bytes(b"img1-bytes")
    save_content(
        "77",
        f"Post text.\n\n![]({local1})",
        post_urn="urn:li:activity:77",
    )
    save_metadata(
        "77",
        post_urn="urn:li:activity:77",
        enrichment_version=ENRICHMENT_VERSION,
        images=[{"cdn_url": cdn1}, {"cdn_url": cdn2}, {"cdn_url": cdn3}],
    )
    ext = PostExtractionResult(
        markdown_body="Updated post text from HTML.",
        html_meta={},
        urls=[],
        mentions=[],
        hashtags=[],
        image_urls=[cdn1, cdn2, cdn3],
    )

    def run_enrich() -> None:
        with patch("requests.get"):
            save_extraction_to_store(
                post_id="77",
                post_urn="urn:li:activity:77",
                post_url="https://www.linkedin.com/feed/update/urn:li:activity:77",
                ext=ext,
                urls_from_api=[],
                activity_time_iso="2026-01-01T00:00:00Z",
                post_created="2026-01-01T00:00:00Z",
                activities_ids=["a"],
            )

    run_enrich()
    body1 = load_content("77", post_urn="urn:li:activity:77")
    meta1 = load_metadata("77", post_urn="urn:li:activity:77")["images"]
    assert body1 is not None
    _assert_three_image_main_style_state(
        body1, meta1, cdn1=cdn1, cdn2=cdn2, cdn3=cdn3, local1_rel=local1
    )
    run_enrich()
    body2 = load_content("77", post_urn="urn:li:activity:77")
    meta2 = load_metadata("77", post_urn="urn:li:activity:77")["images"]
    assert body2 == body1
    _assert_three_image_main_style_state(
        body2, meta2, cdn1=cdn1, cdn2=cdn2, cdn3=cdn3, local1_rel=local1
    )


def test_legacy_v3_rel_maps_full_cdn_url(tmp_path) -> None:
    content_root = tmp_path / "content"
    images = content_root / "images"
    images.mkdir(parents=True)
    cdn = f"{_BASE}?e=1&t=old"
    h = hashlib.sha256(cdn.encode()).hexdigest()[:24]
    (images / f"{h}.jpg").write_bytes(b"data")
    rel = legacy_v3_local_rel_for_cdn_url(cdn, content_root)
    assert rel == f"images/{h}.jpg"

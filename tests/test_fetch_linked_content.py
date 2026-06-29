"""Tests for fetch_linked_content module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from linkedin_api.content_store import load_metadata, save_content, save_metadata
from linkedin_api.fetch_linked_content import (
    FetchResult,
    _iter_posts_with_urls,
    fetch_linked_content,
    has_resource,
    load_resource,
    process_post_linked_content,
    save_resource,
)


@pytest.fixture(autouse=True)
def use_tmp_data_dir(monkeypatch, tmp_path):
    """Redirect data dir so tests don't touch ~/.linkedin_api."""
    monkeypatch.setenv("LINKEDIN_DATA_DIR", str(tmp_path))


def _mock_get_response(html: str, status_code: int = 200) -> MagicMock:
    """Build a requests.get mock that supports iter_content (streaming)."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.encoding = "utf-8"
    resp.text = html  # kept for tests that still read .text directly
    raw = html.encode("utf-8")
    resp.iter_content.return_value = iter([raw])
    return resp


# ---------------------------------------------------------------------------
# FetchResult
# ---------------------------------------------------------------------------


class TestFetchResult:
    def test_ok_with_content(self):
        r = FetchResult(url="https://example.com", content="body text", title="Title")
        assert r.ok is True

    def test_ok_with_title_only(self):
        r = FetchResult(url="https://example.com", title="Title only", content="")
        assert r.ok is True

    def test_not_ok_with_error(self):
        r = FetchResult(url="https://example.com", content="body", error="HTTP 403")
        assert r.ok is False

    def test_not_ok_empty(self):
        r = FetchResult(url="https://example.com")
        assert r.ok is False


# ---------------------------------------------------------------------------
# fetch_linked_content — unit tests (HTTP mocked)
# ---------------------------------------------------------------------------


class TestFetchLinkedContent:
    def test_ignores_linkedin_profile_urls(self):
        result = fetch_linked_content(
            "https://www.linkedin.com/in/johndoe", resolve_redirects=False
        )
        assert result.error == "ignored"
        assert result.ok is False

    def test_ignores_linkedin_hashtag_urls(self):
        result = fetch_linked_content(
            "https://www.linkedin.com/feed/hashtag/ai", resolve_redirects=False
        )
        assert result.error == "ignored"

    def test_ignores_resolved_mailto_target(self):
        with patch(
            "linkedin_api.fetch_linked_content.resolve_redirect",
            return_value="mailto:jobs@example.com",
        ):
            result = fetch_linked_content(
                "https://www.linkedin.com/redir/redirect?url=mailto%3Ajobs%40example.com",
                resolve_redirects=True,
            )
        assert result.error == "ignored"

    def test_skips_image_url(self):
        """Image URLs (detected by extension) should be skipped without an HTTP fetch."""
        result = fetch_linked_content(
            "https://example.com/photo.png", resolve_redirects=False
        )
        assert "skipped" in result.error
        assert result.ok is False

    def test_skips_pdf_document(self):
        """PDF URLs should be skipped without an HTTP fetch."""
        result = fetch_linked_content(
            "https://example.com/report.pdf", resolve_redirects=False
        )
        assert "skipped" in result.error
        assert result.ok is False

    def test_arxiv_pdf_fetches_html_page(self):
        html = (
            "<html><head>"
            '<meta property="og:title" content="Agentic Auto-Scheduling"/>'
            "</head><body><p>" + ("Full paper paragraph. " * 40) + "</p></body></html>"
        )
        with patch("requests.get", return_value=_mock_get_response(html)) as mock_get:
            result = fetch_linked_content(
                "https://arxiv.org/pdf/2511.00592", resolve_redirects=False
            )

        assert result.ok is True
        assert result.title == "Agentic Auto-Scheduling"
        assert "Full paper paragraph." in result.content
        assert mock_get.call_args[0][0] == "https://arxiv.org/html/2511.00592"

    def test_arxiv_pdf_falls_back_to_abs_when_html_sparse(self):
        sparse = "<html><body>HTML not available for this paper.</body></html>"
        abs_html = (
            "<html><head>"
            '<meta property="og:title" content="From Abs"/>'
            "</head><body><p>Abstract with enough text for fallback.</p></body></html>"
        )

        def side_effect(url, **kwargs):
            if "/html/" in url:
                return _mock_get_response(sparse)
            return _mock_get_response(abs_html)

        with patch("requests.get", side_effect=side_effect):
            result = fetch_linked_content(
                "https://arxiv.org/pdf/2511.00592", resolve_redirects=False
            )

        assert result.ok is True
        assert result.title == "From Abs"
        assert "Abstract with enough text" in result.content

    def test_rejects_binary_pdf_body(self):
        with patch(
            "requests.get",
            return_value=_mock_get_response("%PDF-1.7 binary"),
        ):
            result = fetch_linked_content(
                "https://example.com/not-really-html", resolve_redirects=False
            )
        assert not result.ok
        assert result.error

    def test_successful_html_fetch(self):
        html = (
            "<html><head>"
            '<meta property="og:title" content="Great Article"/>'
            "</head><body><p>Hello world</p></body></html>"
        )
        with patch("requests.get", return_value=_mock_get_response(html)):
            result = fetch_linked_content(
                "https://medium.com/some-article", resolve_redirects=False
            )

        assert result.ok is True
        assert result.title == "Great Article"
        assert "Hello world" in result.content
        assert result.url_type == "article"

    def test_server_error_returns_error_result(self):
        with patch(
            "requests.get", return_value=_mock_get_response("", status_code=500)
        ):
            result = fetch_linked_content(
                "https://example.com/broken", resolve_redirects=False
            )

        assert result.ok is False
        assert "500" in result.error

    def test_4xx_with_html_is_parsed(self):
        """4xx responses (e.g. GitHub 406) still contain useful HTML — parse them."""
        html = (
            "<html><head>"
            '<meta property="og:title" content="Repo Title"/>'
            "</head><body></body></html>"
        )
        with patch(
            "requests.get", return_value=_mock_get_response(html, status_code=406)
        ):
            result = fetch_linked_content(
                "https://github.com/org/repo", resolve_redirects=False
            )

        assert result.ok is True
        assert result.title == "Repo Title"

    def test_network_exception_returns_error_result(self):
        with patch("requests.get", side_effect=ConnectionError("timeout")):
            result = fetch_linked_content(
                "https://example.com/timeout", resolve_redirects=False
            )

        assert result.ok is False
        assert result.error  # non-empty error message

    def test_metadata_only_strategy_for_video(self):
        html = (
            "<html><head>"
            '<meta property="og:title" content="My Video"/>'
            "</head><body><p>lots of content</p></body></html>"
        )
        with patch("requests.get", return_value=_mock_get_response(html)):
            result = fetch_linked_content(
                "https://www.youtube.com/watch?v=abc123", resolve_redirects=False
            )

        assert result.url_type == "video"
        assert result.title == "My Video"
        assert result.content == ""  # metadata-only: no body


# ---------------------------------------------------------------------------
# Short URL resolution → classification
# ---------------------------------------------------------------------------


class TestShortUrlResolution:
    """Verify that URL type classification uses the *resolved* URL, not the
    raw lnkd.in short URL (which has no classifiable domain)."""

    def _mock_html(self, title: str = "Title", body: str = "Body") -> str:
        return (
            f"<html><head>"
            f'<meta property="og:title" content="{title}"/>'
            f"</head><body><p>{body}</p></body></html>"
        )

    def test_lnkd_in_to_github_classified_as_repository(self):
        """lnkd.in → github.com must be dispatched as 'repository'."""
        with (
            patch(
                "linkedin_api.fetch_linked_content.resolve_redirect",
                return_value="https://github.com/user/repo",
            ),
            patch(
                "requests.get",
                return_value=_mock_get_response(self._mock_html("Some Repo")),
            ),
        ):
            result = fetch_linked_content(
                "https://lnkd.in/erbBvi7E", resolve_redirects=True
            )

        assert result.url_type == "repository"
        assert result.resolved_url == "https://github.com/user/repo"
        # metadata-only strategy → no body content
        assert result.content == ""
        assert result.title == "Some Repo"

    def test_lnkd_in_to_youtube_classified_as_video(self):
        """lnkd.in → youtube.com must be dispatched as 'video' (metadata-only)."""
        with (
            patch(
                "linkedin_api.fetch_linked_content.resolve_redirect",
                return_value="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            ),
            patch(
                "requests.get",
                return_value=_mock_get_response(self._mock_html("My Video")),
            ),
        ):
            result = fetch_linked_content(
                "https://lnkd.in/erbBvi7E", resolve_redirects=True
            )

        assert result.url_type == "video"
        assert result.content == ""  # metadata-only
        assert result.title == "My Video"

    def test_lnkd_in_to_medium_article_classified_as_article(self):
        """lnkd.in → medium.com must be classified as 'article' and body-fetched."""
        with (
            patch(
                "linkedin_api.fetch_linked_content.resolve_redirect",
                return_value="https://medium.com/@author/some-article-abc123",
            ),
            patch(
                "requests.get",
                return_value=_mock_get_response(
                    self._mock_html("Great Article", "Article body here")
                ),
            ),
        ):
            result = fetch_linked_content(
                "https://lnkd.in/erbBvi7E", resolve_redirects=True
            )

        assert result.url_type == "article"
        assert "Article body here" in result.content

    def test_lnkd_in_to_image_is_skipped(self):
        """lnkd.in resolving to an image URL should be skipped without fetching."""
        with (
            patch(
                "linkedin_api.fetch_linked_content.resolve_redirect",
                return_value="https://example.com/diagram.png",
            ),
            patch("requests.get"),
        ):
            result = fetch_linked_content(
                "https://lnkd.in/erbBvi7E", resolve_redirects=True
            )

        assert "skipped" in result.error
        assert result.ok is False

    def test_failed_redirect_falls_back_gracefully(self):
        """If resolve_redirect returns the original lnkd.in URL (resolution
        failed), fetch_linked_content should still attempt a fetch rather
        than crash — lnkd.in has no special type so it defaults to 'article'."""
        with (
            patch(
                "linkedin_api.fetch_linked_content.resolve_redirect",
                return_value="https://lnkd.in/erbBvi7E",  # unchanged = failed
            ),
            patch("requests.get") as mock_get,
        ):
            mock_get.return_value = _mock_get_response("<html></html>")
            result = fetch_linked_content(
                "https://lnkd.in/erbBvi7E", resolve_redirects=True
            )

        # Should attempt fetch (not crash), resolved_url stays at lnkd.in
        assert result.resolved_url == "https://lnkd.in/erbBvi7E"
        assert result.url_type == "article"  # default for unknown domain

    def test_resolved_url_stored_in_result(self):
        """result.resolved_url must reflect the final URL, not the short one."""
        final = "https://arxiv.org/abs/2401.00000"
        with (
            patch(
                "linkedin_api.fetch_linked_content.resolve_redirect",
                return_value=final,
            ),
            patch("requests.get") as mock_get,
        ):
            mock_get.return_value = MagicMock(
                status_code=200, text=self._mock_html("Paper")
            )
            result = fetch_linked_content(
                "https://lnkd.in/erbBvi7E", resolve_redirects=True
            )

        assert result.url == "https://lnkd.in/erbBvi7E"  # original preserved
        assert result.resolved_url == final

    @pytest.mark.integration
    def test_real_lnkd_in_resolves_and_classifies(self):
        """Live: https://lnkd.in/erbBvi7E resolves via LinkedIn interstitial page
        to presse.economie.gouv.fr and is classified + fetched as an article."""
        result = fetch_linked_content(
            "https://lnkd.in/erbBvi7E", resolve_redirects=True
        )
        assert result.url == "https://lnkd.in/erbBvi7E"
        assert "presse.economie.gouv.fr" in result.resolved_url
        assert result.url_type == "article"
        assert result.ok


# ---------------------------------------------------------------------------
# Resource store
# ---------------------------------------------------------------------------


class TestResourceStore:
    def test_not_stored_initially(self):
        assert has_resource("https://example.com/new") is False

    def test_save_and_has_resource(self):
        url = "https://example.com/article"
        result = FetchResult(
            url=url,
            resolved_url=url,
            title="Test",
            content="Body text",
            url_type="article",
        )
        save_resource(url, result)
        assert has_resource(url) is True

    def test_save_and_load_roundtrip(self):
        url = "https://example.com/roundtrip"
        result = FetchResult(
            url=url,
            resolved_url=url,
            title="My Title",
            content="My Content",
            url_type="article",
            domain="example.com",
        )
        save_resource(url, result)
        loaded = load_resource(url)
        assert loaded is not None
        assert loaded.title == "My Title"
        assert loaded.content == "My Content"
        assert loaded.domain == "example.com"

    def test_load_missing_returns_none(self):
        assert load_resource("https://example.com/missing") is None

    def test_load_resource_finds_fragment_url_via_canonical_key(self, tmp_path):
        url = "https://example.com/article#section"
        save_resource(
            url,
            FetchResult(
                url=url,
                resolved_url=url,
                title="Title",
                content="Body",
            ),
        )
        loaded = load_resource("https://example.com/article")
        assert loaded is not None
        assert loaded.content == "Body"

    def test_refresh_resource_if_corrupt_refetches(self, tmp_path):
        from linkedin_api.fetch_linked_content import refresh_resource_if_corrupt

        url = "https://example.com/article"
        broken = "OpenAI wonâ\x80\x99t".encode("utf-8").decode("latin-1")
        save_resource(
            url,
            FetchResult(url=url, resolved_url=url, title=broken, content=broken),
        )
        fresh_html = (
            "<html><head><title>Fixed Title</title></head>"
            "<body><p>Fixed body with enough text for a valid article fetch.</p></body></html>"
        )
        with patch(
            "requests.get",
            return_value=_mock_get_response(fresh_html),
        ):
            refreshed = refresh_resource_if_corrupt(url)
        assert refreshed is not None
        assert refreshed.title == "Fixed Title"
        assert "Fixed body" in refreshed.content
        assert "â" not in refreshed.content

    def test_fetch_x_status_uses_fxtwitter_api(self, tmp_path):
        payload = {
            "tweet": {
                "text": "",
                "author": {"name": "Akshay 🚀", "screen_name": "akshay_pachaar"},
                "article": {
                    "title": "Loop Engineering Clearly Explained",
                    "content": {
                        "blocks": [
                            {
                                "type": "unstyled",
                                "text": "Stop prompting your agents.",
                                "inlineStyleRanges": [],
                            }
                        ],
                        "entityMap": [],
                    },
                },
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = payload

        with patch("requests.get", return_value=mock_resp):
            result = fetch_linked_content(
                "https://x.com/akshay_pachaar/status/2069118430582866051",
                resolve_redirects=False,
            )

        assert result.ok is True
        assert result.title == "Loop Engineering Clearly Explained"
        assert "Stop prompting your agents." in result.content

    def test_save_writes_md_file(self, tmp_path):
        url = "https://example.com/md-test"
        result = FetchResult(url=url, content="markdown content", title="T")
        json_path = save_resource(url, result)
        md_path = json_path.with_suffix(".md")
        assert md_path.exists()
        assert md_path.read_text() == "markdown content"

    def test_different_urls_different_files(self, tmp_path):
        url_a = "https://example.com/a"
        url_b = "https://example.com/b"
        res_a = FetchResult(url=url_a, content="A")
        res_b = FetchResult(url=url_b, content="B")
        path_a = save_resource(url_a, res_a)
        path_b = save_resource(url_b, res_b)
        assert path_a != path_b


# ---------------------------------------------------------------------------
# process_post_linked_content
# ---------------------------------------------------------------------------


class TestProcessPostLinkedContent:
    def test_skips_ignored_urls(self):
        urls = ["https://www.linkedin.com/in/user"]
        results = process_post_linked_content(urls)
        assert len(results) == 1
        assert results[0].error == "ignored"

    def test_uses_cache_when_skip_cached(self):
        url = "https://example.com/cached"
        cached = FetchResult(
            url=url, content="cached body", title="Cached", fetched_at="2024-01-01"
        )
        save_resource(url, cached)

        with patch("linkedin_api.fetch_linked_content.fetch_linked_content"):
            results = process_post_linked_content([url], skip_cached=True)

        assert results[0].title == "Cached"

    def test_refetches_when_not_skip_cached(self):
        url = "https://example.com/refetch"
        # Pre-store a result
        save_resource(url, FetchResult(url=url, content="old", title="Old"))

        fresh = FetchResult(url=url, content="fresh body", title="Fresh")
        with patch(
            "linkedin_api.fetch_linked_content.fetch_linked_content",
            return_value=fresh,
        ):
            results = process_post_linked_content([url], skip_cached=False)

        assert results[0].title == "Fresh"

    def test_failed_fetch_not_stored(self):
        url = "https://example.com/fail"
        fail = FetchResult(url=url, error="HTTP 500")
        with patch(
            "linkedin_api.fetch_linked_content.fetch_linked_content",
            return_value=fail,
        ):
            process_post_linked_content([url], skip_cached=False)

        assert not has_resource(url)

    def test_empty_list_returns_empty(self):
        assert process_post_linked_content([]) == []


# ---------------------------------------------------------------------------
# _iter_posts_with_urls — URL discovery from metadata and .md fallback
# ---------------------------------------------------------------------------


class TestIterPostsWithUrls:
    URN = "urn:li:activity:123"

    def test_yields_urls_from_metadata(self):
        """Posts with urls already in .meta.json are yielded directly."""
        save_content(self.URN, "some text")
        save_metadata(self.URN, urls=["https://example.com/article"])

        results = list(_iter_posts_with_urls())

        assert len(results) == 1
        _, urls = results[0]
        assert urls == ["https://example.com/article"]

    def test_yields_mention_urls_with_resource_urls(self):
        """LinkedIn mention URLs are filtered; only external resources are fetched."""
        save_content(self.URN, "x")
        save_metadata(
            self.URN,
            urls=["https://example.com/resource"],
            mentions=[
                {"name": "Acme", "url": "https://www.linkedin.com/company/acme"},
            ],
        )

        results = list(_iter_posts_with_urls())
        _, urls = results[0]
        assert urls == ["https://example.com/resource"]

    def test_extracts_urls_from_md_when_metadata_urls_empty(self):
        """When urls field is absent, URLs are extracted from the .md content."""
        save_content(self.URN, "Check out https://github.com/user/repo for details.")
        save_metadata(self.URN)  # no urls

        results = list(_iter_posts_with_urls())

        assert len(results) == 1
        _, urls = results[0]
        assert "https://github.com/user/repo" in urls

    def test_persists_extracted_urls_to_metadata(self):
        """URLs extracted from .md are written back to .meta.json for future runs."""
        save_content(self.URN, "See https://arxiv.org/abs/2401.00000 for the paper.")
        save_metadata(self.URN)

        list(_iter_posts_with_urls())

        meta = load_metadata(self.URN)
        assert meta is not None
        assert "https://arxiv.org/abs/2401.00000" in meta.get("urls", [])

    def test_filters_linkedin_urls_from_md_extraction(self):
        """LinkedIn profile/hashtag URLs in .md content are excluded."""
        save_content(
            self.URN,
            "Follow https://www.linkedin.com/in/johndoe and visit https://example.com/good",
        )
        save_metadata(self.URN)

        results = list(_iter_posts_with_urls())

        _, urls = results[0]
        assert all("linkedin.com/in/" not in u for u in urls)
        assert "https://example.com/good" in urls

    def test_skips_posts_with_no_urls_anywhere(self):
        """Posts with no urls in metadata and no URLs in .md are not yielded."""
        save_content(self.URN, "Just some plain text with no links.")
        save_metadata(self.URN)

        assert list(_iter_posts_with_urls()) == []


# ---------------------------------------------------------------------------
# Cloudflare challenge detection
# ---------------------------------------------------------------------------


class TestCloudflareDetection:
    def _cf_html(
        self,
        title: str = "Just a moment...",
        body: str = "Enable JavaScript and cookies to continue",
    ) -> str:
        return (
            f"<html><head><title>{title}</title></head>"
            f"<body><p>{body}</p></body></html>"
        )

    def test_cloudflare_title_marker_detected(self):
        with patch("requests.get", return_value=_mock_get_response(self._cf_html())):
            result = fetch_linked_content(
                "https://medium.com/@author/article", resolve_redirects=False
            )
        assert result.error == "cloudflare challenge"
        assert result.ok is False
        assert result.title == ""
        assert result.content == ""

    def test_cloudflare_body_marker_detected(self):
        html = (
            "<html><head><title>Some other title</title></head>"
            "<body><p>Enable JavaScript and cookies to continue</p></body></html>"
        )
        with patch("requests.get", return_value=_mock_get_response(html)):
            result = fetch_linked_content(
                "https://medium.com/@author/article", resolve_redirects=False
            )
        assert result.error == "cloudflare challenge"

    def test_non_cloudflare_page_not_detected(self):
        html = (
            "<html><head>"
            '<meta property="og:title" content="Real Article"/>'
            "</head><body><p>Actual content here.</p></body></html>"
        )
        with patch("requests.get", return_value=_mock_get_response(html)):
            result = fetch_linked_content(
                "https://medium.com/@author/real", resolve_redirects=False
            )
        assert result.ok is True
        assert result.error == ""

    def test_cloudflare_result_not_saved(self):
        url = "https://medium.com/@author/cf-blocked"
        with patch("requests.get", return_value=_mock_get_response(self._cf_html())):
            results = process_post_linked_content([url], skip_cached=False)
        assert not has_resource(url)
        assert results[0].error == "cloudflare challenge"


# ---------------------------------------------------------------------------
# cited_by URN normalization
# ---------------------------------------------------------------------------


class TestCitedByUrnNormalization:
    def test_save_resource_normalizes_legacy_urn_in_cited_by(self, tmp_path):
        """Raw URN entries written before the hash-conversion fix are normalized."""
        import hashlib
        import json

        from linkedin_api.fetch_linked_content import _resource_dir, _url_stem

        url = "https://example.com/legacy-urn"
        urn = "urn:li:activity:9999999999999"
        expected_hash = hashlib.sha256(urn.encode()).hexdigest()

        # Simulate a file written with a raw URN in cited_by
        stem = _url_stem(url)
        rdir = _resource_dir()
        legacy_data = {
            "url": url,
            "resolved_url": url,
            "title": "T",
            "content": "C",
            "url_type": "article",
            "domain": "example.com",
            "error": "",
            "fetched_at": "2024-01-01",
            "cited_by": [urn],  # old raw-URN format
        }
        (rdir / f"{stem}.json").write_text(json.dumps(legacy_data), encoding="utf-8")

        # Re-save: the URN should be converted to a hash
        result = FetchResult(
            url=url, resolved_url=url, title="T", content="C", url_type="article"
        )
        save_resource(url, result, citing_post_urns=[])

        saved = json.loads((rdir / f"{stem}.json").read_text(encoding="utf-8"))
        assert urn not in saved["cited_by"]
        assert expected_hash in saved["cited_by"]

    def test_update_resource_cited_by_normalizes_legacy_urn(self, tmp_path):
        """_update_resource_cited_by also converts existing URN entries to hashes."""
        import hashlib
        import json

        from linkedin_api.fetch_linked_content import (
            _resource_dir,
            _update_resource_cited_by,
            _url_stem,
        )

        url = "https://example.com/legacy-urn-update"
        old_urn = "urn:li:activity:1111111111111"
        new_urn = "urn:li:activity:2222222222222"
        old_hash = hashlib.sha256(old_urn.encode()).hexdigest()
        new_hash = hashlib.sha256(new_urn.encode()).hexdigest()

        stem = _url_stem(url)
        rdir = _resource_dir()
        (rdir / f"{stem}.json").write_text(
            json.dumps({"url": url, "cited_by": [old_urn]}), encoding="utf-8"
        )

        _update_resource_cited_by(url, [new_urn])

        saved = json.loads((rdir / f"{stem}.json").read_text(encoding="utf-8"))
        assert old_urn not in saved["cited_by"]
        assert old_hash in saved["cited_by"]
        assert new_hash in saved["cited_by"]

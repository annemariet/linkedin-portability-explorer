"""Tests for fetch_linked_content module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from linkedin_api.content_store import load_metadata, save_content, save_metadata
from linkedin_api.fetch_linked_content import (
    FetchResult,
    _extractor_backend,
    _fetch_tavily,
    _iter_posts_with_urls,
    _strategy_for,
    _tavily_api_key,
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

    def test_images_stored_as_remote_urls_not_downloaded(self):
        """result.images is persisted as-is (remote URLs) — not downloaded or
        embedded in the .md; see module docstring for why."""
        url = "https://example.com/with-images"
        result = FetchResult(
            url=url,
            content="Body text",
            images=["https://cdn.example.com/a.jpg"],
        )
        json_path = save_resource(url, result)

        md_text = json_path.with_suffix(".md").read_text()
        assert md_text == "Body text"

        stored = json.loads(json_path.read_text())
        assert stored["images"] == ["https://cdn.example.com/a.jpg"]


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
        """``mentions[].url`` are included for fetch alongside ``urls``."""
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
        assert "https://example.com/resource" in urls
        assert "https://www.linkedin.com/company/acme" in urls

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
# TAVILY_API_KEY resolution — own convention vs shared lucys-foundry keychain
# ---------------------------------------------------------------------------


class TestTavilyApiKeyResolution:
    """This repo's own keyring convention (service=TAVILY_API_KEY, account=
    LINKEDIN_ACCOUNT) predates and doesn't match lucys-foundry's shared
    keychain (service="lucys-foundry", account="tavily", written by
    ``manage_keys.py set tavily``). Unifying them is tracked as amai-lab ADR
    0001 §4 / LUC-96 — until then, ``_tavily_api_key`` checks both."""

    def test_uses_own_keyring_convention_first(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)

        def fake_get_password(service, account):
            return "own-key" if service == "TAVILY_API_KEY" else None

        with patch("keyring.get_password", side_effect=fake_get_password):
            assert _tavily_api_key() == "own-key"

    def test_falls_back_to_shared_lucys_foundry_keychain(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)

        def fake_get_password(service, account):
            if service == "lucys-foundry" and account == "tavily":
                return "shared-key"
            return None

        with patch("keyring.get_password", side_effect=fake_get_password):
            assert _tavily_api_key() == "shared-key"

    def test_falls_back_to_legacy_agent_fleet_rts(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)

        def fake_get_password(service, account):
            if service == "agent-fleet-rts" and account == "tavily":
                return "legacy-key"
            return None

        with patch("keyring.get_password", side_effect=fake_get_password):
            assert _tavily_api_key() == "legacy-key"

    def test_falls_back_to_env_var_when_no_keyring_hit(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "env-key")
        with patch("keyring.get_password", return_value=None):
            assert _tavily_api_key() == "env-key"

    def test_empty_when_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        with patch("keyring.get_password", return_value=None):
            assert _tavily_api_key() == ""


# ---------------------------------------------------------------------------
# LINKEDIN_EXTRACTOR backend selection
# ---------------------------------------------------------------------------


class TestExtractorBackend:
    def test_defaults_to_httpx(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_EXTRACTOR", raising=False)
        assert _extractor_backend() == "httpx"

    def test_selects_tavily_when_key_present(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_EXTRACTOR", "tavily")
        with patch(
            "linkedin_api.fetch_linked_content._tavily_api_key",
            return_value="fake-key",
        ):
            assert _extractor_backend() == "tavily"

    def test_falls_back_to_httpx_without_key(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_EXTRACTOR", "tavily")
        with patch(
            "linkedin_api.fetch_linked_content._tavily_api_key", return_value=""
        ):
            assert _extractor_backend() == "httpx"

    def test_unknown_value_falls_back_to_httpx(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_EXTRACTOR", "bogus")
        assert _extractor_backend() == "httpx"

    def test_metadata_only_types_ignore_extractor(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_EXTRACTOR", "tavily")
        with patch(
            "linkedin_api.fetch_linked_content._tavily_api_key",
            return_value="fake-key",
        ):
            from linkedin_api.fetch_linked_content import _fetch_metadata_only

            assert _strategy_for("video") is _fetch_metadata_only
            assert _strategy_for("repository") is _fetch_metadata_only

    def test_article_type_uses_selected_backend(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_EXTRACTOR", raising=False)
        from linkedin_api.fetch_linked_content import _fetch_html_body

        assert _strategy_for("article") is _fetch_html_body

        monkeypatch.setenv("LINKEDIN_EXTRACTOR", "tavily")
        with patch(
            "linkedin_api.fetch_linked_content._tavily_api_key",
            return_value="fake-key",
        ):
            assert _strategy_for("article") is _fetch_tavily


# ---------------------------------------------------------------------------
# _fetch_tavily
# ---------------------------------------------------------------------------


class TestFetchTavily:
    def test_raises_without_api_key(self):
        with patch(
            "linkedin_api.fetch_linked_content._tavily_api_key", return_value=""
        ):
            with pytest.raises(ValueError, match="TAVILY_API_KEY"):
                _fetch_tavily("https://example.com/article")

    def test_success_extracts_title_and_content(self):
        mock_client = MagicMock()
        mock_client.extract.return_value = {
            "results": [
                {
                    "url": "https://example.com/article",
                    "raw_content": "# Great Article\n\nBody text here.",
                }
            ],
            "failed_results": [],
        }
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            title, content, images = _fetch_tavily("https://example.com/article")

        assert title == "Great Article"
        assert "Body text here." in content
        assert images == []
        mock_client.extract.assert_called_once()
        _, kwargs = mock_client.extract.call_args
        assert kwargs["urls"] == ["https://example.com/article"]
        assert kwargs["extract_depth"] == "advanced"
        assert kwargs["format"] == "markdown"
        assert kwargs["include_images"] is True

    def test_basic_depth_via_env(self, monkeypatch):
        monkeypatch.setenv("TAVILY_EXTRACT_DEPTH", "basic")
        mock_client = MagicMock()
        mock_client.extract.return_value = {
            "results": [{"url": "u", "raw_content": "content"}],
            "failed_results": [],
        }
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            _fetch_tavily("https://example.com/article")

        _, kwargs = mock_client.extract.call_args
        assert kwargs["extract_depth"] == "basic"

    def test_failed_result_raises(self):
        mock_client = MagicMock()
        mock_client.extract.return_value = {
            "results": [],
            "failed_results": [{"url": "u", "error": "unsupported content type"}],
        }
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            with pytest.raises(ValueError, match="unsupported content type"):
                _fetch_tavily("https://example.com/broken")

    def test_no_results_raises(self):
        mock_client = MagicMock()
        mock_client.extract.return_value = {"results": [], "failed_results": []}
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            with pytest.raises(ValueError, match="no content extracted"):
                _fetch_tavily("https://example.com/empty")

    def test_prefers_title_field_over_markdown_derivation(self):
        """Tavily's response has a real ``title`` field — use it, don't derive
        one from the first markdown line (which may be unrelated boilerplate)."""
        mock_client = MagicMock()
        mock_client.extract.return_value = {
            "results": [
                {
                    "url": "https://example.com/article",
                    "title": "The Real Title",
                    "raw_content": "Some unrelated first line\n\nBody.",
                }
            ],
            "failed_results": [],
        }
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            title, _, _ = _fetch_tavily("https://example.com/article")

        assert title == "The Real Title"

    def test_strips_linkedin_guest_preamble(self):
        """Tavily's raw_content for a LinkedIn post URL includes the
        logged-out guest-view nav/sign-in chrome before the actual post —
        strip it, keeping from the '<Name>'s Post' heading onward."""
        mock_client = MagicMock()
        mock_client.extract.return_value = {
            "results": [
                {
                    "url": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                    "title": "Some Post",
                    "raw_content": (
                        "Agree & Join LinkedIn\n\n"
                        "By clicking Continue...\n\n"
                        "[Sign in](...)[Join now](...)\n\n"
                        "# Jane Doe’s Post\n\n"
                        "The actual post content here."
                    ),
                }
            ],
            "failed_results": [],
        }
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            _, content, _ = _fetch_tavily(
                "https://www.linkedin.com/feed/update/urn:li:activity:1/"
            )

        assert content.startswith("# Jane Doe’s Post")
        assert "Agree & Join LinkedIn" not in content
        assert "The actual post content here." in content

    def test_does_not_strip_preamble_for_non_linkedin_url(self):
        mock_client = MagicMock()
        content_with_marker = "Agree & Join LinkedIn\n\n# Someone’s Post\n\nBody."
        mock_client.extract.return_value = {
            "results": [
                {
                    "url": "https://example.com/article",
                    "raw_content": content_with_marker,
                }
            ],
            "failed_results": [],
        }
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            _, content, _ = _fetch_tavily("https://example.com/article")

        assert content == content_with_marker

    def test_does_not_strip_when_no_post_heading_found(self):
        """LinkedIn pages without a '<Name>'s Post' heading (e.g. articles)
        are left untouched rather than mangled by a false-positive strip."""
        mock_client = MagicMock()
        content = "Agree & Join LinkedIn\n\n# Some Article Title\n\nBody."
        mock_client.extract.return_value = {
            "results": [
                {
                    "url": "https://www.linkedin.com/pulse/some-article",
                    "raw_content": content,
                }
            ],
            "failed_results": [],
        }
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            _, result_content, _ = _fetch_tavily(
                "https://www.linkedin.com/pulse/some-article"
            )

        assert result_content == content

    def test_uses_api_images_field_when_present(self):
        mock_client = MagicMock()
        mock_client.extract.return_value = {
            "results": [
                {
                    "url": "https://example.com/article",
                    "raw_content": "# Title\n\nBody.",
                    "images": ["https://cdn.example.com/api-image.jpg"],
                }
            ],
            "failed_results": [],
        }
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            _, _, images = _fetch_tavily("https://example.com/article")

        assert images == ["https://cdn.example.com/api-image.jpg"]

    def test_no_images_field_returns_empty_list(self):
        """No markdown-scraping fallback — LinkedIn's own image refs in the
        markdown are unreliable (comment avatars, broken lazy-load
        placeholders), so an empty API field just means no images."""
        mock_client = MagicMock()
        mock_client.extract.return_value = {
            "results": [
                {
                    "url": "u",
                    "raw_content": "Text with a ![markdown image](https://cdn.example.com/x.jpg) in it.",
                    "images": [],
                }
            ],
            "failed_results": [],
        }
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            _, _, images = _fetch_tavily("https://example.com/article")

        assert images == []

    def test_strips_explore_categories_footer(self):
        """The guest-view footer (category nav, copyright, language picker,
        sign-in CTA) starts at '## Explore content categories' — drop it."""
        mock_client = MagicMock()
        mock_client.extract.return_value = {
            "results": [
                {
                    "url": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                    "raw_content": (
                        "Agree & Join LinkedIn\n\n"
                        "# Jane Doe’s Post\n\n"
                        "The actual post content.\n\n"
                        "## Explore content categories\n\n"
                        "*   Career\n*   Productivity\n\n"
                        "LinkedIn© 2026\n"
                        "## Sign in to view more content"
                    ),
                }
            ],
            "failed_results": [],
        }
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            _, content, _ = _fetch_tavily(
                "https://www.linkedin.com/feed/update/urn:li:activity:1/"
            )

        assert "The actual post content." in content
        assert "Explore content categories" not in content
        assert "Career" not in content
        assert "Sign in to view more content" not in content

    def test_strips_engagement_chrome_lines(self):
        """Like/Reply/Reaction button rows scattered through comments are
        pure UI chrome — drop them, but leave real content (including
        comments that are just a number, e.g. "guess the number" posts)."""
        mock_client = MagicMock()
        mock_client.extract.return_value = {
            "results": [
                {
                    "url": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                    "raw_content": (
                        "# Jane Doe’s Post\n\n"
                        "The post text.\n\n"
                        "[John Smith](url) 5mo\n\n"
                        "175\n\n"
                        "[Like](url)[Reply](url) 1 Reaction \n\n"
                        "[Alice](url) 3mo\n\n"
                        "Great point!\n\n"
                        "[Like](url)[Reply](url)[2 Reactions](url) 3 Reactions "
                    ),
                }
            ],
            "failed_results": [],
        }
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            _, content, _ = _fetch_tavily(
                "https://www.linkedin.com/feed/update/urn:li:activity:1/"
            )

        assert "175" in content  # a bare-number comment is real content
        assert "Great point!" in content
        assert "Like" not in content
        assert "Reply" not in content
        assert "Reaction" not in content

    def test_does_not_strip_chrome_lines_for_non_linkedin_url(self):
        mock_client = MagicMock()
        mock_client.extract.return_value = {
            "results": [
                {
                    "url": "https://example.com/article",
                    "raw_content": "# Title\n\n[Like](url)[Reply](url) 1 Reaction",
                }
            ],
            "failed_results": [],
        }
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            _, content, _ = _fetch_tavily("https://example.com/article")

        assert "[Like](url)[Reply](url) 1 Reaction" in content


class TestFetchLinkedContentViaTavily:
    def test_dispatches_to_tavily_end_to_end(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_EXTRACTOR", "tavily")
        mock_client = MagicMock()
        mock_client.extract.return_value = {
            "results": [
                {
                    "url": "https://example.com/article",
                    "raw_content": "# Piece\n\nReal body from Tavily.",
                }
            ],
            "failed_results": [],
        }
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            result = fetch_linked_content(
                "https://example.com/article", resolve_redirects=False
            )

        assert result.ok is True
        assert result.title == "Piece"
        assert "Real body from Tavily." in result.content

    def test_tavily_failure_returns_error_result(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_EXTRACTOR", "tavily")
        mock_client = MagicMock()
        mock_client.extract.side_effect = RuntimeError("network down")
        with (
            patch(
                "linkedin_api.fetch_linked_content._tavily_api_key",
                return_value="fake-key",
            ),
            patch("tavily.TavilyClient", return_value=mock_client),
        ):
            result = fetch_linked_content(
                "https://example.com/article", resolve_redirects=False
            )

        assert result.ok is False
        assert "network down" in result.error


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

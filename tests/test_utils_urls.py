"""Tests for linkedin_api.utils.urls module."""

import pytest
from unittest.mock import MagicMock, patch

from linkedin_api.utils.urls import (
    categorize_url,
    extract_classified_links,
    extract_urls_from_text,
    is_linkedin_internal_url,
    is_linkedin_mention_url,
    linkedin_mention_type,
    resolve_redirect,
    should_ignore_url,
)


class TestExtractUrlsFromText:
    def test_basic_url(self):
        urls = extract_urls_from_text("Check out https://example.com/page")
        assert "https://example.com/page" in urls

    def test_multiple_urls(self):
        text = "See https://a.com and https://b.com for details"
        urls = extract_urls_from_text(text)
        assert len(urls) == 2

    def test_empty_text(self):
        assert extract_urls_from_text("") == []

    def test_no_urls(self):
        assert extract_urls_from_text("No URLs here") == []

    def test_dedup(self):
        text = "https://a.com and https://a.com again"
        urls = extract_urls_from_text(text)
        assert len(urls) == 1


class TestLinkedinSignupRedirectHashtag:
    def test_extracts_hashtag_from_signup_redirect(self):
        from linkedin_api.utils.urls import linkedin_signup_redirect_hashtag

        url = (
            "https://www.linkedin.com/signup/cold-join"
            "?session_redirect=https%3A%2F%2Fwww.linkedin.com%2Ffeed%2Fhashtag%2Fslasheo"
            "&trk=public_post-text"
        )
        assert linkedin_signup_redirect_hashtag(url) == "slasheo"

    def test_returns_none_for_non_signup_url(self):
        from linkedin_api.utils.urls import linkedin_signup_redirect_hashtag

        assert linkedin_signup_redirect_hashtag("https://example.com/foo") is None

    def test_returns_none_when_redirect_is_not_hashtag(self):
        from linkedin_api.utils.urls import linkedin_signup_redirect_hashtag

        url = (
            "https://www.linkedin.com/signup/cold-join"
            "?session_redirect=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fsomeone"
        )
        assert linkedin_signup_redirect_hashtag(url) is None


class TestLinkedinRedirUnwrapUrl:
    def test_extracts_lnkd_in_target(self):
        from linkedin_api.utils.urls import linkedin_redir_unwrap_url

        url = (
            "https://www.linkedin.com/redir/redirect"
            "?url=https%3A%2F%2Flnkd.in%2FeRgDaRJ8"
            "&urlhash=YcyT&trk=public_post-text"
        )
        assert linkedin_redir_unwrap_url(url) == "https://lnkd.in/eRgDaRJ8"

    def test_externalredirect_path_also_unwrapped(self):
        from linkedin_api.utils.urls import linkedin_redir_unwrap_url

        url = (
            "https://www.linkedin.com/redir/externalRedirect"
            "?url=https%3A%2F%2Fexample.com%2Farticle"
        )
        assert linkedin_redir_unwrap_url(url) == "https://example.com/article"

    def test_returns_none_for_non_redir_url(self):
        from linkedin_api.utils.urls import linkedin_redir_unwrap_url

        assert linkedin_redir_unwrap_url("https://example.com/foo") is None
        assert linkedin_redir_unwrap_url("https://www.linkedin.com/in/jane") is None

    def test_returns_none_when_url_param_missing(self):
        from linkedin_api.utils.urls import linkedin_redir_unwrap_url

        assert (
            linkedin_redir_unwrap_url(
                "https://www.linkedin.com/redir/redirect?urlhash=YcyT"
            )
            is None
        )


class TestLinkedinMentionType:
    def test_person(self):
        assert linkedin_mention_type("https://www.linkedin.com/in/jane") == "person"

    def test_company(self):
        assert (
            linkedin_mention_type("https://www.linkedin.com/company/acme") == "company"
        )

    def test_school(self):
        assert linkedin_mention_type("https://www.linkedin.com/school/mit") == "school"

    def test_non_mention_url_returns_empty(self):
        assert linkedin_mention_type("https://github.com/x/y") == ""

    def test_is_linkedin_mention_url_matches_type(self):
        assert is_linkedin_mention_url("https://www.linkedin.com/company/acme") is True
        assert is_linkedin_mention_url("https://github.com/x/y") is False


class TestExtractClassifiedLinks:
    def test_splits_mentions_tags_and_resource_urls(self):
        urls_in = [
            "https://www.linkedin.com/in/jane",
            "https://www.linkedin.com/feed/hashtag/ai?trk=x",
            "https://github.com/x/y",
        ]
        urls, mentions, tags = extract_classified_links(urls_in)
        assert tags == ["ai"]
        assert len(mentions) == 1
        assert mentions[0]["url"] == "https://www.linkedin.com/in/jane"
        assert mentions[0]["type"] == "person"
        assert "https://github.com/x/y" in urls

    def test_company_url_typed_as_company(self):
        urls, mentions, _ = extract_classified_links(
            ["https://www.linkedin.com/company/acme"]
        )
        assert mentions[0]["type"] == "company"

    def test_signup_redirect_hashtag_goes_to_tags_not_urls(self):
        signup_url = (
            "https://www.linkedin.com/signup/cold-join"
            "?session_redirect=https%3A%2F%2Fwww.linkedin.com%2Ffeed%2Fhashtag%2Fslasheo"
            "&trk=public_post-text"
        )
        urls, mentions, tags = extract_classified_links(
            [signup_url, "https://example.com/a"]
        )
        assert "slasheo" in tags
        assert signup_url not in urls
        assert "https://example.com/a" in urls

    def test_signup_url_without_hashtag_redirect_excluded(self):
        signup_url = "https://www.linkedin.com/signup/cold-join?trk=x"
        urls, mentions, tags = extract_classified_links([signup_url])
        assert signup_url not in urls
        assert tags == []

    def test_redir_wrapper_unwrapped_to_lnkd_in(self):
        redir_url = (
            "https://www.linkedin.com/redir/redirect"
            "?url=https%3A%2F%2Flnkd.in%2FeRgDaRJ8"
            "&urlhash=YcyT&trk=public_post-text"
        )
        urls, _, _ = extract_classified_links([redir_url, "https://example.com/a"])
        assert redir_url not in urls
        assert "https://lnkd.in/eRgDaRJ8" in urls
        assert "https://example.com/a" in urls

    def test_linkedin_post_stays_in_urls(self):
        u = "https://www.linkedin.com/posts/foo_activity-123"
        urls, mentions, tags = extract_classified_links([u])
        assert u in urls
        assert mentions == []
        assert tags == []


class TestIsLinkedinInternalUrl:
    def test_subdomains(self):
        assert is_linkedin_internal_url("https://ie.linkedin.com/in/x") is True
        assert is_linkedin_internal_url("https://lnkd.in/abc") is True
        assert is_linkedin_internal_url("https://github.com/x") is False


class TestCategorizeUrl:
    def test_github(self):
        result = categorize_url("https://github.com/user/repo")
        assert result["type"] == "repository"

    def test_youtube(self):
        result = categorize_url("https://youtube.com/watch?v=123")
        assert result["type"] == "video"

    def test_medium(self):
        result = categorize_url("https://medium.com/@user/article")
        assert result["type"] == "article"

    def test_pdf(self):
        result = categorize_url("https://example.com/doc.pdf")
        assert result["type"] == "document"

    def test_arxiv_pdf_path(self):
        result = categorize_url("https://arxiv.org/pdf/2511.00592")
        assert result["type"] == "document"

    def test_arxiv_html_and_abs_urls(self):
        from linkedin_api.utils.urls import (
            arxiv_abs_url,
            arxiv_html_url,
            arxiv_paper_id,
            rewrite_fetch_url,
        )

        assert arxiv_paper_id("https://arxiv.org/pdf/2511.00592") == "2511.00592"
        assert (
            arxiv_html_url("https://arxiv.org/pdf/2511.00592")
            == "https://arxiv.org/html/2511.00592"
        )
        assert (
            arxiv_abs_url("https://arxiv.org/pdf/2511.00592")
            == "https://arxiv.org/abs/2511.00592"
        )
        assert (
            rewrite_fetch_url("https://arxiv.org/pdf/2511.00592")
            == "https://arxiv.org/html/2511.00592"
        )
        assert (
            rewrite_fetch_url("https://arxiv.org/abs/2511.00592v2")
            == "https://arxiv.org/html/2511.00592v2"
        )

    def test_x_status_id(self):
        from linkedin_api.utils.urls import is_x_status_url, x_status_id

        assert (
            x_status_id("https://x.com/akshay_pachaar/status/2069118430582866051")
            == "2069118430582866051"
        )
        assert is_x_status_url("https://twitter.com/user/status/1234567890")

    def test_is_plausible_resource_url_rejects_code_fragments(self):
        from linkedin_api.utils.urls import is_plausible_resource_url

        assert not is_plausible_resource_url("http://json.dumps?trk=public_post-text")
        assert is_plausible_resource_url("https://example.com/article")

    def test_should_ignore_code_fragment_hosts(self):
        assert should_ignore_url("http://BUILD.bazel?trk=public_post-text") is True
        assert should_ignore_url("http://df.head?trk=public_post-text") is True
        assert should_ignore_url("http://Promise.all?trk=public_post-text") is True

    def test_should_ignore_mailto(self):
        assert should_ignore_url("mailto:jobs@flotthq.com") is True

    def test_should_ignore_linkedin_chrome(self):
        assert should_ignore_url("https://www.linkedin.com/") is True
        assert should_ignore_url("https://www.linkedin.com/legal/cookie-policy") is True
        assert (
            should_ignore_url(
                "https://www.linkedin.com/top-content/artificial-intelligence/ai-in-coding"
            )
            is True
        )

    def test_should_not_ignore_linkedin_redir_wrapper(self):
        url = (
            "https://www.linkedin.com/redir/redirect"
            "?url=https%3A%2F%2Fexample.com%2Farticle"
        )
        assert should_ignore_url(url) is False

    def test_fix_mojibake_smart_quotes(self):
        from linkedin_api.utils.urls import fix_mojibake

        original = 'OpenAI won\'t let you "escape" freely'
        broken = original.encode("utf-8").decode("latin-1")
        assert fix_mojibake(broken) == original

    def test_fix_mojibake_mixed_unicode_body(self):
        from linkedin_api.utils.urls import fix_mojibake

        broken_line = "The constraint doesnâ\x80\x99t limit what the model can express"
        assert (
            fix_mojibake(broken_line)
            == "The constraint doesn’t limit what the model can express"
        )
        mixed = "OpenAI endpoints\n" + "Ã© may be escaped in JSON as\n" + "\\u00e9\n"
        fixed = fix_mojibake(mixed)
        assert "é may be escaped" in fixed
        assert "\\u00e9" in fixed


class TestResolveRedirect:
    """Tests for lnkd.in interstitial page parsing."""

    def _mock_lnkd_response(
        self, page_text: str, final_url: str = "", status_code: int = 200
    ) -> MagicMock:
        """Build a mock requests.get response for a lnkd.in page."""
        resp = MagicMock()
        resp.status_code = status_code
        resp.url = (
            final_url or "https://lnkd.in/erbBvi7E"
        )  # unchanged = no HTTP redirect
        resp.text = page_text
        return resp

    @patch("requests.get")
    def test_lnkd_in_url_in_interstitial_text(self, mock_get):
        """LinkedIn interstitial page includes the target URL in plain text."""
        page = (
            "<html><body>"
            "<p>This link will take you to a page that's not on LinkedIn</p>"
            "<p>Because this is an external link, we're unable to verify it for safety.</p>"
            "<p>https://presse.economie.gouv.fr/acces-illegitimes-au-fichier-national-des-comptes-bancaires-ficoba/</p>"
            "</body></html>"
        )
        mock_get.return_value = self._mock_lnkd_response(page)

        result = resolve_redirect("https://lnkd.in/erbBvi7E")

        assert (
            result
            == "https://presse.economie.gouv.fr/acces-illegitimes-au-fichier-national-des-comptes-bancaires-ficoba/"
        )

    @patch("requests.get")
    def test_lnkd_in_ignores_urls_in_html_attributes(self, mock_get):
        """URLs in HTML attributes (stylesheet href, favicon) are not visible
        to get_text() and are therefore never returned."""
        page = (
            "<html><head>"
            '<link rel="stylesheet" href="https://static.licdn.com/aero-v1/sc/h/abc.css"/>'
            '<link rel="icon" href="https://static.licdn.com/sc/h/favicon.ico"/>'
            "</head><body>"
            "<p>Continue to https://github.com/user/interesting-repo</p>"
            "</body></html>"
        )
        mock_get.return_value = self._mock_lnkd_response(page)

        result = resolve_redirect("https://lnkd.in/eXYZabc")

        assert result == "https://github.com/user/interesting-repo"

    @patch("requests.get")
    def test_lnkd_in_returns_original_when_no_url_found(self, mock_get):
        """Falls back to original URL if nothing useful is found in the page."""
        page = "<html><body><p>Nothing to see here.</p></body></html>"
        mock_get.return_value = self._mock_lnkd_response(page)

        original = "https://lnkd.in/erbBvi7E"
        result = resolve_redirect(original)

        assert result == original

    @patch("requests.get")
    def test_lnkd_in_direct_redirect_uses_final_url_on_406(self, mock_get):
        """When lnkd.in redirects directly (no interstitial), final server may return 406.
        We use response.url to get the resolved target."""
        mock_get.return_value = self._mock_lnkd_response(
            "", final_url="https://github.com/datagouv/datagouv-mcp", status_code=406
        )

        result = resolve_redirect("https://lnkd.in/eAWEsmVw")

        assert result == "https://github.com/datagouv/datagouv-mcp"

    def test_non_lnkd_in_uses_head_redirect(self):
        """Non lnkd.in URLs resolve to final URL via redirect."""
        with patch("requests.get"), patch("requests.head") as mock_head:
            mock_head.return_value = MagicMock(url="https://final.example.com/page")
            result = resolve_redirect("https://short.example.com/abc")

        assert result == "https://final.example.com/page"

    @pytest.mark.integration
    def test_real_lnkd_in_erbBvi7E(self):
        """Live: https://lnkd.in/erbBvi7E should resolve to the French government press release."""
        result = resolve_redirect("https://lnkd.in/erbBvi7E")
        assert "presse.economie.gouv.fr" in result

    @pytest.mark.integration
    def test_real_lnkd_in_eAWEsmVw_resolves_to_github_datagouv_mcp(self):
        """Live: lnkd.in/eAWEsmVw gives HTTP 406 (no interstitial) but redirects to GitHub.
        We should resolve to https://github.com/datagouv/datagouv-mcp."""
        result = resolve_redirect("https://lnkd.in/eAWEsmVw")
        assert result == "https://github.com/datagouv/datagouv-mcp"

    @pytest.mark.integration
    def test_real_lnkd_in_eMcHSAFH_resolves_to_stats_agriculture_with_ssl_verify_off(
        self, monkeypatch
    ):
        """Live: lnkd.in/eMcHSAFH redirects to stats.agriculture.gouv.fr which has SSL cert
        issues. With REQUESTS_SSL_VERIFY=false, we should resolve to the target."""
        import warnings

        import urllib3

        monkeypatch.setenv("REQUESTS_SSL_VERIFY", "false")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            result = resolve_redirect("https://lnkd.in/eMcHSAFH")
        assert "stats.agriculture.gouv.fr" in result
        assert "cartostat" in result

    @pytest.mark.integration
    def test_real_lnkd_in_geemzfeQ_resolves_to_theshamblog(self):
        """Live: lnkd.in/geemzfeQ should show LinkedIn interstitial with target
        https://theshamblog.com/an-ai-agent-wrote-a-hit-piece-on-me-part-4/"""
        result = resolve_redirect("https://lnkd.in/geemzfeQ")
        assert "theshamblog.com" in result


class TestShouldIgnoreUrl:
    def test_linkedin_profile(self):
        assert should_ignore_url("https://linkedin.com/in/john") is True

    def test_linkedin_hashtag(self):
        assert should_ignore_url("https://linkedin.com/feed/hashtag/ai") is True

    def test_linkedin_signup_direct(self):
        assert should_ignore_url("https://www.linkedin.com/signup/cold-join") is True

    def test_linkedin_signup_hashtag_redirect(self):
        # LinkedIn wraps hashtag links in signup redirects for unauthenticated HTML
        url = (
            "https://www.linkedin.com/signup/cold-join"
            "?session_redirect=https%3A%2F%2Fwww.linkedin.com%2Ffeed%2Fhashtag%2Fslasheo"
            "&trk=public_post-text"
        )
        assert should_ignore_url(url) is True

    def test_linkedin_auth_wall(self):
        assert should_ignore_url("https://www.linkedin.com/authwall?trk=x") is True

    def test_hostname_with_file_ext_tld_txt(self):
        # LinkedIn auto-links "llms.txt" as http://llms.txt?trk=...
        assert should_ignore_url("http://llms.txt?trk=public_post-text") is True

    def test_hostname_with_file_ext_tld_cpp(self):
        assert should_ignore_url("http://Llama.cpp?trk=public_post-text") is True

    def test_real_url_with_txt_in_path_not_ignored(self):
        assert should_ignore_url("https://example.com/readme.txt") is False

    def test_external_url(self):
        assert should_ignore_url("https://github.com/repo") is False

"""Tests for post HTML extraction → metadata URLs."""

from __future__ import annotations

from linkedin_api.post_extraction import extract_post_from_html


def test_extract_promotes_urls_from_trafilatura_markdown_body():
    """Guest DOM may miss anchors; links kept in markdown must still become meta urls."""
    html = """
    <html><head>
      <meta property="og:description" content="A few truths still hold up for data leaders about agentic analytics and documentation."/>
      <meta property="og:title" content="Self-Service Analytics"/>
    </head><body>
      <article>
        <p>A few truths still hold up for data leaders about agentic analytics and documentation.
        <a href="https://lnkd.in/eFEpsGFn">https://lnkd.in/eFEpsGFn</a></p>
      </article>
    </body></html>
    """
    ext = extract_post_from_html(
        html, "https://www.linkedin.com/feed/update/urn:li:activity:1"
    )
    assert ext is not None
    assert any("lnkd.in/eFEpsGFn" in u for u in ext.urls)

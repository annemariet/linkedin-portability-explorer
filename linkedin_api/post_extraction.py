"""
Single pipeline for public LinkedIn post HTML → markdown + structured metadata.

Bump ``ENRICHMENT_VERSION`` when extraction/classification semantics change so
downstream can re-fetch stale ``.meta.json`` (see ``enrich_activities``).

Flow: fetch HTML → parse with BeautifulSoup → classify links from the **post body DOM**
(not from markdown strings) → body text as Markdown via **trafilatura** (fallback: plain
text from og:description). Author/date from JSON-LD + existing ``post_html`` helpers.

Comments and full comment threads are out of scope (may need Playwright later).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from linkedin_api.content_store import (
    download_image_to_store,
    resolve_urls_for_metadata,
    save_comments,
    save_content,
    save_metadata,
)
from linkedin_api.utils.post_html import (
    find_post_body_root,
    linkedin_http_fetch_is_blocked,
    parse_comments_from_ld_json,
    parse_post_body_from_soup,
    parse_post_images_from_ld_json,
    parse_post_meta_from_soup,
)
from linkedin_api.utils.urls import (
    extract_classified_links,
    extract_urls_from_text,
    linkedin_hashtag_keyword,
    linkedin_mention_type,
    linkedin_redir_unwrap_url,
    linkedin_signup_redirect_hashtag,
    should_ignore_url,
)

# Increment when DOM classification, markdown conversion, or metadata shape changes.
ENRICHMENT_VERSION = 3


def _strip_trafilatura_comments(md: str) -> str:
    """
    Remove LinkedIn comment-preview paragraphs appended by trafilatura.

    LinkedIn server-side HTML includes a few top comments in a section that
    trafilatura treats as main content.  Comment links carry LinkedIn's
    ``trk=public_post_comment`` tracking param (distinct from ``public_post-text``
    used in the post body).  We split on the first paragraph that contains this
    marker and discard everything from that point on.
    """
    if not md:
        return md
    paragraphs = re.split(r"\n\n+", md)
    clean: list[str] = []
    for para in paragraphs:
        if "public_post_comment" in para or "public_post_see-more-comments" in para:
            break
        clean.append(para)
    result = "\n\n".join(clean).strip()
    # Fallback: if stripping removed everything, keep original
    return result if result else md


def _is_comment_actor_href(href: str) -> bool:
    h = (href or "").lower()
    return "public_post_comment_" in h or "comment_actor" in h


def _normalize_anchor_href(href: str, base_url: str) -> str:
    s = (href or "").strip()
    if not s or s.startswith("#") or s.lower().startswith("javascript:"):
        return ""
    return urljoin(base_url, s)


def classify_links_from_soup(
    soup: BeautifulSoup,
    base_url: str,
) -> tuple[list[str], list[dict[str, str]], list[str], list[str]]:
    """
    Walk anchor and image tags in the **post body** subtree only.

    Returns ``(urls, mentions, hashtags, image_urls)`` — same semantics as
    ``extract_classified_links`` but derived from HTML, not markdown.

    - **mentions**: ``/in/``, ``/company/``, ``/school/`` on LinkedIn hosts.
    - **hashtags**: hashtag links → keyword only (no URL in metadata elsewhere).
    - **urls**: everything else (external, ``/posts/``, ``/redir/``, ``lnkd.in``, …).
    - **image_urls**: ``<img src=…>`` in the body (for diagnostics / future inline MD).
    """
    root = find_post_body_root(soup)
    if root is None:
        # No JS-rendered DOM body; still surface images from JSON-LD.
        return [], [], [], parse_post_images_from_ld_json(soup)

    base = (base_url or "").strip() or "https://www.linkedin.com"
    tags_set: set[str] = set()
    mentions_map: dict[str, dict[str, str]] = {}
    resource_urls: list[str] = []
    seen_res: set[str] = set()
    image_urls: list[str] = []
    seen_img: set[str] = set()

    for img in root.find_all("img", src=True):
        src = _normalize_anchor_href(str(img.get("src") or ""), base)
        if src and src not in seen_img:
            seen_img.add(src)
            image_urls.append(src)

    for a in root.find_all("a", href=True):
        raw = str(a.get("href") or "")
        if _is_comment_actor_href(raw):
            continue
        href = _normalize_anchor_href(raw, base)
        if not href:
            continue
        hk = linkedin_hashtag_keyword(href) or linkedin_signup_redirect_hashtag(href)
        if hk:
            tags_set.add(hk)
            continue
        mtype = linkedin_mention_type(href)
        if mtype:
            name = a.get_text(strip=True)
            if href not in mentions_map:
                mentions_map[href] = {"name": name, "url": href, "type": mtype}
            elif name and not (mentions_map[href].get("name") or "").strip():
                mentions_map[href]["name"] = name
            continue
        href = linkedin_redir_unwrap_url(href) or href
        if should_ignore_url(href):
            continue
        if href not in seen_res:
            seen_res.add(href)
            resource_urls.append(href)

    # Plain URLs in body text (not only inside <a>)
    for u in extract_urls_from_text(root.get_text(" ", strip=False)):
        if not u or u in seen_res:
            continue
        hk = linkedin_hashtag_keyword(u) or linkedin_signup_redirect_hashtag(u)
        if hk:
            tags_set.add(hk)
            continue
        mtype = linkedin_mention_type(u)
        if mtype:
            if u not in mentions_map:
                mentions_map[u] = {"name": "", "url": u, "type": mtype}
            continue
        u = linkedin_redir_unwrap_url(u) or u
        if should_ignore_url(u):
            continue
        seen_res.add(u)
        resource_urls.append(u)

    # Supplement: LD-JSON images when DOM has none (common for multi-image posts).
    if not image_urls:
        image_urls = parse_post_images_from_ld_json(soup)

    return (
        resource_urls,
        list(mentions_map.values()),
        sorted(tags_set),
        image_urls,
    )


def _trafilatura_markdown(html: str, url: str) -> str:
    from trafilatura import extract

    out = extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_images=True,
        include_tables=True,
        include_formatting=True,
        include_comments=False,
    )
    return (out or "").strip()


@dataclass
class PostExtractionResult:
    """Output of :func:`extract_post_from_html`."""

    markdown_body: str
    html_meta: dict[str, str]
    urls: list[str]
    mentions: list[dict[str, str]]
    hashtags: list[str]
    image_urls: list[str]
    comment_count: int = 0
    comments: list[dict] = field(default_factory=list)


def append_missing_resource_urls(markdown: str, urls: list[str]) -> str:
    """Append ``## Links`` for resource URLs not present as text (resolved-aware)."""
    from linkedin_api.utils.urls import extract_urls_from_text, resolve_redirect

    def _resolved_set(urls_in: list[str]) -> set[str]:
        out: set[str] = set()
        for u in urls_in:
            s = (u or "").strip()
            if not s:
                continue
            out.add(s)
            try:
                r = resolve_redirect(s)
            except Exception:
                r = ""
            if r and r != s:
                out.add(r)
        return out

    canonical = resolve_urls_for_metadata(urls or [])
    body_urls = extract_urls_from_text(markdown)
    body_resolved = _resolved_set(body_urls)
    missing: list[str] = []
    for u in canonical:
        if not u:
            continue
        if u in markdown:
            continue
        try:
            u_resolved = resolve_redirect(u)
        except Exception:
            u_resolved = u
        if u_resolved in markdown:
            continue
        if body_resolved & _resolved_set([u]):
            continue
        missing.append(u)
    if not missing:
        return markdown
    block = "\n\n## Links\n\n" + "\n".join(f"- <{u}>" for u in missing)
    return markdown.rstrip() + block


def merge_classification_with_api(
    dom_urls: list[str],
    dom_mentions: list[dict[str, str]],
    dom_hashtags: list[str],
    urls_from_api: list[str],
) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """
    DOM-derived classification is primary; URLs from Portability CSV text fill gaps
    (same rules as ``extract_classified_links(..., extra_urls)``).
    """
    extra_only, ex_m, ex_t = extract_classified_links(urls_from_api)
    url_seen = {u for u in dom_urls}
    out_urls = list(dom_urls)
    for u in extra_only:
        if u not in url_seen:
            url_seen.add(u)
            out_urls.append(u)
    by_url = {m["url"]: dict(m) for m in dom_mentions if m.get("url")}
    for m in ex_m:
        u = m.get("url") or ""
        if u and u not in by_url:
            by_url[u] = dict(m)
        elif u and u in by_url:
            if (m.get("name") or "").strip() and not (
                by_url[u].get("name") or ""
            ).strip():
                by_url[u]["name"] = m["name"]
            if (m.get("type") or "").strip() and not (
                by_url[u].get("type") or ""
            ).strip():
                by_url[u]["type"] = m["type"]
    hashtag_set = set(dom_hashtags) | set(ex_t)
    return out_urls, list(by_url.values()), sorted(hashtag_set)


def save_extraction_to_store(
    *,
    post_id: str,
    post_urn: str,
    post_url: str,
    ext: PostExtractionResult,
    urls_from_api: list[str],
    activity_time_iso: str,
    post_created: str,
    activities_ids: list[str],
) -> tuple[str, list[str]]:
    """
    Merge CSV URLs, resolve, append ``## Links`` if needed, write ``.md`` + ``.meta.json``.

    Shared by ``enrich_activities`` and ``backfill_content_store`` for successful HTML extraction.
    Returns ``(body_markdown, resolved_resource_urls)``.
    """
    u, m, t = merge_classification_with_api(
        ext.urls, ext.mentions, ext.hashtags, urls_from_api
    )
    meta_urls = resolve_urls_for_metadata(u)
    body = append_missing_resource_urls(ext.markdown_body, meta_urls)

    # Download the first image and embed it in the markdown body.
    if ext.image_urls:
        local_img = download_image_to_store(ext.image_urls[0])
        if local_img:
            body = body.rstrip() + f"\n\n![]({local_img})"

    save_content(post_id, body, post_urn=post_urn)
    save_metadata(
        post_id,
        urls=meta_urls,
        mentions=m,
        hashtags=t,
        images=ext.image_urls,
        post_url=post_url,
        post_author=ext.html_meta.get("post_author") or "",
        post_author_url=ext.html_meta.get("post_author_url") or "",
        activity_time_iso=activity_time_iso,
        post_created_at=post_created,
        post_urn=post_urn,
        activities_ids=activities_ids,
        enrichment_version=ENRICHMENT_VERSION,
    )
    if ext.comments:
        save_comments(post_id, ext.comment_count, ext.comments, post_urn=post_urn)
    return body, meta_urls


def extract_post_from_html(html: str, final_url: str) -> PostExtractionResult | None:
    """
    Parse one LinkedIn post HTML document.

    Returns ``None`` if the page looks like a login wall or has no substantial body.
    """
    if linkedin_http_fetch_is_blocked(final_url, html):
        return None
    soup = BeautifulSoup(html, "html.parser")
    plain = parse_post_body_from_soup(soup)
    if not plain or len(plain) < 50:
        return None

    urls, mentions, hashtags, image_urls = classify_links_from_soup(soup, final_url)
    html_meta = parse_post_meta_from_soup(soup)
    comment_count, comments = parse_comments_from_ld_json(soup)

    md_tf = _trafilatura_markdown(html, final_url)
    if md_tf:
        md_tf = _strip_trafilatura_comments(md_tf)
    body = md_tf if (md_tf and len(md_tf) >= 50) else plain

    # Trafilatura often keeps links that the guest DOM walk misses (no body root /
    # anchors outside find_post_body_root). Promote URLs from the body text so
    # meta.urls drives linked-resource fetch.
    urls, mentions, hashtags = merge_classification_with_api(
        urls, mentions, hashtags, extract_urls_from_text(body)
    )

    return PostExtractionResult(
        markdown_body=body,
        html_meta=html_meta,
        urls=urls,
        mentions=mentions,
        hashtags=hashtags,
        image_urls=image_urls,
        comment_count=comment_count,
        comments=comments,
    )

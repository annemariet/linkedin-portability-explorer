"""Extract post author and timestamps from public LinkedIn post HTML."""

from __future__ import annotations

import json
import re
from typing import Any
from bs4 import BeautifulSoup
from bs4.element import Tag

_LI_SUBDOMAIN = re.compile(r"https?://[a-z]{2}\.linkedin\.com", re.I)

# Logged-out ``/feed/update/urn:li:activity:…`` often redirects here; og:description is this blurb.
_LI_GENERIC_OG_BLURB = "500 million+ members"
_LI_GENERIC_OG_BLURB_2 = "manage your professional identity"

# Same as enrich_activities / enrich_profiles (public post body)
_CONTENT_SELECTORS = [
    "article[data-id]",
    ".feed-shared-update-v2__description",
    ".feed-shared-text",
    '[data-test-id="main-feed-activity-card"]',
]


def normalize_linkedin_profile_url(url: str) -> str:
    """Normalize regional linkedin.com hosts to https://www.linkedin.com."""
    s = (url or "").strip()
    if not s:
        return ""
    s = s.split("?")[0]
    s = _LI_SUBDOMAIN.sub("https://www.linkedin.com", s)
    if s.startswith("//linkedin.com"):
        s = "https://www.linkedin.com" + s[len("//linkedin.com") :]
    elif "//linkedin.com" in s and not s.startswith("https://www.linkedin.com"):
        s = s.replace("//linkedin.com", "//www.linkedin.com", 1)
        if s.startswith("//www.linkedin.com"):
            s = "https:" + s
    return s


def _author_from_json_ld_node(obj: dict[str, Any]) -> dict[str, str]:
    """Pull post author and date from a schema.org SocialMediaPosting-like node."""
    out: dict[str, str] = {}
    t = obj.get("@type")
    types = {t} if isinstance(t, str) else set(t or [])
    if not types & {"SocialMediaPosting", "Article", "NewsArticle", "BlogPosting"}:
        return out

    dp = obj.get("datePublished")
    if dp:
        out["post_created_at"] = str(dp).strip()

    author = obj.get("author")
    if isinstance(author, list):
        author = next((x for x in author if isinstance(x, dict)), None)
    if not isinstance(author, dict):
        return out

    name = (author.get("name") or "").strip()
    url = (author.get("url") or "").strip()
    if name and 1 < len(name) < 200:
        out["post_author"] = name
    if url:
        nu = normalize_linkedin_profile_url(url)
        if nu:
            out["post_author_url"] = nu
    return out


def _iter_ld_json_objects(soup: BeautifulSoup):
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    yield item
        elif isinstance(data, dict):
            yield data


def parse_post_meta_from_soup(soup: BeautifulSoup) -> dict[str, str]:
    """
    Post author, author URL, and ``post_created_at`` (ISO) from public post HTML.

    Order: JSON-LD ``datePublished`` / ``author``; then ``<meta>`` article times;
    author DOM links last (same as ``parse_post_author_from_soup`` without meta
    key overlap for date).
    """
    merged = parse_post_author_from_soup(soup)
    if not merged.get("post_created_at"):
        for tag in soup.find_all("meta"):
            prop = tag.get("property") or tag.get("name", "")
            content = str(tag.get("content") or "").strip()
            if not content:
                continue
            if prop in ("article:published_time", "og:article:published_time"):
                merged["post_created_at"] = content
                break
    return merged


def parse_post_author_from_soup(soup: BeautifulSoup) -> dict[str, str]:
    """
    Best-effort post author, profile URL, and published time from public post HTML.

    Prefer JSON-LD (``SocialMediaPosting`` / ``Article``) when present; fall back to
    the main feed actor link (``public_post_feed-actor-name`` / ``feed-actor-name``),
    excluding comment actor links.
    """
    merged: dict[str, str] = {}

    for obj in _iter_ld_json_objects(soup):
        part = _author_from_json_ld_node(obj)
        if part.get("post_author") or part.get("post_author_url"):
            merged.update(part)
            break

    if merged.get("post_author") and merged.get("post_author_url"):
        return merged

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        if "public_post_comment_" in href:
            continue
        if not (
            "public_post_feed-actor-name" in href or "feed-actor-name" in href
        ) or not ("/in/" in href or "/company/" in href):
            continue
        name = a.get_text(strip=True)
        if not name or not (1 < len(name) < 200):
            continue
        url = normalize_linkedin_profile_url(href)
        if not merged.get("post_author"):
            merged["post_author"] = name
        if not merged.get("post_author_url") and url:
            merged["post_author_url"] = url
        break

    return merged


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def parse_comments_from_ld_json(soup: BeautifulSoup) -> tuple[int, list[dict]]:
    """
    Parse comment previews from a ``SocialMediaPosting`` JSON-LD block.

    Returns ``(total_count, comments)`` where ``total_count`` is the ``commentCount``
    field (may exceed ``len(comments)`` — LinkedIn only embeds the top few).

    Each comment dict has:
    - ``author``: display name
    - ``author_url``: LinkedIn profile URL (normalised), or ``""`` if not found in DOM
    - ``timestamp``: ISO 8601 string from ``datePublished``
    - ``text``: plain-text body
    - ``likes``: like count (int)

    Author profile URLs are not in JSON-LD but are available in the DOM via
    ``trk=public_post_comment_actor-name`` anchors; we join the two by display name.
    """
    total_count = 0
    ld_comments: list[dict] = []

    for obj in _iter_ld_json_objects(soup):
        t = obj.get("@type")
        types = {t} if isinstance(t, str) else set(t or [])
        if not types & {"SocialMediaPosting", "Article", "NewsArticle", "BlogPosting"}:
            continue
        total_count = int(obj.get("commentCount") or 0)
        raw_comments = obj.get("comment") or []
        if isinstance(raw_comments, dict):
            raw_comments = [raw_comments]
        for c in raw_comments:
            if not isinstance(c, dict):
                continue
            author_obj = c.get("author") or {}
            name = (
                author_obj.get("name") if isinstance(author_obj, dict) else ""
            ) or ""
            name = name.strip()
            stat = c.get("interactionStatistic") or {}
            likes = 0
            if isinstance(stat, dict):
                try:
                    likes = int(stat.get("userInteractionCount") or 0)
                except (TypeError, ValueError):
                    likes = 0
            ld_comments.append(
                {
                    "author": name,
                    "author_url": "",
                    "timestamp": str(c.get("datePublished") or "").strip(),
                    "text": str(c.get("text") or "").strip(),
                    "likes": likes,
                }
            )
        break  # only the first matching ld+json block

    if not ld_comments:
        return total_count, []

    # Enrich with author profile URLs from DOM (trk=public_post_comment_actor-name)
    url_by_name: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        if "public_post_comment_actor-name" not in href:
            continue
        name = a.get_text(strip=True)
        if not name:
            continue
        url = normalize_linkedin_profile_url(href)
        if url:
            url_by_name[_normalize_name(name)] = url

    for c in ld_comments:
        key = _normalize_name(c["author"])
        if key in url_by_name:
            c["author_url"] = url_by_name[key]

    return total_count, ld_comments


def parse_post_images_from_ld_json(soup: BeautifulSoup) -> list[str]:
    """
    Extract image URLs from a ``SocialMediaPosting`` JSON-LD block.

    Returns URLs from the ``image`` array (``ImageObject.url``), with ``og:image``
    as a single-item fallback.  LinkedIn posts with attached images list all of them
    here; the DOM ``<img>`` tags are JS-rendered and absent in static HTML.
    """
    for obj in _iter_ld_json_objects(soup):
        t = obj.get("@type")
        types = {t} if isinstance(t, str) else set(t or [])
        if not types & {"SocialMediaPosting", "Article", "NewsArticle", "BlogPosting"}:
            continue
        images = obj.get("image") or []
        if isinstance(images, (str, dict)):
            images = [images]
        urls: list[str] = []
        for img in images:
            if isinstance(img, dict):
                url = (img.get("url") or img.get("contentUrl") or "").strip()
            elif isinstance(img, str):
                url = img.strip()
            else:
                continue
            if url:
                urls.append(url)
        if urls:
            return urls
    # Fallback: og:image gives the first (primary) image.
    og = soup.find("meta", property="og:image")
    if og:
        url = str(og.get("content") or "").strip()
        if url:
            return [url]
    return []


def parse_post_author_from_html(html: str) -> dict[str, str]:
    if not html:
        return {}
    return parse_post_author_from_soup(BeautifulSoup(html, "html.parser"))


def linkedin_http_fetch_is_blocked(final_url: str, html: str) -> bool:
    """
    True if the HTTP response is LinkedIn's login/signup shell, not a public post page.

    Unauthenticated requests to ``/feed/update/urn:li:…`` commonly redirect to
    ``/signup/cold-join``; parsing ``og:description`` then yields generic marketing
    copy that must not be stored as post content.  Also detects the GDPR cookie-consent
    wall ("Before you continue to LinkedIn") which serves HTTP 200 at the original URL
    or after a redirect to ``/cookie-policy``.
    """
    u = (final_url or "").lower()
    if "linkedin.com/signup" in u or "linkedin.com/uas/login" in u:
        return True
    if "linkedin.com/cookie-policy" in u or "linkedin.com/legal/cookie" in u:
        return True
    h = html or ""
    if "d_registration-cold-join" in h:
        return True
    if 'data-app-id="com.linkedin.registration-frontend' in h:
        return True
    hl = h.lower()
    if "before you continue to linkedin" in hl:
        return True
    if _LI_GENERIC_OG_BLURB.lower() in hl and _LI_GENERIC_OG_BLURB_2.lower() in hl:
        if "socialmediaposting" not in hl:
            return True
    return False


def _find_post_body_element(soup: BeautifulSoup) -> Tag | None:
    """First substantial post body node from known LinkedIn selectors."""
    for selector in _CONTENT_SELECTORS:
        for elem in soup.select(selector):
            text = elem.get_text(strip=True)
            if text and len(text) > 20:
                return elem
    return None


def find_post_body_root(soup: BeautifulSoup) -> Tag | None:
    """Public alias for link classification and DOM-scoped extraction."""
    return _find_post_body_element(soup)


def parse_post_body_from_soup(soup: BeautifulSoup) -> str:
    """Extract main post body text from public LinkedIn HTML."""
    content_text: list[str] = []
    for selector in _CONTENT_SELECTORS:
        for elem in soup.select(selector):
            text = elem.get_text(strip=True)
            if text and len(text) > 20:
                content_text.append(text)
    if content_text:
        return "\n".join(content_text)
    og = soup.find("meta", property="og:description")
    if og and og.get("content"):
        og_text = str(og["content"])
        og_lower = og_text.lower()
        is_generic_blurb = (
            _LI_GENERIC_OG_BLURB.lower() in og_lower
            and _LI_GENERIC_OG_BLURB_2.lower() in og_lower
        )
        # linkedin_http_fetch_is_blocked() lets pages with a generic
        # "SocialMediaPosting" JSON-LD stub through even when the actual
        # post can't be displayed (that stub is present on the URL
        # template regardless of whether real content loaded) -- so
        # reject the known marketing blurb here too, at the point it
        # would actually be used, rather than relying on that check alone.
        if not is_generic_blurb:
            content_text.append(og_text)
    title = soup.find("title")
    if title:
        t = title.get_text(strip=True)
        if " | " in t:
            content_text.append(t.split(" | ")[0])
    return "\n".join(content_text) if content_text else ""


def parse_post_meta_from_html(html: str) -> dict[str, str]:
    """Author, author URL, and ``post_created_at`` from full HTML document."""
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    return parse_post_meta_from_soup(soup)

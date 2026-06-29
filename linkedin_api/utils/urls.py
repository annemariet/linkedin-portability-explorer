"""
URL extraction and categorization utilities.

Extracted from extract_resources.py for reuse across modules.
"""

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse, urlunparse

from kg_vault.catalog import canonical_source_id


def linkedin_hashtag_keyword(url: str) -> Optional[str]:
    """Hashtag text from a LinkedIn hashtag URL, or None if not a hashtag link."""
    if not url or not is_linkedin_internal_url(url):
        return None
    try:
        path = urlparse(url.strip()).path
    except Exception:
        return None
    m = re.search(r"/hashtag/([^/?#]+)", path, re.I)
    if not m:
        return None
    return unquote(m.group(1)).strip() or None


def linkedin_signup_redirect_hashtag(url: str) -> Optional[str]:
    """Return the hashtag keyword when a LinkedIn signup/authwall URL wraps a hashtag link.

    LinkedIn serves static HTML where hashtag ``<a>`` tags point to
    ``/signup/cold-join?session_redirect=.../feed/hashtag/<keyword>`` for
    unauthenticated visitors.  This decodes the redirect destination and
    extracts the hashtag keyword so callers can add it to ``tags`` rather
    than treating the signup URL as a fetchable resource.
    """
    if not url or not is_linkedin_internal_url(url):
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    path = parsed.path.lower()
    if "/signup/" not in path and "/authwall" not in path:
        return None
    try:
        params = parse_qs(parsed.query)
        redirect = (params.get("session_redirect") or [""])[0]
    except Exception:
        return None
    if not redirect:
        return None
    return linkedin_hashtag_keyword(unquote(redirect))


def linkedin_redir_unwrap_url(url: str) -> str | None:
    """Extract the real target from a LinkedIn /redir/redirect?url=... wrapper.

    LinkedIn replaces external ``<a href>`` links in its HTML with a JS/meta-refresh
    redirect page (title "External Redirection | LinkedIn", 3-second delay).
    ``requests`` cannot follow JS redirects, so the target must be extracted
    statically from the ``url`` query parameter.

    Returns the decoded target URL, or ``None`` if this is not a redir wrapper.
    """
    if not url or not is_linkedin_internal_url(url):
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    if parsed.path.lower().rstrip("/") not in (
        "/redir/redirect",
        "/redir/externalredirect",
    ):
        return None
    try:
        target = (parse_qs(parsed.query).get("url") or [""])[0]
    except Exception:
        return None
    return unquote(target) if target else None


def is_linkedin_mention_url(url: str) -> bool:
    """True for LinkedIn profile, company, or school URLs."""
    if not url or not is_linkedin_internal_url(url):
        return False
    try:
        path = urlparse(url.strip()).path.lower()
    except Exception:
        return False
    return bool(
        re.match(r"/in/[^/]+", path)
        or re.match(r"/company/[^/]+", path)
        or re.match(r"/school/[^/]+", path)
    )


def extract_classified_links(
    urls: List[str],
) -> Tuple[List[str], List[Dict[str, str]], List[str]]:
    """
    Classify a list of URLs for content-store metadata.

    Returns ``(urls, mentions, tags)``:

    - ``urls`` — resources and other links, excluding hashtag and profile/company/school URLs.
    - ``mentions`` — ``{"name": str, "url": str}`` for LinkedIn profiles/companies/schools.
    - ``tags`` — hashtag keywords only (no URL stored), from ``/feed/hashtag/…`` links.
    """
    deduped = list(dict.fromkeys(u.strip() for u in (urls or []) if u and u.strip()))
    tags_set: set[str] = set()
    mentions_map: Dict[str, Dict[str, str]] = {}
    resource_urls: List[str] = []

    for u in deduped:
        u = linkedin_redir_unwrap_url(u) or u
        hk = linkedin_hashtag_keyword(u) or linkedin_signup_redirect_hashtag(u)
        if hk:
            tags_set.add(hk)
            continue
        if is_linkedin_mention_url(u):
            mentions_map[u] = {"name": "", "url": u}
            continue
        if should_ignore_url(u):
            continue
        resource_urls.append(u)

    return resource_urls, list(mentions_map.values()), sorted(tags_set)


def extract_urls_from_text(text: str) -> List[str]:
    """
    Extract all URLs from text using regex.

    Args:
        text: Text content to search for URLs

    Returns:
        List of unique URLs found
    """
    if not text:
        return []

    url_pattern = r"https?://[^\s<>\"'{}|\\^`\[\]]+[^\s<>\"'{}|\\^`\[\].,;:!?]"
    urls = re.findall(url_pattern, text)

    cleaned_urls = []
    for url in urls:
        url = url.rstrip(".,;:!?)")
        try:
            parsed = urlparse(url)
            if parsed.netloc:
                cleaned_urls.append(url)
        except Exception:
            continue

    return list(set(cleaned_urls))


def _repair_mojibake_segment(segment: str) -> str:
    """Try to repair one line/segment mis-decoded as latin-1 or cp1252."""
    if "â" not in segment and "Ã" not in segment:
        return segment
    for encoding in ("latin-1", "cp1252"):
        try:
            repaired = segment.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired != segment and "\ufffd" not in repaired:
            return repaired
    return segment


def fix_mojibake(text: str) -> str:
    """Repair UTF-8 text that was mis-decoded as latin-1/cp1252 (e.g. smart quotes)."""
    if not text or ("â" not in text and "Ã" not in text):
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
        if repaired != text and "\ufffd" not in repaired:
            return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return "".join(
        _repair_mojibake_segment(line) for line in text.splitlines(keepends=True)
    )


def categorize_url(url: str) -> Dict[str, Optional[str]]:
    """
    Categorize a URL by domain and type.

    Returns:
        Dict with 'domain' and 'type' keys
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        path = parsed.path.lower()

        if domain.startswith("www."):
            domain = domain[4:]

        url_lower = url.lower()
        file_extensions = {
            ".pdf": "document",
            ".doc": "document",
            ".docx": "document",
            ".ppt": "presentation",
            ".pptx": "presentation",
            ".mp4": "video",
            ".jpg": "image",
            ".jpeg": "image",
            ".png": "image",
            ".gif": "image",
            ".svg": "image",
        }

        resource_type: Optional[str]
        for ext, resource_type in file_extensions.items():
            if ext in url_lower:
                return {"domain": domain, "type": resource_type}

        if re.search(r"/pdf(?:/|$)", path) or path.endswith("/pdf"):
            return {"domain": domain, "type": "document"}

        resource_type = None

        if any(d in domain for d in ["youtube.com", "youtu.be", "vimeo.com"]):
            resource_type = "video"
        elif any(d in domain for d in ["github.com", "gitlab.com", "bitbucket.org"]):
            resource_type = "repository"
        elif any(d in domain for d in ["docs.", "readthedocs.io"]):
            resource_type = "documentation"
        elif any(
            d in domain
            for d in ["medium.com", "substack.com", "dev.to", "hashnode.com"]
        ) or any(p in path for p in ["/blog/", "/article/"]):
            resource_type = "article"
        elif any(d in domain for d in ["arxiv.org", "scholar.google.com"]):
            resource_type = "research"
        elif "linkedin.com" in domain and "/pulse/" in url:
            resource_type = "article"

        if resource_type is None:
            resource_type = "article"

        return {"domain": domain, "type": resource_type}
    except Exception:
        return {"domain": None, "type": "unknown"}


def strip_utm_params(url: str) -> str:
    """Return *url* with all ``utm_*`` tracking parameters removed.

    Used for resource-store deduplication: two URLs that differ only in UTM
    campaign tags refer to the same canonical resource.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
        query = urlencode(
            [
                (k, v)
                for k, v in parse_qsl(parsed.query)
                if not k.lower().startswith("utm_")
            ]
        )
        return urlunparse(parsed._replace(query=query))
    except Exception:
        return url


def canonical_resource_url(url: str) -> str:
    """Stable resource identity (UTM/tracking stripped, normalized host/path).

    Alias for :func:`kg_vault.catalog.canonical_source_id` without redirect resolution.
    Pass ``resolved=`` when the caller already followed redirects.
    """
    return canonical_source_id(url)


def is_linkedin_internal_url(url: str) -> bool:
    """True for linkedin.com / lnkd.in hosts (incl. regional subdomains)."""
    if not (url or "").strip():
        return False
    try:
        netloc = urlparse(url.strip()).netloc.lower()
    except Exception:
        return False
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return (
        "linkedin.com" in netloc or netloc == "lnkd.in" or netloc.endswith(".lnkd.in")
    )


def is_comment_feed_url(url: str) -> bool:
    """True if URL is a feed/update with a comment URN (not a post); such URLs don't return post content."""
    return bool(url and "urn:li:comment:" in url)


# Hostname TLDs that are file extensions, not real web TLDs.
# LinkedIn auto-links text like "llms.txt" or "Llama.cpp" as bare HTTP URLs;
# these will never resolve to a useful page.
_FILE_EXT_TLDS: frozenset[str] = frozenset(
    {
        "txt",
        "cpp",
        "py",
        "js",
        "ts",
        "go",
        "rs",
        "md",
        "json",
        "yaml",
        "yml",
        "csv",
        "log",
        "sh",
        "sql",
        "c",
        "h",
        "rb",
        "java",
        "kt",
        "swift",
        "r",
        "bazel",
        "xlsx",
    }
)

# Hostname “TLD” segments that are code tokens, not real DNS TLDs (df.head, Promise.all, …).
_CODE_LIKE_TLDS: frozenset[str] = frozenset(
    {
        "all",
        "get",
        "head",
        "tail",
        "shape",
        "dtypes",
        "info",
        "corr",
        "column",
        "append",
        "client",
        "search",
        "total",
        "date",
        "array",
        "tts",
        "title",
        "amelie",
    }
)

_LINKEDIN_CHROME_PREFIXES = (
    "/legal/",
    "/mypreferences/",
    "/top-content/",
)


def is_plausible_resource_url(url: str) -> bool:
    """False for malformed or code-fragment URLs that regex extraction can produce."""
    raw = (url or "").strip()
    if not raw.startswith(("http://", "https://")):
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if not host or "." not in host:
        return False
    if host in {"json.dumps", "json.loads", "repr", "str"}:
        return False
    labels = host.split(".")
    if len(labels) < 2 or not labels[-1].isalpha() or len(labels[-1]) < 2:
        return False
    return True


def _host_looks_like_filename(url: str) -> bool:
    """True when the hostname ends in a common file extension (e.g. llms.txt, Llama.cpp)."""
    try:
        host = urlparse(url).hostname or ""
        tld = host.rsplit(".", 1)[-1].lower() if "." in host else ""
        return tld in _FILE_EXT_TLDS
    except Exception:
        return False


def _host_looks_like_code_fragment(url: str) -> bool:
    """True for bare ``http://df.head``-style URLs LinkedIn invents from post text."""
    if _host_looks_like_filename(url):
        return True
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host or host.startswith("@") or "@" in host:
        return True
    if "." not in host:
        return True
    tld = host.rsplit(".", 1)[-1]
    if tld in _CODE_LIKE_TLDS:
        return True
    return False


def _is_linkedin_chrome_url(url: str) -> bool:
    """LinkedIn site chrome scraped from public post HTML — not linked articles."""
    if not is_linkedin_internal_url(url):
        return False
    try:
        parsed = urlparse(url.strip())
        path = (parsed.path or "/").lower().rstrip("/") or "/"
    except Exception:
        return False
    if path == "/":
        return True
    return any(path.startswith(prefix) for prefix in _LINKEDIN_CHROME_PREFIXES)


_ARXIV_PAPER_RE = re.compile(
    r"^https?://(?:www\.)?arxiv\.org/(?:pdf|abs|html)/([^/?#]+)",
    re.IGNORECASE,
)


def arxiv_paper_id(url: str) -> str | None:
    """Extract arXiv paper id (with optional version suffix) from a paper URL."""
    match = _ARXIV_PAPER_RE.match((url or "").strip())
    if not match:
        return None
    return match.group(1).removesuffix(".pdf")


def arxiv_html_url(url: str) -> str | None:
    """Return arXiv HTML article URL when the input is an arXiv paper link."""
    paper_id = arxiv_paper_id(url)
    if not paper_id:
        return None
    return f"https://arxiv.org/html/{paper_id}"


def arxiv_abs_url(url: str) -> str | None:
    """Return arXiv abstract page URL for a paper link, if applicable."""
    paper_id = arxiv_paper_id(url)
    if not paper_id:
        return None
    return f"https://arxiv.org/abs/{paper_id}"


def rewrite_fetch_url(url: str) -> str:
    """Rewrite known non-HTML URLs to an HTML page when possible."""
    rewritten = arxiv_html_url(url) or arxiv_abs_url(url)
    return rewritten or url


_X_STATUS_RE = re.compile(
    r"^https?://(?:(?:www\.)?(?:x|twitter)\.com)(?:/[^/]+)?/status/(\d+)",
    re.IGNORECASE,
)


def x_status_id(url: str) -> str | None:
    """Return numeric status id from an X/Twitter status URL."""
    match = _X_STATUS_RE.match((url or "").strip())
    return match.group(1) if match else None


def is_x_status_url(url: str) -> bool:
    return x_status_id(url) is not None


def should_ignore_url(url: str) -> bool:
    """Check if URL should be ignored (hashtags, profile links, auth pages, etc.)."""
    raw = (url or "").strip()
    if raw.lower().startswith("mailto:"):
        return True
    if not is_plausible_resource_url(raw):
        return True
    if _host_looks_like_code_fragment(raw):
        return True
    if _is_linkedin_chrome_url(raw):
        return True
    if "linkedin.com/in/" in raw or "linkedin.com/pub/" in raw:
        return True
    if "linkedin.com/feed/hashtag/" in raw:
        return True
    if "linkedin.com/company/" in raw:
        return True
    if raw.startswith("https://www.linkedin.com/feed/"):
        return True
    if "linkedin.com/signup/" in raw or "linkedin.com/authwall" in raw:
        return True
    if "linkedin.com/showcase/" in raw or "linkedin.com/school/" in raw:
        return True
    return False


def resolve_redirect(url: str, max_redirects: int = 5) -> str:
    """Resolve redirects to get the final URL.

    Handles LinkedIn short URLs (lnkd.in) which use an intermediate page.
    When lnkd.in redirects directly (no interstitial), uses response.url even
    if the final server returns 4xx/5xx (e.g. 406).

    Returns:
        Final URL after following redirects, or original URL if resolution fails
    """
    if max_redirects <= 0:
        return url

    # Statically unwrap LinkedIn's JS-redirect wrapper before any HTTP request.
    # The page uses a meta-refresh delay so requests cannot follow it automatically.
    unwrapped = linkedin_redir_unwrap_url(url)
    if unwrapped:
        return resolve_redirect(unwrapped, max_redirects=max_redirects - 1)

    import os

    import requests
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    verify = os.environ.get("REQUESTS_SSL_VERIFY", "true").lower() not in ("0", "false")

    if "lnkd.in" in url:
        try:
            response = requests.get(
                url, timeout=15, allow_redirects=True, headers=headers, verify=verify
            )
            # Prefer the final non-LinkedIn URL from the HTTP redirect chain when present.
            if response.history:
                for hop in reversed(list(response.history) + [response]):
                    hop_url = str(getattr(hop, "url", ""))
                    hop_lower = hop_url.lower()
                    if (
                        hop_url
                        and "linkedin.com" not in hop_lower
                        and "lnkd.in" not in hop_lower
                    ):
                        return hop_url
            # LinkedIn sometimes shows a security interstitial with the target URL in
            # the page text: "This link will take you to… https://…"
            # Parsing with BeautifulSoup and searching get_text() naturally
            # excludes URLs buried in HTML attributes (script src, link href…).
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                for found in re.findall(r"https?://\S+", soup.get_text()):
                    found_str = str(found).rstrip(".,;:!?)")
                    found_lower = found_str.lower()
                    if (
                        "linkedin.com" not in found_lower
                        and "lnkd.in" not in found_lower
                    ):
                        return found_str
            # Direct redirect (no interstitial): lnkd.in → target. Use final URL
            # even if target returns 406, 404, etc. (e.g. GitHub 406, expired lnkd.in).
            if response.url and response.url != url:
                final = str(response.url)
                final_lower = final.lower()
                if "linkedin.com" not in final_lower and "lnkd.in" not in final_lower:
                    return final
        except Exception:
            pass
        return url

    try:
        response = requests.head(
            url, timeout=(5, 10), allow_redirects=True, headers=headers, verify=verify
        )
        if response.url != url:
            return str(response.url)
    except Exception:
        pass

    return url

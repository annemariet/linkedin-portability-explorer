"""
Fetch content from URLs linked in LinkedIn posts/comments.

Pluggable extractor with strategy dispatch by URL type.

Body-fetch backend selectable via ``LINKEDIN_EXTRACTOR``:
  - ``httpx``  (default) — requests + BeautifulSoup body extraction.
  - ``tavily`` — Tavily Extract API; handles JS-rendered / Cloudflare-gated
    pages the httpx backend can't. Falls back to httpx if ``TAVILY_API_KEY``
    isn't configured. See ``TAVILY_EXTRACT_DEPTH`` (basic|advanced).
Metadata-only for YouTube/GitHub/podcasts regardless of backend. arXiv and
X/Twitter status URLs are special-cased regardless of backend (see
``_fetch_arxiv`` / ``_fetch_x_status``).

Storage
-------
Fetched resource content is persisted in the *resource store*:
  ``get_data_dir() / "resources/"``
Each URL is identified by the SHA-256 hash of its (resolved) URL.
  - ``{hash}.json``  — FetchResult (including title and body text)
  - ``{hash}.md``    — body text as plain text / future Markdown

Note: ``FetchResult.images`` (when Tavily's API provides it) is stored as
remote URLs only — not downloaded or embedded. Tavily's own markdown
``![]()`` refs for LinkedIn pages are unreliable (comment avatars mixed in
with the post's real image, and the real image's URL is often a broken
lazy-load placeholder rather than the actual CDN link), so there's no
markdown-scraping fallback either. Getting the real post image reliably
needs the DOM/JSON-LD approach in ``post_extraction.py``, which requires a
raw HTML fetch Tavily doesn't give us — not implemented here yet.

Typical pipeline
----------------
1. Post content (from API or HTTP fetch) is stored in the content store.
2. ``enrich_activities`` populates ``meta.json`` ``urls`` field.
3. This module reads those URLs and fetches their content.

CLI
---
  uv run linkedin-fetch-content          # all posts (preferred)
  uv run python -m linkedin_api.fetch_linked_content          # same
  uv run linkedin-fetch-content --limit 5
  uv run linkedin-fetch-content --dry-run
  uv run linkedin-fetch-content --verbose   # per-URL log lines (no progress bar)
  uv run linkedin-fetch-content --no-progress

  LINKEDIN_EXTRACTOR=tavily uv run linkedin-fetch-content

Compare backends on specific URLs before switching the default (see
``compare_extractors.py``):
  uv run python -m linkedin_api.compare_extractors "https://…" "https://…"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from linkedin_api.activity_csv import get_data_dir
from linkedin_api.content_store import (
    _content_dir,
    _load_registry,
    update_urls_metadata,
)
from linkedin_api.html_text import extract_html_body_text, x_article_blocks_to_text
from linkedin_api.utils.auth import get_secret
from linkedin_api.utils.urls import (
    arxiv_paper_id,
    canonical_resource_url,
    categorize_url,
    extract_urls_from_text,
    fix_mojibake,
    is_x_status_url,
    resolve_redirect,
    rewrite_fetch_url,
    should_ignore_url,
    strip_utm_params,
    x_status_id,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request headers
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _ssl_verify() -> bool:
    """Honor REQUESTS_SSL_VERIFY=false for sites with certificate issues."""
    return os.environ.get("REQUESTS_SSL_VERIFY", "true").lower() not in ("0", "false")


# ---------------------------------------------------------------------------
# FetchResult
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    """Result of fetching a single linked resource URL."""

    url: str
    resolved_url: str = ""
    title: str = ""
    content: str = ""
    images: list[str] = field(default_factory=list)
    url_type: str = ""
    domain: str = ""
    error: str = ""
    fetched_at: str = ""
    tldr: str = ""
    summary_author: str = ""
    summary_bullets: list[str] = field(default_factory=list)
    summary_model: str = ""
    summarized_at: str = ""

    @property
    def ok(self) -> bool:
        """True if at least a title or content was retrieved without error."""
        return bool((self.content or self.title) and not self.error)


_BINARY_CONTENT_MARKERS = ("%PDF-", "\x89PNG")


def is_exportable_resource(result: FetchResult) -> bool:
    """Skip binary PDFs, errors, and title-only noise from vault export."""
    if result.error:
        return False
    body = (result.content or "").strip()
    title = (result.title or "").strip()
    if body and any(body.startswith(marker) for marker in _BINARY_CONTENT_MARKERS):
        return False
    if not body and not title:
        return False
    if not body and title.startswith(("http://", "https://")):
        return False
    return True


# ---------------------------------------------------------------------------
# Strategy type alias
# ---------------------------------------------------------------------------

FetchStrategy = Callable[[str], tuple[str, str, list[str]]]  # (title, content, images)

# ---------------------------------------------------------------------------
# Concrete strategies
# ---------------------------------------------------------------------------


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB — skip pathologically large pages
_BINARY_PREFIXES = (b"%PDF-", b"\x89PNG\r\n", b"PK\x03\x04", b"\x1f\x8b\x08")


def _decode_response_bytes(raw: bytes, resp: requests.Response) -> str:
    """Decode HTTP body bytes, rejecting obvious binary payloads."""
    if not raw:
        raise ValueError("empty response")
    if any(raw.startswith(prefix) for prefix in _BINARY_PREFIXES):
        raise ValueError("binary content")
    encoding = (resp.encoding or "").strip()
    if encoding and encoding.lower() not in {"utf-8", "utf8"}:
        try:
            return fix_mojibake(raw.decode(encoding))
        except (UnicodeDecodeError, LookupError):
            pass
    try:
        from charset_normalizer import from_bytes

        detected = from_bytes(raw).best()
        if detected is not None:
            return fix_mojibake(str(detected))
    except Exception:
        pass
    return fix_mojibake(raw.decode("utf-8", errors="replace"))


def _fetch_soup(
    url: str, timeout: tuple[int, int] = (5, 15)
) -> tuple[BeautifulSoup, str]:
    """Fetch *url* and return (parsed soup, og:title or <title>).

    Uses streaming to cap download size at ``_MAX_RESPONSE_BYTES`` so a slow-
    streaming server cannot block the pipeline indefinitely.
    ``timeout`` is a (connect, read) tuple: connect must succeed within 5 s,
    each read chunk within 15 s.
    """
    resp = requests.get(
        url,
        timeout=timeout,
        allow_redirects=True,
        headers=_HEADERS,
        verify=_ssl_verify(),
        stream=True,
    )
    if resp.status_code >= 500:
        raise ValueError(f"HTTP {resp.status_code}")
    raw = b""
    for chunk in resp.iter_content(chunk_size=32768):
        raw += chunk
        if len(raw) >= _MAX_RESPONSE_BYTES:
            break
    resp.close()
    if not raw:
        raise ValueError("empty response")
    html = _decode_response_bytes(raw, resp)
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:title")
    title = (
        fix_mojibake(str(og["content"]).strip())
        if og and og.get("content")
        else fix_mojibake(soup.title.get_text(strip=True) if soup.title else "")
    )
    return soup, title


def _fetch_html_body(url: str) -> tuple[str, str, list[str]]:
    """Extract title and body text, preserving inline code as Markdown backticks.

    No image extraction here — see ``post_extraction.py`` for the DOM-scoped
    (post-body-subtree) approach used for the user's own posts; porting that
    to arbitrary linked URLs is future work.
    """
    soup, title = _fetch_soup(url)
    content = extract_html_body_text(soup)
    return title, content, []


_FXTWITTER_STATUS_API = "https://api.fxtwitter.com/status/{tweet_id}"
_VXTWITTER_STATUS_API = "https://api.vxtwitter.com/status/{tweet_id}"
_X_LOGIN_WALL_MARKERS = (
    "log in\nsign up",
    "don't miss what's happening",
    "people on x are the first to know",
)


def _is_x_login_wall(content: str) -> bool:
    lower = (content or "").lower()
    if len(content) > 2500:
        return False
    return any(marker in lower for marker in _X_LOGIN_WALL_MARKERS)


def _x_status_title(tweet: dict) -> str:
    article = tweet.get("article") or {}
    title = (article.get("title") or "").strip()
    if title:
        return title
    author = tweet.get("author") or {}
    name = (author.get("name") or "").strip()
    handle = (author.get("screen_name") or "").strip()
    if name and handle:
        return f"{name} (@{handle}) on X"
    if name:
        return f"{name} on X"
    return "Post on X"


def _x_status_content(tweet: dict) -> str:
    parts: list[str] = []
    text = (tweet.get("text") or "").strip()
    if text:
        parts.append(text)
    article = tweet.get("article") or {}
    article_body = x_article_blocks_to_text(article.get("content"))
    if article_body:
        parts.append(article_body)
    elif (article.get("preview_text") or "").strip():
        parts.append(str(article["preview_text"]).strip())
    return "\n\n".join(parts)


def _fetch_x_status_api(tweet_id: str) -> dict | None:
    for template in (_FXTWITTER_STATUS_API, _VXTWITTER_STATUS_API):
        try:
            resp = requests.get(
                template.format(tweet_id=tweet_id),
                timeout=(5, 20),
                headers=_HEADERS,
            )
            if resp.status_code >= 400:
                continue
            payload = resp.json()
        except Exception:
            continue
        tweet = payload.get("tweet")
        if isinstance(tweet, dict):
            return tweet
    return None


def _fetch_x_status(url: str) -> tuple[str, str, list[str]]:
    """Fetch X/Twitter status text via fxTwitter (fallback vxTwitter)."""
    tweet_id = x_status_id(url)
    if not tweet_id:
        return _fetch_html_body(url)
    tweet = _fetch_x_status_api(tweet_id)
    if tweet is None:
        return _fetch_html_body(url)
    title = _x_status_title(tweet)
    content = _x_status_content(tweet)
    if not content.strip():
        return _fetch_html_body(url)
    return title, content, []


def _fetch_metadata_only(url: str) -> tuple[str, str, list[str]]:
    """Title-only fetch (og:title / <title>); body extraction deferred.

    Used for video platforms (YouTube), code repositories (GitHub), etc.
    """
    _, title = _fetch_soup(url, timeout=(5, 10))
    return title, "", []


_MIN_ARXIV_HTML_CHARS = 300


def _fetch_arxiv(url: str) -> tuple[str, str, list[str]]:
    """Fetch arXiv paper text from HTML; fall back to abstract page."""
    paper_id = arxiv_paper_id(url)
    if not paper_id:
        return _fetch_html_body(url)
    html_url = f"https://arxiv.org/html/{paper_id}"
    try:
        title, content, _images = _fetch_html_body(html_url)
        if len(content.strip()) >= _MIN_ARXIV_HTML_CHARS:
            return title, content, []
    except Exception:
        pass
    return _fetch_html_body(f"https://arxiv.org/abs/{paper_id}")


#: Shared cross-project keychain used by amai-lab's lucys-foundry
#: ``manage_keys.py set tavily`` (service "lucys-foundry", legacy
#: "agent-fleet-rts", account "tavily"). Unifying this with this repo's own
#: TAVILY_API_KEY/LINKEDIN_ACCOUNT convention is tracked as amai-lab ADR 0001
#: §4 / LUC-96 — not resolved yet, hence checking both here.
_SHARED_TAVILY_KEYRING_LOOKUPS: tuple[tuple[str, str], ...] = (
    ("lucys-foundry", "tavily"),
    ("agent-fleet-rts", "tavily"),
)


def _tavily_api_key() -> str:
    """Resolve TAVILY_API_KEY: this repo's own keyring convention (see
    ``get_secret``, which also falls back to the env var), then the shared
    lucys-foundry keychain (see ``_SHARED_TAVILY_KEYRING_LOOKUPS``)."""
    key = get_secret("TAVILY_API_KEY")
    if key:
        return key

    try:
        import keyring

        for service, account in _SHARED_TAVILY_KEYRING_LOOKUPS:
            key = keyring.get_password(service, account)
            if key:
                return key
    except Exception:
        pass

    return ""


def _title_from_markdown(content: str) -> str:
    """First non-empty line of extracted markdown, used as a title fallback
    when Tavily's response has no (or an empty) ``title`` field."""
    for line in content.splitlines():
        line = line.strip()
        if line:
            return line.lstrip("#").strip()
    return ""


#: Tavily fetches LinkedIn post pages via the logged-out guest view, which
#: prepends a fixed nav/sign-in preamble ("Agree & Join LinkedIn", top nav,
#: sign-in/join links — same on every post) before the actual content. Drop
#: everything up to this heading, where the real post starts.
_LINKEDIN_POST_HEADING_RE = re.compile(r"^#\s+.+[’']s Post\s*$", re.MULTILINE)

#: Guest-view footer starts here: a content-category nav menu, copyright,
#: language picker, and a "sign in to view more" call-to-action — none of it
#: post content. Drop everything from this heading onward.
_LINKEDIN_FOOTER_MARKER = "## Explore content categories"

#: Words that make up LinkedIn's like/reply/share/reaction-count UI chrome.
#: A line built entirely out of these (after stripping markdown link syntax)
#: is a button row, not content — safe to drop. Lines of pure digits (e.g. a
#: comment that's just a number, common in "guess the number" posts) are
#: untouched since they have no letters to match here.
_LINKEDIN_CHROME_WORDS = frozenset(
    {
        "like",
        "reply",
        "comment",
        "share",
        "copy",
        "reaction",
        "reactions",
        "linkedin",
        "facebook",
        "x",
    }
)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _is_linkedin_chrome_line(line: str) -> bool:
    # Surround each link's text with spaces so adjacent links (e.g.
    # "[Like](url)[Reply](url)") don't concatenate into one token.
    text = _MARKDOWN_LINK_RE.sub(r" \1 ", line)
    tokens = re.findall(r"[A-Za-z]+", text)
    return bool(tokens) and all(t.lower() in _LINKEDIN_CHROME_WORDS for t in tokens)


def _clean_linkedin_markdown(content: str, url: str) -> str:
    """Strip LinkedIn guest-view chrome from Tavily's markdown: the nav/
    sign-in preamble before the post, the "Explore content categories" footer
    (categories nav, copyright, language picker, sign-in CTA) after the
    comments, and inline Like/Reply/Share/Reaction button rows scattered
    throughout. No-op for non-LinkedIn URLs, or when a marker isn't found
    (e.g. articles without a "<Name>'s Post" heading) — safe to call
    unconditionally.
    """
    if "linkedin.com" not in url:
        return content

    match = _LINKEDIN_POST_HEADING_RE.search(content)
    if match:
        content = content[match.start() :]

    footer_idx = content.find(_LINKEDIN_FOOTER_MARKER)
    if footer_idx != -1:
        content = content[:footer_idx]

    lines = [ln for ln in content.splitlines() if not _is_linkedin_chrome_line(ln)]
    content = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return content.strip()


def _fetch_tavily(url: str) -> tuple[str, str, list[str]]:
    """Extract title/body/images via Tavily's Extract API.

    Handles JS-rendered pages and Cloudflare-gated sites that ``_fetch_html_body``
    cannot (see ``TAVILY_EXTRACT_DEPTH``, default "advanced").
    """
    from tavily import TavilyClient  # type: ignore[import-untyped]

    api_key = _tavily_api_key()
    if not api_key:
        raise ValueError("TAVILY_API_KEY not configured (keyring or env)")

    depth = os.environ.get("TAVILY_EXTRACT_DEPTH", "advanced").strip().lower()
    if depth not in ("basic", "advanced"):
        depth = "advanced"

    client = TavilyClient(api_key=api_key)
    response = client.extract(
        urls=[url], extract_depth=depth, format="markdown", include_images=True
    )

    failed = response.get("failed_results") or []
    if failed:
        reason = failed[0].get("error") or "extraction failed"
        raise ValueError(f"tavily: {reason}")

    results = response.get("results") or []
    if not results:
        raise ValueError("tavily: no content extracted")

    result = results[0]
    content = _clean_linkedin_markdown(str(result.get("raw_content") or ""), url)
    title = str(result.get("title") or "") or _title_from_markdown(content)
    # Tavily's own images field is the only source now — its markdown ![]()
    # refs for LinkedIn are unreliable (comment avatars, broken lazy-load
    # placeholders for the real post image), see module docstring.
    images = [str(u) for u in (result.get("images") or []) if u]
    return title, content, images


# ---------------------------------------------------------------------------
# Strategy registry  (dispatch table — extend here for new URL types)
# ---------------------------------------------------------------------------

#: Body-fetch backends selectable via ``LINKEDIN_EXTRACTOR=httpx|tavily``.
_BODY_BACKENDS: dict[str, FetchStrategy] = {
    "httpx": _fetch_html_body,
    "tavily": _fetch_tavily,
}

#: URL types that are always metadata-only (body extraction deferred), regardless
#: of ``LINKEDIN_EXTRACTOR`` — no point spending Tavily credits on YouTube/GitHub.
_METADATA_ONLY_URL_TYPES: frozenset[str] = frozenset({"video", "repository", "podcast"})

#: URL types whose content we never attempt to fetch (binary / media files).
SKIP_TYPES: frozenset[str] = frozenset(
    {"image", "document", "presentation", "archive", "audio"}
)


def _extractor_backend() -> str:
    """Resolve ``LINKEDIN_EXTRACTOR`` (default ``httpx``).

    Falls back to ``httpx`` with a warning when ``tavily`` is selected but no
    ``TAVILY_API_KEY`` is configured, so the pipeline keeps working without it.
    """
    backend = os.environ.get("LINKEDIN_EXTRACTOR", "httpx").strip().lower()
    if backend not in _BODY_BACKENDS:
        backend = "httpx"
    if backend == "tavily" and not _tavily_api_key():
        logger.warning(
            "LINKEDIN_EXTRACTOR=tavily but no TAVILY_API_KEY configured "
            "(keyring or env) — falling back to httpx"
        )
        return "httpx"
    return backend


def _strategy_for(url_type: str) -> FetchStrategy:
    """Dispatch by URL type; body-fetch types honor ``LINKEDIN_EXTRACTOR``."""
    if url_type in _METADATA_ONLY_URL_TYPES:
        return _fetch_metadata_only
    return _BODY_BACKENDS[_extractor_backend()]


# Cloudflare JS challenge markers — title and body fragments that identify a
# challenge page rather than real content (Medium, some news sites).
_CLOUDFLARE_TITLE_MARKER = "just a moment"
_CLOUDFLARE_BODY_MARKERS = ("enable javascript and cookies to continue",)

_HOST_UNRESOLVED_RE = re.compile(r"host='([^']+)'")


def _format_fetch_error(exc: BaseException) -> str:
    """Short, log-friendly error text (avoid urllib3 stack dumps)."""
    msg = str(exc)
    if msg.startswith("No connection adapters were found for 'mailto:"):
        return "mailto link"
    if "CERTIFICATE_VERIFY_FAILED" in msg or "SSLError" in msg:
        return "SSL certificate verification failed"
    if "Read timed out" in msg or "ConnectTimeout" in msg:
        return "timeout"
    if "NameResolutionError" in msg or "Failed to resolve" in msg:
        match = _HOST_UNRESOLVED_RE.search(msg)
        if match:
            return f"unresolved host ({match.group(1)})"
        return "DNS failure"
    if len(msg) > 160:
        return msg[:157] + "..."
    return msg


# ---------------------------------------------------------------------------
# Resource store (keyed by SHA-256 of the resolved URL)
# ---------------------------------------------------------------------------


def _resource_dir() -> Path:
    """Return (and create) the resource storage directory."""
    d = get_data_dir() / "resources"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _url_stem(url: str) -> str:
    """Stable filename stem derived from the canonical URL (UTM + fragment stripped)."""
    return hashlib.sha256(canonical_resource_url(url).encode()).hexdigest()


def _resource_json_paths(url: str) -> list[Path]:
    """Candidate JSON paths for *url*, canonical key first then legacy aliases."""
    resource_dir = _resource_dir()
    raw = strip_utm_params(url or "")
    stems = [_url_stem(url)]
    legacy = hashlib.sha256(raw.encode()).hexdigest()
    if legacy not in stems:
        stems.append(legacy)
    return [resource_dir / f"{stem}.json" for stem in stems]


def has_resource(url: str) -> bool:
    """True if a FetchResult has been stored for *url*."""
    return any(path.exists() for path in _resource_json_paths(url))


def _read_resource_json(json_path: Path) -> FetchResult | None:
    if not json_path.exists():
        return None
    data: dict = json.loads(json_path.read_text(encoding="utf-8"))
    data.pop("cited_by", None)  # stored alongside but not part of FetchResult
    allowed = {f.name for f in fields(FetchResult)}
    bullets = data.get("summary_bullets")
    if bullets is None:
        bullets = []
    elif not isinstance(bullets, list):
        bullets = []
    filtered = {k: v for k, v in data.items() if k in allowed}
    filtered.setdefault("summary_bullets", [str(b) for b in bullets if str(b).strip()])
    result = FetchResult(**filtered)
    result.title = fix_mojibake(result.title)
    result.content = fix_mojibake(result.content)
    return result


def _has_mojibake(text: str) -> bool:
    return bool(text) and ("â" in text or "Ã" in text)


def _needs_resource_refresh(result: FetchResult) -> bool:
    if _has_mojibake(result.content) or _has_mojibake(result.title):
        return True
    url = result.resolved_url or result.url or ""
    if is_x_status_url(url) and _is_x_login_wall(result.content):
        return True
    return False


def load_resource(url: str) -> FetchResult | None:
    """Load a stored FetchResult for *url*, or ``None`` if not found."""
    stem = _url_stem(url)
    _hydrate_resource_from_object_store(stem)
    for json_path in _resource_json_paths(url):
        result = _read_resource_json(json_path)
        if result is not None:
            return result
    return None


def _linkedin_resource_prefix() -> str:
    raw = (os.environ.get("LINKEDIN_RESOURCE_PREFIX") or "linkedin/resources").strip()
    return raw.strip("/")


@lru_cache(maxsize=1)
def _object_store_config():
    try:
        from kg_vault.object_sync import (  # type: ignore[import-not-found]
            s3_store_config_from_env,
        )
    except ImportError:
        return None
    return s3_store_config_from_env(prefix=_linkedin_resource_prefix())


def _hydrate_resource_from_object_store(stem: str) -> None:
    cfg = _object_store_config()
    if cfg is None:
        return
    try:
        from kg_vault.object_sync import s3_download_file
    except ImportError:
        return
    resource_dir = _resource_dir()
    json_path = resource_dir / f"{stem}.json"
    if not json_path.exists():
        s3_download_file(cfg, f"{stem}.json", json_path)
    md_path = resource_dir / f"{stem}.md"
    if not md_path.exists():
        s3_download_file(cfg, f"{stem}.md", md_path)


def _mirror_resource_to_object_store(
    stem: str, json_path: Path, md_path: Path | None
) -> None:
    cfg = _object_store_config()
    if cfg is None:
        return
    try:
        from kg_vault.object_sync import s3_upload_file
    except ImportError:
        return
    s3_upload_file(cfg, json_path, f"{stem}.json")
    if md_path is not None and md_path.is_file():
        s3_upload_file(cfg, md_path, f"{stem}.md")


def refresh_resource_if_corrupt(url: str) -> FetchResult | None:
    """Load a resource, re-fetching from the web when mojibake survives repair."""
    result = load_resource(url)
    if result is None:
        return None
    if not _needs_resource_refresh(result):
        return result
    fetch_url = canonical_resource_url(result.resolved_url or result.url or url)
    logger.info("refreshing corrupted resource cache for %s", fetch_url)
    fresh = fetch_linked_content(fetch_url)
    if fresh.ok:
        save_resource(fetch_url, fresh)
        return load_resource(url) or fresh
    return result


def save_resource(
    url: str,
    result: FetchResult,
    *,
    citing_post_urns: list[str] | tuple[str, ...] = (),
) -> Path:
    """Persist *result* for *url*, keyed by canonical URL (UTM stripped).

    Writes:
    - ``{stem}.json``  — FetchResult dict + ``cited_by`` list (always)
    - ``{stem}.md``    — body text (only when content is non-empty)

    ``cited_by`` is merged with any existing entries so multiple posts citing
    the same resource accumulate rather than overwrite. ``result.images``
    (remote URLs) is stored as-is — not downloaded — see module docstring.
    """
    stem = _url_stem(url)
    resource_dir = _resource_dir()
    json_path = resource_dir / f"{stem}.json"

    existing_cited_by: list[str] = []
    if json_path.exists():
        try:
            existing_raw = json.loads(json_path.read_text(encoding="utf-8"))
            raw_cited = existing_raw.get("cited_by") or []
            # Normalize legacy raw-URN entries (stored before hash-conversion fix)
            existing_cited_by = [
                hashlib.sha256(e.encode()).hexdigest() if e.startswith("urn:") else e
                for e in raw_cited
            ]
            old_content = (existing_raw.get("content") or "").strip()
            new_content = (result.content or "").strip()
            if old_content and new_content and old_content != new_content:
                result = replace(
                    result,
                    tldr="",
                    summary_author="",
                    summary_bullets=[],
                    summary_model="",
                    summarized_at="",
                )
        except Exception:
            pass

    # Store content-store hashes (sha256(urn)) so cited_by entries are
    # directly usable as filenames: content/<hash>.md / content/<hash>.meta.json
    new_hashes = [hashlib.sha256(u.encode()).hexdigest() for u in citing_post_urns if u]
    data = asdict(result)
    data["url"] = canonical_resource_url(data["url"])
    resolved = (data.get("resolved_url") or data["url"] or "").strip()
    data["resolved_url"] = canonical_resource_url(resolved) if resolved else ""
    data["cited_by"] = list(dict.fromkeys(existing_cited_by + new_hashes))
    json_path.write_text(
        json.dumps(data, indent=0, ensure_ascii=False), encoding="utf-8"
    )

    md_path: Path | None = None
    if result.content:
        md_path = resource_dir / f"{stem}.md"
        md_path.write_text(result.content, encoding="utf-8")

    _mirror_resource_to_object_store(stem, json_path, md_path)
    return json_path


def _update_resource_cited_by(url: str, urns: list[str]) -> None:
    """Merge *urns* into the ``cited_by`` list of an already-stored resource."""
    json_path = next((p for p in _resource_json_paths(url) if p.exists()), None)
    if json_path is None or not urns:
        return
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        raw_existing = data.get("cited_by") or []
        # Normalize legacy raw-URN entries
        existing = [
            hashlib.sha256(e.encode()).hexdigest() if e.startswith("urn:") else e
            for e in raw_existing
        ]
        new_hashes = [hashlib.sha256(u.encode()).hexdigest() for u in urns if u]
        data["cited_by"] = list(dict.fromkeys(existing + new_hashes))
        json_path.write_text(
            json.dumps(data, indent=0, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core fetch logic
# ---------------------------------------------------------------------------


def fetch_linked_content(
    url: str,
    *,
    resolve_redirects: bool = True,
) -> FetchResult:
    """Fetch content from a linked URL using the appropriate strategy.

    1. Skips ignored URLs (LinkedIn profiles, hashtags, etc.).
    2. Resolves redirects (including ``lnkd.in`` short URLs).
    3. Dispatches to the registered strategy for the detected URL type.
    4. Falls back to ``_fetch_html_body`` for unknown types.

    Returns a :class:`FetchResult`; never raises.
    """
    if should_ignore_url(url):
        return FetchResult(url=url, error="ignored")

    resolved = resolve_redirect(url) if resolve_redirects else url
    fetch_url = rewrite_fetch_url(resolved)

    if should_ignore_url(fetch_url):
        return FetchResult(url=url, resolved_url=resolved, error="ignored")

    info = categorize_url(fetch_url)
    url_type = info.get("type") or "article"
    domain = info.get("domain") or ""

    if url_type in SKIP_TYPES:
        return FetchResult(
            url=url,
            resolved_url=resolved,
            url_type=url_type,
            domain=domain,
            error=f"skipped ({url_type})",
        )

    logger.info("GET %s [%s]", fetch_url, url_type)
    try:
        if "arxiv.org" in fetch_url.lower():
            title, content, images = _fetch_arxiv(fetch_url)
        elif is_x_status_url(fetch_url):
            title, content, images = _fetch_x_status(fetch_url)
        else:
            title, content, images = _strategy_for(url_type)(fetch_url)
        title = fix_mojibake(title)
        content = fix_mojibake(content)
        logger.info("  -> %s", title[:80] if title else "(no title)")
        if not (title.strip() or content.strip()):
            return FetchResult(
                url=url,
                resolved_url=resolved,
                url_type=url_type,
                domain=domain,
                error="empty content",
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
        if _CLOUDFLARE_TITLE_MARKER in title.lower() or any(
            m in content.lower() for m in _CLOUDFLARE_BODY_MARKERS
        ):
            return FetchResult(
                url=url,
                resolved_url=resolved,
                url_type=url_type,
                domain=domain,
                error="cloudflare challenge",
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
        return FetchResult(
            url=url,
            resolved_url=resolved,
            title=title,
            content=content,
            images=images,
            url_type=url_type,
            domain=domain,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        err = _format_fetch_error(exc)
        logger.warning("  -> failed: %s", err)
        return FetchResult(
            url=url,
            resolved_url=resolved,
            url_type=url_type,
            domain=domain,
            error=err,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )


def _process_one_url(
    url: str,
    *,
    skip_cached: bool = True,
    citing_post_urn: str = "",
) -> FetchResult:
    """Fetch (or load cached) a single URL and update ``cited_by`` when applicable."""
    urns = [citing_post_urn] if citing_post_urn else []
    if skip_cached and has_resource(url):
        cached = load_resource(url)
        if cached is not None:
            if urns:
                _update_resource_cited_by(url, urns)
            return cached
    result = fetch_linked_content(url)
    if result.ok:
        save_resource(url, result, citing_post_urns=urns)
    return result


def process_post_linked_content(
    urls: list[str],
    *,
    skip_cached: bool = True,
    citing_post_urn: str = "",
) -> list[FetchResult]:
    """Fetch and store content for a list of URLs extracted from a post.

    Args:
        urls: URLs to process (typically from post metadata).
        skip_cached: If True, skip URLs already in the resource store.
        citing_post_urn: URN of the post that references these URLs; recorded
            in ``cited_by`` so each resource tracks which posts linked to it.

    Returns:
        List of :class:`FetchResult` (including failures and skips).
    """
    results: list[FetchResult] = []
    for url in urls:
        results.append(
            _process_one_url(
                url, skip_cached=skip_cached, citing_post_urn=citing_post_urn
            )
        )
    return results


# ---------------------------------------------------------------------------
# Pipeline integration (streaming)
# ---------------------------------------------------------------------------


def fetch_linked_content_streaming(
    limit: int | None = None,
    skip_cached: bool = True,
    urns: set[str] | None = None,
):
    """
    Generator for pipeline use. Yields (urls_done, total_urls) after each URL.

    Only processes URLs from posts whose URN is in ``urns`` (if provided), and
    skips URLs already in the resource store when ``skip_cached`` is True.
    Deduplicates by canonical URL (UTM stripped) so variants of the same resource
    are fetched once and accumulate all citing URNs in ``cited_by``.

    Returns total URLs successfully fetched via StopIteration.value.
    """
    # Collect canonical_url → (first_raw_url, [citing_urns])
    url_to_urns: dict[str, list[str]] = {}
    canonical_to_raw: dict[str, str] = {}
    for post_urn, post_urls in _iter_posts_with_urls(urns=urns):
        for url in post_urls:
            canon = canonical_resource_url(url)
            if canon not in url_to_urns:
                url_to_urns[canon] = []
                canonical_to_raw[canon] = url
            if post_urn and post_urn not in url_to_urns[canon]:
                url_to_urns[canon].append(post_urn)

    # For already-cached resources update cited_by; collect the rest to fetch.
    jobs: list[tuple[str, list[str]]] = []
    for canon, citing_urns in url_to_urns.items():
        raw_url = canonical_to_raw[canon]
        if skip_cached and has_resource(raw_url):
            if citing_urns:
                _update_resource_cited_by(raw_url, citing_urns)
        else:
            jobs.append((raw_url, citing_urns))

    if limit:
        jobs = jobs[:limit]

    urls_fetched = 0
    for i, (url, citing_urns) in enumerate(jobs):
        result = fetch_linked_content(url)
        if result.ok:
            save_resource(url, result, citing_post_urns=citing_urns)
            urls_fetched += 1
        yield i + 1, len(jobs)
    return urls_fetched


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _urls_from_metadata(meta: dict) -> list[str]:
    """``urls`` plus mention profile/company links (for fetching linked pages)."""
    out: list[str] = []
    seen: set[str] = set()
    for u in meta.get("urls") or []:
        s = str(u).strip()
        if s and s not in seen and not should_ignore_url(s):
            seen.add(s)
            out.append(s)
    for m in meta.get("mentions") or []:
        if not isinstance(m, dict):
            continue
        u = str(m.get("url") or "").strip()
        if u and u not in seen and not should_ignore_url(u):
            seen.add(u)
            out.append(u)
    return out


def _iter_posts_with_urls(urns: set[str] | None = None):
    """Yield (urn, urls) for posts that have URLs.

    Args:
        urns: When provided, only posts whose URN is in this set are yielded.
              Pass the set of URNs from the current fetch period to avoid
              re-processing the entire content store every run.

    First checks ``urls`` and ``mentions`` in ``.meta.json``; if empty, falls back to
    extracting URLs from the ``.md`` content file and persists them so future
    runs skip re-extraction.
    """
    content_dir = _content_dir()
    registry = _load_registry()

    for meta_path in sorted(content_dir.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        stem = meta_path.name.replace(".meta.json", "")
        urn = registry.get(stem, "")

        if urns is not None and urn not in urns:
            continue

        urls = _urls_from_metadata(meta)
        if not urls and urn:
            md_path = content_dir / f"{stem}.md"
            if md_path.exists():
                text = md_path.read_text(encoding="utf-8")
                extracted = [
                    u for u in extract_urls_from_text(text) if not should_ignore_url(u)
                ]
                if extracted:
                    update_urls_metadata(urn, extracted)
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        pass
                    else:
                        urls = _urls_from_metadata(meta)

        if not urls:
            continue
        yield urn, urls


def _collect_post_url_jobs(limit_posts: int | None = None) -> list[tuple[str, str]]:
    """Return ``(citing_post_urn, url)`` pairs in post order for CLI progress."""
    jobs: list[tuple[str, str]] = []
    posts = 0
    for urn, urls in _iter_posts_with_urls():
        if limit_posts is not None and posts >= limit_posts:
            break
        for url in urls:
            jobs.append((urn, url))
        posts += 1
    return jobs


def _record_fetch_result(
    res: FetchResult,
    *,
    quiet: bool,
    verbose: bool,
    counts: dict[str, int],
) -> None:
    """Update *counts* and optionally print one result line."""
    if res.error == "ignored" or res.error.startswith("skipped"):
        counts["skipped"] += 1
        if verbose and not quiet:
            print(f"   ⏭  {res.url}  ({res.error})")
    elif res.ok:
        counts["fetched"] += 1
        if verbose and not quiet:
            print(
                f"   ✅ {res.resolved_url or res.url}  [{res.url_type}] {res.title!r}"
            )
    else:
        counts["failed"] += 1
        line = f"   ❌ {res.url}  {res.error}"
        if verbose and not quiet:
            print(line)
        else:
            tqdm.write(line, file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch content from URLs linked in LinkedIn posts/comments.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of posts to process (for testing).",
    )
    parser.add_argument(
        "--skip-cached",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip URLs already in the resource store (default: on).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be fetched without actually fetching.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Hide successes and skips (still shows failures and progress).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Per-post/per-URL lines and INFO fetch logs (disables progress bar).",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable the progress bar (for logs or scripting).",
    )
    args = parser.parse_args()

    if args.quiet and args.verbose:
        print("Use only one of --quiet or --verbose", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    counts = {"fetched": 0, "skipped": 0, "failed": 0}

    if args.dry_run:
        posts_processed = 0
        for urn, urls in _iter_posts_with_urls():
            if args.limit and posts_processed >= args.limit:
                break
            label = urn or "(unknown URN)"
            print(f"\n📄 {label}  ({len(urls)} URL(s))")
            for url in urls:
                cached = " [cached]" if has_resource(url) else ""
                print(f"   {url}{cached}")
            posts_processed += 1
        if posts_processed == 0:
            total_posts = sum(1 for _ in _content_dir().glob("*.meta.json"))
            if total_posts == 0:
                print(
                    f"\n⚠️  No posts in the content store ({_content_dir()}). "
                    "Run collect+enrich first, e.g. `linkedin-pipeline --last 7d`."
                )
            else:
                print(
                    f"\n⚠️  {total_posts} post(s) in the content store, "
                    "but none have extractable URLs."
                )
        print(f"\n✨ Done — {posts_processed} post(s) listed (dry run).")
        return 0

    jobs = _collect_post_url_jobs(limit_posts=args.limit)
    posts_processed = len({urn for urn, _ in jobs})
    use_progress = not args.no_progress and not args.verbose

    if args.verbose:
        current_urn: str | None = None
        for urn, url in jobs:
            if urn != current_urn:
                current_urn = urn
                label = urn or "(unknown URN)"
                print(f"\n📄 {label}")
            res = _process_one_url(
                url, skip_cached=args.skip_cached, citing_post_urn=urn
            )
            _record_fetch_result(res, quiet=args.quiet, verbose=True, counts=counts)
    else:
        bar = tqdm(
            jobs,
            desc="Fetching URLs",
            unit="url",
            disable=not use_progress,
            file=sys.stderr,
        )
        for urn, url in bar:
            res = _process_one_url(
                url, skip_cached=args.skip_cached, citing_post_urn=urn
            )
            _record_fetch_result(res, quiet=args.quiet, verbose=False, counts=counts)
            if use_progress:
                bar.set_postfix(
                    ok=counts["fetched"],
                    skip=counts["skipped"],
                    fail=counts["failed"],
                    refresh=False,
                )

    print(
        f"\n✨ Done — {posts_processed} post(s) processed, "
        f"{counts['fetched']} URL(s) fetched, "
        f"{counts['skipped']} skipped, "
        f"{counts['failed']} failed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

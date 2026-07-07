"""
Fetch content from URLs linked in LinkedIn posts/comments.

Pluggable extractor with strategy dispatch by URL type.

Body-fetch backend selectable via ``LINKEDIN_EXTRACTOR``:
  - ``httpx``  (default) — requests + BeautifulSoup body extraction.
  - ``tavily`` — Tavily Extract API; handles JS-rendered / Cloudflare-gated
    pages the httpx backend can't. Falls back to httpx if ``TAVILY_API_KEY``
    isn't configured. See ``TAVILY_EXTRACT_DEPTH`` (basic|advanced).
Metadata-only for YouTube/GitHub/podcasts regardless of backend.

Storage
-------
Fetched resource content is persisted in the *resource store*:
  ``get_data_dir() / "resources/"``
Each URL is identified by the SHA-256 hash of its (resolved) URL.
  - ``{hash}.json``  — FetchResult (including title and body text)
  - ``{hash}.md``    — body text as plain text / future Markdown

Typical pipeline
----------------
1. Post content (from API or HTTP fetch) is stored in the content store.
2. ``enrich_activities`` populates ``meta.json`` ``urls`` field.
3. This module reads those URLs and fetches their content.

CLI
---
  uv run python -m linkedin_api.fetch_linked_content          # all posts
  uv run python -m linkedin_api.fetch_linked_content --limit 5
  uv run python -m linkedin_api.fetch_linked_content --dry-run

  LINKEDIN_EXTRACTOR=tavily uv run python -m linkedin_api.fetch_linked_content

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
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests
from bs4 import BeautifulSoup

from linkedin_api.activity_csv import get_data_dir
from linkedin_api.content_store import (
    _content_dir,
    _load_registry,
    update_urls_metadata,
)
from linkedin_api.utils.auth import get_secret
from linkedin_api.utils.urls import (
    categorize_url,
    extract_urls_from_text,
    resolve_redirect,
    should_ignore_url,
    strip_utm_params,
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
    url_type: str = ""
    domain: str = ""
    error: str = ""
    fetched_at: str = ""

    @property
    def ok(self) -> bool:
        """True if at least a title or content was retrieved without error."""
        return bool((self.content or self.title) and not self.error)


# ---------------------------------------------------------------------------
# Strategy type alias
# ---------------------------------------------------------------------------

FetchStrategy = Callable[[str], tuple[str, str]]  # returns (title, content)

# ---------------------------------------------------------------------------
# Concrete strategies
# ---------------------------------------------------------------------------


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB — skip pathologically large pages


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
    html = raw.decode(resp.encoding or "utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:title")
    title = (
        str(og["content"]).strip()
        if og and og.get("content")
        else (soup.title.get_text(strip=True) if soup.title else "")
    )
    return soup, title


def _fetch_html_body(url: str) -> tuple[str, str]:
    """Extract title and body text, stripping nav/chrome noise."""
    soup, title = _fetch_soup(url)
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    body = soup.find("body") or soup
    lines = [
        ln.strip() for ln in body.get_text(separator="\n").splitlines() if ln.strip()
    ]
    return title, "\n".join(lines)


def _fetch_metadata_only(url: str) -> tuple[str, str]:
    """Title-only fetch (og:title / <title>); body extraction deferred.

    Used for video platforms (YouTube), code repositories (GitHub), etc.
    """
    _, title = _fetch_soup(url, timeout=(5, 10))
    return title, ""


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


def _strip_linkedin_guest_preamble(content: str, url: str) -> str:
    """No-op for non-LinkedIn URLs, or pages that don't match the guest-view
    post heading (e.g. articles, profile pages) — safe to call unconditionally."""
    if "linkedin.com" not in url:
        return content
    match = _LINKEDIN_POST_HEADING_RE.search(content)
    if not match:
        return content
    return content[match.start() :]


def _fetch_tavily(url: str) -> tuple[str, str]:
    """Extract title/body via Tavily's Extract API.

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
    response = client.extract(urls=[url], extract_depth=depth, format="markdown")

    failed = response.get("failed_results") or []
    if failed:
        reason = failed[0].get("error") or "extraction failed"
        raise ValueError(f"tavily: {reason}")

    results = response.get("results") or []
    if not results:
        raise ValueError("tavily: no content extracted")

    result = results[0]
    content = _strip_linkedin_guest_preamble(str(result.get("raw_content") or ""), url)
    title = str(result.get("title") or "") or _title_from_markdown(content)
    return title, content


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

# ---------------------------------------------------------------------------
# Resource store (keyed by SHA-256 of the resolved URL)
# ---------------------------------------------------------------------------


def _resource_dir() -> Path:
    """Return (and create) the resource storage directory."""
    d = get_data_dir() / "resources"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _url_stem(url: str) -> str:
    """Stable filename stem derived from the canonical URL (UTM params stripped)."""
    return hashlib.sha256(strip_utm_params(url).encode()).hexdigest()


def has_resource(url: str) -> bool:
    """True if a FetchResult has been stored for *url*."""
    return (_resource_dir() / f"{_url_stem(url)}.json").exists()


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
    the same resource accumulate rather than overwrite.
    """
    stem = _url_stem(url)
    resource_dir = _resource_dir()
    json_path = resource_dir / f"{stem}.json"

    existing_cited_by: list[str] = []
    if json_path.exists():
        try:
            raw_cited = (
                json.loads(json_path.read_text(encoding="utf-8")).get("cited_by") or []
            )
            # Normalize legacy raw-URN entries (stored before hash-conversion fix)
            existing_cited_by = [
                hashlib.sha256(e.encode()).hexdigest() if e.startswith("urn:") else e
                for e in raw_cited
            ]
        except Exception:
            pass

    # Store content-store hashes (sha256(urn)) so cited_by entries are
    # directly usable as filenames: content/<hash>.md / content/<hash>.meta.json
    new_hashes = [hashlib.sha256(u.encode()).hexdigest() for u in citing_post_urns if u]
    data = asdict(result)
    # Strip UTM from url/resolved_url — the file is keyed by canonical URL anyway
    data["url"] = strip_utm_params(data["url"])
    data["resolved_url"] = strip_utm_params(data["resolved_url"])
    data["cited_by"] = list(dict.fromkeys(existing_cited_by + new_hashes))
    json_path.write_text(
        json.dumps(data, indent=0, ensure_ascii=False), encoding="utf-8"
    )

    if result.content:
        md_path = resource_dir / f"{stem}.md"
        md_path.write_text(result.content, encoding="utf-8")

    return json_path


def _update_resource_cited_by(url: str, urns: list[str]) -> None:
    """Merge *urns* into the ``cited_by`` list of an already-stored resource."""
    json_path = _resource_dir() / f"{_url_stem(url)}.json"
    if not json_path.exists() or not urns:
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


def load_resource(url: str) -> FetchResult | None:
    """Load a stored FetchResult for *url*, or ``None`` if not found."""
    json_path = _resource_dir() / f"{_url_stem(url)}.json"
    if not json_path.exists():
        return None
    data: dict = json.loads(json_path.read_text(encoding="utf-8"))
    data.pop("cited_by", None)  # stored alongside but not part of FetchResult
    return FetchResult(**data)


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
    info = categorize_url(resolved)
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

    logger.info("GET %s [%s]", resolved, url_type)
    strategy = _strategy_for(url_type)
    try:
        title, content = strategy(resolved)
        logger.info("  -> %s", title[:80] if title else "(no title)")
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
            url_type=url_type,
            domain=domain,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        logger.warning("  -> failed: %s", exc)
        return FetchResult(
            url=url,
            resolved_url=resolved,
            url_type=url_type,
            domain=domain,
            error=str(exc),
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )


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
    urns = [citing_post_urn] if citing_post_urn else []
    results: list[FetchResult] = []
    for url in urls:
        if skip_cached and has_resource(url):
            cached = load_resource(url)
            if cached is not None:
                if urns:
                    _update_resource_cited_by(url, urns)
                results.append(cached)
                continue
        result = fetch_linked_content(url)
        if result.ok:
            save_resource(url, result, citing_post_urns=urns)
        results.append(result)
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
            canon = strip_utm_params(url)
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
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    for m in meta.get("mentions") or []:
        if not isinstance(m, dict):
            continue
        u = str(m.get("url") or "").strip()
        if u and u not in seen:
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
    args = parser.parse_args()

    posts_processed = 0
    urls_fetched = 0
    urls_failed = 0
    urls_skipped = 0

    for urn, urls in _iter_posts_with_urls():
        if args.limit and posts_processed >= args.limit:
            break

        label = urn or "(unknown URN)"
        print(f"\n📄 {label}  ({len(urls)} URL(s))")

        if args.dry_run:
            for url in urls:
                cached = " [cached]" if has_resource(url) else ""
                print(f"   {url}{cached}")
            posts_processed += 1
            continue

        results = process_post_linked_content(urls, skip_cached=args.skip_cached)
        for res in results:
            if res.error == "ignored" or res.error.startswith("skipped"):
                urls_skipped += 1
                print(f"   ⏭  {res.url}  ({res.error})")
            elif res.ok:
                urls_fetched += 1
                print(
                    f"   ✅ {res.resolved_url or res.url}  [{res.url_type}] {res.title!r}"
                )
            else:
                urls_failed += 1
                print(f"   ❌ {res.url}  {res.error}")

        posts_processed += 1

    print(
        f"\n✨ Done — {posts_processed} post(s) processed, "
        f"{urls_fetched} URL(s) fetched, "
        f"{urls_skipped} skipped, "
        f"{urls_failed} failed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

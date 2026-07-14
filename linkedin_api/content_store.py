"""File-based content storage for post/comment text (Markdown).

Stores the full content of any post or comment by URN, including both
the user's own content and other people's posts they interacted with.
The user's own text is also in the CSV ``content`` column, but the
content store is the canonical source for enrichment and indexing.

Files are stored under ``get_data_dir() / "content/"`` as Markdown,
named by ``post_id`` (``{post_id}.md``). Legacy rows without ``post_id``
fall back to SHA-256 of a post URN.

Content sourcing priority (handled by callers):
1. Portability API text (available for own content at extraction time)
2. ``requests`` + HTML-to-Markdown for public posts
(URLs requiring login are not enriched.)

Phase 3 metadata (summary, topics, etc.) stored as ``{post_id}.meta.json`` sidecar.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, cast

from linkedin_api.activity_csv import get_data_dir
from linkedin_api.content_keys import storage_key
from linkedin_api.utils.urls import resolve_redirect, strip_utm_params


def _content_dir() -> Path:
    """Return (and create) the content storage directory."""
    d = get_data_dir() / "content"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _legacy_stem_from_urn(urn: str) -> str:
    return hashlib.sha256(urn.encode()).hexdigest()


def _lookup_stems(post_id: str = "", *, post_urn: str = "") -> list[str]:
    """Candidate stems for read paths (post_id first, then legacy URN hash)."""
    stems: list[str] = []
    stem, pu = storage_key(post_id, post_urn=post_urn)
    if stem:
        stems.append(stem)
    u = (post_urn or "").strip()
    if u:
        legacy = _legacy_stem_from_urn(u)
        if legacy not in stems:
            stems.append(legacy)
    return stems


def _meta_path_for_stem(stem: str) -> Path:
    return _content_dir() / f"{stem}.meta.json"


def _meta_path(post_id: str = "", *, post_urn: str = "") -> Path:
    stem, _ = storage_key(post_id, post_urn=post_urn)
    return _meta_path_for_stem(stem)


def save_content(
    post_id: str,
    text: str,
    *,
    post_urn: str = "",
) -> Path:
    """Persist *text* for the post identified by *post_id*. Returns the file path written."""
    stem, pu = storage_key(post_id, post_urn=post_urn)
    if not stem or not text:
        raise ValueError("post_id (or post_urn) and text must be non-empty")
    path = _content_dir() / f"{stem}.md"
    path.write_text(text, encoding="utf-8")
    _register_post(stem, pu)
    return path


def _images_dir() -> Path:
    d = _content_dir() / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_image_to_store(url: str) -> str | None:
    """
    Download *url* to ``content/images/``; return a path relative to the
    content directory (e.g. ``"images/abc123.jpg"``) or ``None`` on failure.

    Uses a URL-hash filename so repeated calls for the same URL are no-ops.
    LinkedIn CDN images have a very long expiry but downloading preserves them
    offline and guards against future URL changes.
    """
    import urllib.parse

    try:
        import requests as _req
    except ImportError:
        return None

    url = (url or "").strip()
    if not url:
        return None

    images_dir = _images_dir()
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:24]
    parsed = urllib.parse.urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        suffix = ".jpg"
    filename = f"{url_hash}{suffix}"
    local_path = images_dir / filename
    if local_path.exists():
        return f"images/{filename}"
    try:
        resp = _req.get(
            url,
            timeout=15,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        if resp.status_code == 200 and resp.content:
            local_path.write_bytes(resp.content)
            return f"images/{filename}"
    except Exception:
        pass
    return None


def _comments_path(post_id: str = "", *, post_urn: str = "") -> Path:
    stem, _ = storage_key(post_id, post_urn=post_urn)
    return _content_dir() / f"{stem}.comments.json"


def save_comments(
    post_id: str,
    total_count: int,
    comments: list[dict[str, Any]],
    *,
    post_urn: str = "",
) -> Path | None:
    """
    Persist comment preview data as a ``{post_id}.comments.json`` sidecar.

    ``total_count`` is LinkedIn's reported total (may exceed ``len(comments)``).
    Each comment dict should have: ``author``, ``author_url``, ``timestamp``,
    ``text``, ``likes``.

    Returns ``None`` (no write) when ``comments`` is empty.
    """
    stem, pu = storage_key(post_id, post_urn=post_urn)
    if not stem or not comments:
        return None
    payload: dict[str, Any] = {
        "post_urn": pu,
        "post_id": (post_id or "").strip() or stem,
        "total_count": total_count,
        "comments": comments,
    }
    path = _content_dir() / f"{stem}.comments.json"
    path.write_text(json.dumps(payload, indent=0, ensure_ascii=False), encoding="utf-8")
    return path


def load_comments(post_id: str = "", *, post_urn: str = "") -> dict[str, Any] | None:
    """Load comment sidecar for the post, or ``None`` if not present."""
    for stem in _lookup_stems(post_id, post_urn=post_urn):
        path = _content_dir() / f"{stem}.comments.json"
        if path.exists():
            return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    return None


def has_comments(post_id: str = "", *, post_urn: str = "") -> bool:
    """True if a comment sidecar exists for the post."""
    return any(
        (_content_dir() / f"{stem}.comments.json").exists()
        for stem in _lookup_stems(post_id, post_urn=post_urn)
    )


def load_content(post_id: str = "", *, post_urn: str = "") -> str | None:
    """Load stored content for the post, or ``None`` if not found."""
    for stem in _lookup_stems(post_id, post_urn=post_urn):
        path = _content_dir() / f"{stem}.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
    return None


def has_content(post_id: str = "", *, post_urn: str = "") -> bool:
    """Return ``True`` if content has been stored for the post."""
    return any(
        (_content_dir() / f"{stem}.md").exists()
        for stem in _lookup_stems(post_id, post_urn=post_urn)
    )


def content_path(post_id: str = "", *, post_urn: str = "") -> Path:
    """Return the file path where content for the post would be stored."""
    stem, _ = storage_key(post_id, post_urn=post_urn)
    return _content_dir() / f"{stem}.md"


# --- Phase 3 metadata (summary, topics, etc.) ---

_META_KEYS = (
    "summary",
    "topics",
    "technologies",
    "people",
    "category",
    "urls",
    "mentions",
    "hashtags",
    "images",
    "post_url",
    "post_urn",
    "post_author",
    "post_author_url",
    "post_id",
    "activities_ids",
    "summarized_at",
    "activity_time_iso",
    "post_created_at",
    "enrichment_version",
    "tldr",
    "summary_bullets",
    "summary_model",
    "tags",
)


def _merge_mentions(
    previous: list[dict[str, Any]] | None, incoming: list[dict[str, Any]] | None
) -> list[dict[str, str]]:
    """Union by ``url``; prefer non-empty ``name`` when merging."""
    by_url: dict[str, dict[str, str]] = {}
    for group in (previous or [], incoming or []):
        for raw in group:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url") or "").strip()
            if not url:
                continue
            name = str(raw.get("name") or "").strip()
            if url not in by_url:
                by_url[url] = {"name": name, "url": url}
            elif name and not (by_url[url].get("name") or "").strip():
                by_url[url]["name"] = name
    return list(by_url.values())


def _merge_hashtags(
    previous: list[Any] | None, incoming: list[Any] | None
) -> list[str]:
    prev = {str(x).strip() for x in (previous or []) if x and str(x).strip()}
    inc = {str(x).strip() for x in (incoming or []) if x and str(x).strip()}
    return sorted(prev | inc)


def resolve_urls_for_metadata(urls: list[str] | None) -> list[str]:
    """Return unique URLs after best-effort redirect resolution (see ``resolve_redirect``).

    Deduplicates by canonical URL (UTM params stripped) so two links to the same
    article with different campaign tags produce a single entry.
    """
    if not urls:
        return []
    out: list[str] = []
    seen_canon: set[str] = set()
    for u in urls:
        s = (u or "").strip()
        if not s:
            continue
        canon = strip_utm_params(s)
        if canon in seen_canon:
            continue
        seen_canon.add(canon)
        try:
            resolved = resolve_redirect(s)
        except Exception:
            resolved = s
        canon_resolved = strip_utm_params(resolved)
        if canon_resolved not in seen_canon:
            seen_canon.add(canon_resolved)
        out.append(resolved)
    return out


def _ms_to_iso(ts_ms: int | float | None) -> str:
    """Convert epoch ms to ISO string (human-readable)."""
    if ts_ms is None:
        return ""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def _normalize_activity_time_iso(meta: dict) -> str:
    """Return activity_time_iso (ISO). Backward compat: reaction_created_at, reaction_timestamp_ms."""
    iso = (
        meta.get("activity_time_iso") or meta.get("reaction_created_at") or ""
    ).strip()
    if iso:
        return iso
    ts = meta.get("reaction_timestamp_ms")
    if ts is not None and isinstance(ts, (int, float)):
        return _ms_to_iso(int(ts))
    return ""


def _iso_to_ms(iso_str: str | None) -> int | None:
    """Parse ISO string to epoch ms for sorting. Returns None if invalid."""
    s = (iso_str or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def save_metadata(
    post_id: str,
    summary: Optional[str] = None,
    topics: Optional[list[str]] = None,
    technologies: Optional[list[str]] = None,
    people: Optional[list[str]] = None,
    category: Optional[str] = None,
    urls: list[str] | None = None,
    post_url: str = "",
    *,
    post_urn: str = "",
    **extra: Any,
) -> Path:
    """Save metadata for the post identified by *post_id*.

    Merges with any existing file: ``activities_ids`` are unioned in order;
    ``post_id``, ``post_urn``, and ``post_author_url`` are kept from the
    previous file when the new values are empty. ``urls`` are de-duplicated
    and passed through ``resolve_urls_for_metadata``.
    """
    stem, pu = storage_key(post_id, post_urn=post_urn)
    existing = dict(load_metadata(post_id, post_urn=post_urn) or {})
    from_extra = {k: v for k, v in extra.items() if k in _META_KEYS}
    meta: dict[str, Any] = {
        "summary": summary if summary is not None else "",
        "topics": topics if topics is not None else [],
        "technologies": technologies if technologies is not None else [],
        "people": people if people is not None else [],
        "category": category if category is not None else "",
        "urls": urls or [],
        "mentions": [],
        "hashtags": [],
        "images": [],
        "post_url": post_url or "",
        "post_urn": "",
        "post_author": "",
        "post_author_url": "",
        "post_id": (post_id or "").strip() or stem,
        "activities_ids": [],
        "summarized_at": existing.get("summarized_at") or "",
        "activity_time_iso": "",
        "post_created_at": "",
        "enrichment_version": "",
    }
    meta.update({k: v for k, v in existing.items() if k in _META_KEYS})
    meta.update(from_extra)
    if pu and not (str(meta.get("post_urn") or "")).strip():
        meta["post_urn"] = pu
    if summary is not None:
        meta["summary"] = summary
    if topics is not None:
        meta["topics"] = topics
    if technologies is not None:
        meta["technologies"] = technologies
    if people is not None:
        meta["people"] = people
    if category is not None:
        meta["category"] = category or ""
    # Resolve incoming URLs (HTTP redirect-following + canonical dedup).
    # Then merge with existing and re-dedup by canonical so that:
    #   (a) existing duplicates are cleaned up, and
    #   (b) new URLs that canonicalise to an already-stored URL are dropped.
    existing_urls: list[str] = existing.get("urls") or []
    resolved_new = resolve_urls_for_metadata(urls or [])
    seen_canon: set[str] = set()
    merged: list[str] = []
    for u in existing_urls + resolved_new:
        c = strip_utm_params(u)
        if c not in seen_canon:
            seen_canon.add(c)
            merged.append(u)
    meta["urls"] = merged
    prev_mentions = (
        existing.get("mentions") if isinstance(existing.get("mentions"), list) else None
    )
    prev_hashtags = existing.get("hashtags")
    meta["mentions"] = _merge_mentions(
        prev_mentions,
        meta.get("mentions") if isinstance(meta.get("mentions"), list) else None,
    )
    meta["hashtags"] = _merge_hashtags(
        prev_hashtags if isinstance(prev_hashtags, list) else None,
        meta.get("hashtags") if isinstance(meta.get("hashtags"), list) else None,
    )
    prev_images = existing.get("images")
    inc_images = meta.get("images")
    if isinstance(prev_images, list) and isinstance(inc_images, list):
        meta["images"] = list(
            dict.fromkeys(str(x) for x in prev_images + inc_images if x)
        )
    elif isinstance(inc_images, list):
        meta["images"] = [str(x) for x in inc_images if x]
    elif isinstance(prev_images, list):
        meta["images"] = [str(x) for x in prev_images if x]
    else:
        meta["images"] = []
    meta["post_url"] = post_url or meta.get("post_url") or ""

    prev_ids = existing.get("activities_ids") or []
    if not isinstance(prev_ids, list):
        prev_ids = [str(prev_ids)]
    else:
        prev_ids = [str(x) for x in prev_ids if x]
    new_ids = meta.get("activities_ids") or []
    if not isinstance(new_ids, list):
        new_ids = [str(new_ids)]
    else:
        new_ids = [str(x) for x in new_ids if x]
    meta["activities_ids"] = list(dict.fromkeys(prev_ids + new_ids))

    for k in ("post_id", "post_urn", "post_author_url"):
        if not (str(meta.get(k) or "")).strip() and existing.get(k):
            meta[k] = existing[k]

    if not (str(meta.get("post_created_at") or "")).strip() and existing.get(
        "post_created_at"
    ):
        meta["post_created_at"] = existing["post_created_at"]
    if not (str(meta.get("activity_time_iso") or "")).strip() and existing.get(
        "activity_time_iso"
    ):
        meta["activity_time_iso"] = existing["activity_time_iso"]
    if not (str(meta.get("enrichment_version") or "")).strip() and existing.get(
        "enrichment_version"
    ):
        meta["enrichment_version"] = existing["enrichment_version"]

    path = _meta_path_for_stem(stem)
    path.write_text(json.dumps(meta, indent=0), encoding="utf-8")
    _register_post(stem, pu)
    return path


def update_urls_metadata(post_id: str, urls: list[str], *, post_urn: str = "") -> Path:
    """Update only the ``urls`` field in metadata, preserving all other fields.

    Creates a minimal metadata record if none exists yet. URLs are resolved
    via ``resolve_urls_for_metadata``.
    """
    meta = dict(load_metadata(post_id, post_urn=post_urn) or {})
    meta["urls"] = resolve_urls_for_metadata(urls)
    stem, _ = storage_key(post_id, post_urn=post_urn)
    path = _meta_path_for_stem(stem)
    path.write_text(json.dumps(meta, indent=0), encoding="utf-8")
    return path


def update_metadata_fields(post_id: str, *, post_urn: str = "", **kwargs: Any) -> Path:
    """Merge specified metadata fields, preserving others. Only _META_KEYS are applied."""
    meta = dict(load_metadata(post_id, post_urn=post_urn) or {})
    for k, v in kwargs.items():
        if k in _META_KEYS:
            meta[k] = v
    stem, _ = storage_key(post_id, post_urn=post_urn)
    path = _meta_path_for_stem(stem)
    path.write_text(json.dumps(meta, indent=0), encoding="utf-8")
    return path


def merge_enrichment_activity(
    post_id: str,
    *,
    post_urn: str = "",
    activity_id: str = "",
    post_url: str = "",
    activity_time_iso: str = "",
) -> Path | None:
    """
    Union ``activity_id`` into ``activities_ids``; fill empty ``post_url`` /
    ``activity_time_iso`` from the current CSV row when missing.

    Returns ``None`` if there is no metadata or nothing would change.
    """
    existing = load_metadata(post_id, post_urn=post_urn)
    if existing is None:
        return None
    meta = dict(existing)
    prev = meta.get("activities_ids") or []
    if not isinstance(prev, list):
        prev = [str(prev)] if prev else []
    else:
        prev = [str(x) for x in prev if x]
    aid = (activity_id or "").strip()
    merged = list(dict.fromkeys(prev + ([aid] if aid else [])))
    changed = merged != prev
    if merged != prev:
        meta["activities_ids"] = merged
    if (post_url or "").strip() and not (str(meta.get("post_url") or "")).strip():
        meta["post_url"] = post_url.strip()
        changed = True
    if (activity_time_iso or "").strip() and not (
        str(meta.get("activity_time_iso") or "")
    ).strip():
        meta["activity_time_iso"] = activity_time_iso.strip()
        changed = True
    if not changed:
        return None
    stem, _ = storage_key(post_id, post_urn=post_urn)
    path = _meta_path_for_stem(stem)
    path.write_text(json.dumps(meta, indent=0), encoding="utf-8")
    return path


def merge_post_identity(
    post_id: str,
    *,
    post_urn: str = "",
    extra_activity_ids: list[str] | None = None,
) -> Path | None:
    """
    Fill identity fields from CSV without touching summary/topics or re-resolving ``urls``.

    - Sets ``post_id`` / ``post_urn`` when currently empty.
    - Unions ``activities_ids`` with *extra_activity_ids* (order preserved, de-duplicated).

    Returns ``None`` if there is no metadata file or nothing would change.
    """
    existing = load_metadata(post_id, post_urn=post_urn)
    if existing is None:
        return None
    meta = dict(existing)
    pid = (post_id or "").strip()
    if pid and not (str(meta.get("post_id") or "")).strip():
        meta["post_id"] = pid
    pu = (post_urn or "").strip()
    if pu and not (str(meta.get("post_urn") or "")).strip():
        meta["post_urn"] = pu

    prev = meta.get("activities_ids") or []
    if not isinstance(prev, list):
        prev = [str(prev)] if prev else []
    else:
        prev = [str(x) for x in prev if x]
    extra = extra_activity_ids or []
    extra = [str(x) for x in extra if x]
    meta["activities_ids"] = list(dict.fromkeys(prev + extra))

    if meta == existing:
        return None

    stem, _ = storage_key(post_id, post_urn=post_urn)
    path = _meta_path_for_stem(stem)
    path.write_text(json.dumps(meta, indent=0), encoding="utf-8")
    return path


def update_summary_metadata(
    post_id: str,
    summary: str,
    topics: list[str],
    technologies: list[str] | None = None,
    people: list[str] | None = None,
    category: str | None = None,
    *,
    post_urn: str = "",
    tldr: str = "",
    summary_bullets: list[str] | None = None,
    summary_model: str = "",
    tags: list[str] | None = None,
) -> Path:
    """Update metadata with LLM summary. Preserves urls, post_url from enrichment.

    ``technologies``/``people``/``category`` are left as-is when omitted
    (``None``) rather than force-written to empty, so a caller that doesn't
    fill one of them doesn't wipe out anything a prior run stored there.
    Pass an explicit value (including ``[]``/``""``) to overwrite. Note:
    ``people`` (LLM-extracted names/companies, including ones with no DOM
    link) is intentionally separate from ``mentions`` (DOM-scraped profile
    links) — the two are complementary, not duplicates.
    """
    meta = dict(load_metadata(post_id, post_urn=post_urn) or {})
    meta["summary"] = summary
    meta["topics"] = topics
    meta["technologies"] = (
        technologies if technologies is not None else meta.get("technologies", [])
    )
    meta["people"] = people if people is not None else meta.get("people", [])
    meta["category"] = category if category is not None else meta.get("category", "")
    meta["tldr"] = (tldr or "").strip()
    meta["summary_bullets"] = list(summary_bullets or [])
    meta["summary_model"] = (summary_model or "").strip()
    if tags is not None:
        meta["tags"] = [str(t).strip() for t in tags if str(t).strip()]
    meta["summarized_at"] = datetime.now(timezone.utc).isoformat()
    stem, _ = storage_key(post_id, post_urn=post_urn)
    path = _meta_path_for_stem(stem)
    path.write_text(json.dumps(meta, indent=0), encoding="utf-8")
    return path


def load_metadata(post_id: str = "", *, post_urn: str = "") -> dict[str, Any] | None:
    """Load metadata for the post, or None if not found."""
    for stem in _lookup_stems(post_id, post_urn=post_urn):
        path = _meta_path_for_stem(stem)
        if path.exists():
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return data
    return None


def has_metadata(post_id: str = "", *, post_urn: str = "") -> bool:
    """True if metadata exists for the post."""
    return any(
        _meta_path_for_stem(stem).exists()
        for stem in _lookup_stems(post_id, post_urn=post_urn)
    )


def post_summary_complete(
    meta: dict[str, Any] | None,
    *,
    content_len: int | None = None,
) -> bool:
    """True when TLDR exists and summary body exists (or TLDR-only for very short posts)."""
    if not meta:
        return False
    if not (meta.get("tldr") or "").strip():
        return False
    bullets = meta.get("summary_bullets")
    if isinstance(bullets, list) and any(str(b).strip() for b in bullets):
        return True
    if (meta.get("summary") or "").strip():
        return True
    if (
        content_len is not None
        and content_len < 200
        and (meta.get("summarized_at") or "").strip()
    ):
        return True
    return False


def needs_summary(post_id: str = "", *, post_urn: str = "") -> bool:
    """True if the post has content but summary metadata is missing or incomplete."""
    if not has_content(post_id, post_urn=post_urn):
        return False
    meta = load_metadata(post_id, post_urn=post_urn)
    content_len = len(load_content(post_id, post_urn=post_urn) or "")
    return not post_summary_complete(meta, content_len=content_len)


def _load_registry() -> dict[str, str]:
    """Load stem -> post_urn registry once. Returns {} if missing."""
    registry_path = _content_dir() / "_urn_registry.json"
    if not registry_path.exists():
        return {}
    data: dict[str, str] = json.loads(registry_path.read_text(encoding="utf-8"))
    return data


def _stem_to_post_id(stem: str, meta: dict[str, Any]) -> str:
    pid = (str(meta.get("post_id") or "")).strip()
    if pid:
        return pid
    if stem.isdigit():
        return stem
    return ""


def list_summarized_metadata(limit: int | None = None) -> list[dict[str, Any]]:
    """All posts that have a non-empty summary."""
    out: list[dict[str, Any]] = []
    content_dir = _content_dir()
    registry = _load_registry()
    for path in sorted(content_dir.glob("*.meta.json")):
        try:
            stem = path.stem.removesuffix(".meta")
            meta = json.loads(path.read_text(encoding="utf-8"))
            if not (meta.get("summary") or "").strip():
                continue
            post_id = _stem_to_post_id(stem, meta)
            urn = (meta.get("post_urn") or registry.get(stem) or "").strip()
            act_ids = meta.get("activities_ids") or []
            if not isinstance(act_ids, list):
                act_ids = []
            out.append(
                {
                    "post_id": post_id,
                    "urn": urn,
                    "summary": (meta.get("summary") or "").strip(),
                    "topics": meta.get("topics") or [],
                    "technologies": meta.get("technologies") or [],
                    "people": meta.get("people") or [],
                    "category": meta.get("category") or "",
                    "summarized_at": meta.get("summarized_at") or "",
                    "post_url": meta.get("post_url") or "",
                    "post_urn": (meta.get("post_urn") or "").strip(),
                    "post_author": (meta.get("post_author") or "").strip(),
                    "post_author_url": (meta.get("post_author_url") or "").strip(),
                    "activities_ids": [str(x) for x in act_ids if x],
                    "activity_time_iso": _normalize_activity_time_iso(meta),
                    "post_created_at": (meta.get("post_created_at") or "").strip(),
                }
            )
            if limit and len(out) >= limit:
                break
        except (json.JSONDecodeError, OSError):
            continue
    return out


def list_posts_for_summary(
    limit: int | None = None,
    *,
    force: bool = False,
    urns: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Posts with content (≥50 chars).

    Skips posts with complete summary metadata (TLDR + bullets/summary text).
    With *force*, re-summarize even when complete.
    When *urns* is set, only posts whose ``post_id`` or ``post_urn`` is in that
    set are considered (pipeline period scope). Without *urns*, scans the whole
    content store.
    """
    out: list[dict[str, Any]] = []
    content_dir = _content_dir()
    registry = _load_registry()
    for path in sorted(content_dir.glob("*.md")):
        stem = path.stem
        content = path.read_text(encoding="utf-8")
        if len(content) < 50:
            continue
        meta_path = content_dir / f"{stem}.meta.json"
        meta: dict[str, Any] = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not force and post_summary_complete(meta, content_len=len(content)):
                continue
        post_id = _stem_to_post_id(stem, meta)
        urn = (meta.get("post_urn") or registry.get(stem) or "").strip()
        if not post_id and not urn:
            continue
        if urns is not None and post_id not in urns and urn not in urns:
            continue
        out.append({"post_id": post_id, "urn": urn, "content": content})
        if limit and len(out) >= limit:
            break
    return out


def list_posts_needing_summary(limit: int | None = None) -> list[dict[str, Any]]:
    """Posts with content (≥50 chars) but incomplete LLM summary."""
    return list_posts_for_summary(limit=limit, force=False)


def _register_post(stem: str, post_urn: str) -> None:
    """Register stem -> post_urn for reverse lookup (legacy file name kept)."""
    if not stem:
        return
    registry_path = _content_dir() / "_urn_registry.json"
    reg: dict[str, str] = {}
    if registry_path.exists():
        reg = json.loads(registry_path.read_text(encoding="utf-8"))
    if post_urn:
        reg[stem] = post_urn
    registry_path.write_text(json.dumps(reg, indent=0), encoding="utf-8")

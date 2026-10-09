"""Post image sidecars: local ``content/images/`` + CDN metadata (fallback only)."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

LOCAL_IMAGE_EMBED_RE = re.compile(r"!\[[^\]]*\]\((images/[^)>\s]+)\)")
CDN_URL_IN_MARKDOWN_RE = re.compile(
    r"!\[[^\]]*\]\((https://media\.licdn\.com/[^)>\s]+)\)"
)

CDN_EXPIRY_CLOCK_SKEW_SECONDS = 60.0
_LINKEDIN_MEDIA_HOST_SUFFIX = ".licdn.com"


def parse_linkedin_cdn_expires_at_unix(url: str) -> int | None:
    """Return Unix seconds from LinkedIn CDN query param ``e=``, if present."""
    parsed = urlparse((url or "").strip())
    raw = parse_qs(parsed.query).get("e", [None])[0]
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def cdn_expires_at_iso(url: str) -> str | None:
    ts = parse_linkedin_cdn_expires_at_unix(url)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def cdn_url_identity(url: str) -> str:
    """Stable image id: scheme + host + path (no query)."""
    parsed = urlparse((url or "").strip())
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def cdn_url_for_log(url: str) -> str:
    """Log-friendly CDN reference without signed query tokens."""
    parsed = urlparse((url or "").strip())
    if parsed.path:
        return parsed.path
    return (url or "").strip()[:120]


def cdn_url_is_expired(url: str, *, now: datetime | None = None) -> bool:
    ts = parse_linkedin_cdn_expires_at_unix(url)
    if ts is None:
        return False
    ref = now or datetime.now(tz=UTC)
    return ref.timestamp() >= float(ts) - CDN_EXPIRY_CLOCK_SKEW_SECONDS


def legacy_v3_image_rel(full_url: str) -> str:
    """v3 on-disk name: ``sha256(full signed URL)`` (same rules as pre-v4 enrich)."""
    url = (full_url or "").strip()
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:24]
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        suffix = ".jpg"
    return f"images/{url_hash}{suffix}"


def image_magic_suffix(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
        return ".gif"
    if len(content) >= 12 and content[0:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return None


def is_image_content(content: bytes, content_type: str) -> bool:
    if image_magic_suffix(content):
        return True
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return ct.startswith("image/")


def image_suffix_for_valid_content(content: bytes, content_type: str) -> str | None:
    """Pick extension from magic bytes first, then ``image/*`` Content-Type."""
    magic = image_magic_suffix(content)
    if magic:
        return magic
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    by_ct = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }
    if ct in by_ct:
        return by_ct[ct]
    return None


def image_meta_record(
    cdn_url: str,
    *,
    local_path: str | None = None,
) -> dict[str, Any]:
    url = (cdn_url or "").strip()
    rec: dict[str, Any] = {"cdn_url": url}
    expires = cdn_expires_at_iso(url)
    if expires:
        rec["cdn_expires_at"] = expires
    if local_path:
        rec["local_path"] = local_path
    return rec


def _entry_cdn_url(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("cdn_url") or "").strip()
    return str(entry or "").strip()


def _entry_local_path(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("local_path") or "").strip()
    s = str(entry or "").strip()
    if s.startswith("images/"):
        return s
    return ""


def _meta_merge_key(entry: Any) -> str:
    cdn = _entry_cdn_url(entry)
    if cdn.startswith("http"):
        return cdn_url_identity(cdn)
    local = _entry_local_path(entry)
    if local:
        return f"local:{local}"
    return ""


def normalize_image_meta_list(images: Any) -> list[dict[str, Any]]:
    if not isinstance(images, list):
        return []
    out: list[dict[str, Any]] = []
    for item in images:
        cdn = _entry_cdn_url(item)
        local = _entry_local_path(item)
        if isinstance(item, dict) and (cdn or local):
            rec = dict(item)
            if cdn:
                rec.setdefault("cdn_url", cdn)
            if local:
                rec.setdefault("local_path", local)
            if "cdn_expires_at" not in rec and cdn:
                exp = cdn_expires_at_iso(cdn)
                if exp:
                    rec["cdn_expires_at"] = exp
            out.append(rec)
        elif cdn.startswith("http"):
            out.append(image_meta_record(cdn, local_path=local or None))
        elif local:
            out.append({"local_path": local, "cdn_url": ""})
    return out


def merge_image_meta_lists(
    previous: list[Any] | None,
    incoming: list[Any] | None,
) -> list[dict[str, Any]]:
    """Merge by CDN path identity (query stripped) or by ``local_path``."""
    merged: dict[str, dict[str, Any]] = {}
    for group in (previous, incoming):
        for rec in normalize_image_meta_list(group):
            key = _meta_merge_key(rec)
            if not key:
                continue
            if key not in merged:
                merged[key] = rec
            else:
                prev = merged[key]
                for field in ("cdn_url", "cdn_expires_at", "local_path"):
                    if rec.get(field):
                        prev[field] = rec[field]
    return list(merged.values())


def resolve_trusted_local_rel(rel: str, content_root: Path) -> str | None:
    """Return *rel* only when it resolves to a file under ``content/images/``."""
    candidate = (rel or "").strip()
    if not candidate.startswith("images/"):
        return None
    if any(part == ".." for part in candidate.split("/")):
        return None
    images_root = (content_root / "images").resolve()
    try:
        target = (content_root / candidate).resolve()
        target.relative_to(images_root)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return candidate


def image_ref_looks_unsafe(rel: str) -> bool:
    """True when *rel* is clearly not a normal ``images/…`` catalog ref."""
    candidate = (rel or "").strip()
    if not candidate.startswith("images/"):
        return True
    if any(part == ".." for part in candidate.split("/")):
        return True
    if "\\" in candidate or candidate.startswith("/"):
        return True
    lowered = candidate.lower()
    if "%2e" in lowered or "%2f" in lowered:
        return True
    return False


def find_trusted_local_embeds(markdown: str, content_root: Path) -> list[str]:
    """Local ``images/…`` embeds from *markdown* that exist under ``content/images/``."""
    if not markdown:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for match in LOCAL_IMAGE_EMBED_RE.finditer(markdown):
        rel = match.group(1)
        trusted = resolve_trusted_local_rel(rel, content_root)
        if trusted and trusted not in seen:
            seen.add(trusted)
            out.append(trusted)
    return out


def build_identity_reuse_map(
    existing_body: str | None,
    existing_images: list[Any] | None,
    content_root: Path,
) -> dict[str, str]:
    """
    Map ``cdn_url_identity`` → trusted ``images/…`` path to reuse (v3 migration).

    Uses prior ``meta.images`` full URLs (v3 hash names) and any trusted embeds.
    """
    reuse: dict[str, str] = {}
    for rel in find_trusted_local_embeds(existing_body or "", content_root):
        reuse.setdefault(f"local:{rel}", rel)

    for rec in normalize_image_meta_list(existing_images):
        cdn = _entry_cdn_url(rec)
        local = _entry_local_path(rec)
        if cdn.startswith("http"):
            ident = cdn_url_identity(cdn)
            legacy = legacy_v3_image_rel(cdn)
            if resolve_trusted_local_rel(legacy, content_root):
                reuse[ident] = legacy
            elif local and resolve_trusted_local_rel(local, content_root):
                reuse[ident] = local
        elif local and resolve_trusted_local_rel(local, content_root):
            reuse[f"local:{local}"] = local
    return reuse


def strip_cdn_image_embeds(markdown: str) -> str:
    """Remove ``![](https://media.licdn.com/…)`` lines from markdown."""
    if not markdown:
        return markdown
    lines = []
    for line in markdown.splitlines():
        if CDN_URL_IN_MARKDOWN_RE.search(line):
            continue
        lines.append(line)
    return "\n".join(lines).rstrip()


def strip_untrusted_local_image_embeds(markdown: str, content_root: Path) -> str:
    """Remove untrusted ``![](images/…)`` embeds; keep surrounding text on the line."""

    def _replace(match: re.Match[str]) -> str:
        rel = match.group(1)
        if resolve_trusted_local_rel(rel, content_root):
            return match.group(0)
        return ""

    if not markdown:
        return markdown
    return LOCAL_IMAGE_EMBED_RE.sub(_replace, markdown)


def _append_local_embeds(body: str, local_rels: list[str]) -> str:
    body = body.rstrip()
    for rel in local_rels:
        token = f"]({rel})"
        if token in body:
            continue
        body = f"{body}\n\n![]({rel})" if body else f"![]({rel})"
    return body


def filter_post_image_urls(urls: list[str]) -> list[str]:
    """De-duplicated HTTPS image URLs from extraction (all hosts), by path identity."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        url = (raw or "").strip()
        if not url.startswith("https://"):
            continue
        ident = cdn_url_identity(url)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(url)
    return out


def filter_linkedin_cdn_urls(urls: list[str]) -> list[str]:
    """Backward-compatible alias for LinkedIn-only filtering (API fallback URLs)."""
    return [
        u
        for u in filter_post_image_urls(urls)
        if urlparse(u).netloc.endswith(_LINKEDIN_MEDIA_HOST_SUFFIX)
    ]


def apply_post_image_sidecar(
    body: str,
    cdn_urls: list[str],
    *,
    existing_body: str | None,
    existing_images: list[Any] | None = None,
    download: Callable[[str], str | None],
) -> tuple[str, list[dict[str, Any]]]:
    """
    Attach post images to sidecar markdown + metadata.

    *download* is ``download_image_to_store`` (injected for tests).
    """
    from linkedin_api.activity_csv import get_data_dir

    content_root = get_data_dir() / "content"
    body = strip_cdn_image_embeds(body)
    body = strip_untrusted_local_image_embeds(body, content_root)
    reuse_map = build_identity_reuse_map(existing_body, existing_images, content_root)

    unique_urls = filter_post_image_urls(cdn_urls)
    trusted_prior = find_trusted_local_embeds(existing_body or "", content_root)
    if len(trusted_prior) == 1 and len(unique_urls) == 1:
        reuse_map.setdefault(cdn_url_identity(unique_urls[0]), trusted_prior[0])

    meta_records: list[dict[str, Any]] = []
    local_embeds: list[str] = []

    for image_url in unique_urls:
        ident = cdn_url_identity(image_url)
        parsed = urlparse(image_url)
        if not parsed.netloc.endswith(_LINKEDIN_MEDIA_HOST_SUFFIX):
            logger.info(
                "Downloading non-LinkedIn image host %s",
                parsed.netloc or cdn_url_for_log(image_url),
            )

        if cdn_url_is_expired(image_url):
            logger.warning(
                "Skipping expired LinkedIn CDN image (e= in the past): %s",
                cdn_url_for_log(image_url),
            )
            local_rel = reuse_map.get(ident)
            rec = image_meta_record(image_url, local_path=local_rel)
            meta_records.append(rec)
            if local_rel:
                local_embeds.append(local_rel)
            continue

        local_rel = reuse_map.get(ident)
        if not local_rel:
            local_rel = download(image_url)

        if local_rel and resolve_trusted_local_rel(local_rel, content_root):
            meta_records.append(image_meta_record(image_url, local_path=local_rel))
            if local_rel not in local_embeds:
                local_embeds.append(local_rel)
        else:
            logger.warning(
                "Post image fetch failed; metadata only: %s",
                cdn_url_for_log(image_url),
            )
            meta_records.append(image_meta_record(image_url))

    for rel in trusted_prior:
        if rel not in local_embeds:
            local_embeds.append(rel)

    if local_embeds:
        body = _append_local_embeds(body, local_embeds)

    return body, meta_records


def sidecar_still_has_trusted_embeds(
    markdown: str,
    prior_trusted: list[str],
    *,
    content_root: Path,
) -> bool:
    if not prior_trusted:
        return True
    current = find_trusted_local_embeds(markdown, content_root)
    return all(rel in current for rel in prior_trusted)

"""Post image sidecars: CDN embeds in markdown + metadata (no enrich-time download)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

LOCAL_IMAGE_EMBED_RE = re.compile(r"!\[[^\]]*\]\((images/[^)>\s]+)\)")
HTTPS_IMAGE_EMBED_RE = re.compile(r"!\[[^\]]*\]\((https://[^)>\s]+)\)")

CDN_EXPIRY_CLOCK_SKEW_SECONDS = 60.0

# Fixture URL for tests (``e=`` decodes to 1780668000 — expired vs 2026-10-09).
EXPIRED_CDN_FIXTURE_URL = (
    "https://media.licdn.com/dms/image/v2/fixture/0?e=1780668000&v=beta&t=fixture"
)


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


def is_linkedin_generic_placeholder_image(url: str) -> bool:
    """LinkedIn UI placeholders ('Posted on LinkedIn' / static assets), not post photos."""
    parsed = urlparse((url or "").strip())
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if host == "static.licdn.com":
        return True
    if "/scds/common/u/" in path:
        return True
    if "ghost" in path and "image" in path:
        return True
    return False


def filter_post_image_urls(urls: list[str]) -> list[str]:
    """De-duplicated HTTPS image URLs by path identity (placeholders excluded)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        url = (raw or "").strip()
        if not url.startswith("https://"):
            continue
        if is_linkedin_generic_placeholder_image(url):
            continue
        ident = cdn_url_identity(url)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(url)
    return out


def image_meta_record(
    cdn_url: str,
    *,
    local_path: str | None = None,
) -> dict[str, Any]:
    url = (cdn_url or "").strip()
    ident = cdn_url_identity(url) if url.startswith("http") else ""
    rec: dict[str, Any] = {"cdn_url": url}
    if ident:
        rec["identity"] = ident
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


def _entry_identity(entry: Any) -> str:
    if isinstance(entry, dict):
        stored = str(entry.get("identity") or "").strip()
        if stored:
            return stored
    cdn = _entry_cdn_url(entry)
    if cdn.startswith("http"):
        return cdn_url_identity(cdn)
    return ""


def _meta_merge_key(entry: Any) -> str:
    ident = _entry_identity(entry)
    if ident:
        return ident
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
                rec.setdefault("identity", cdn_url_identity(cdn))
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
            out.append({"local_path": local, "cdn_url": "", "identity": ""})
    return out


def merge_image_meta_lists(
    previous: list[Any] | None,
    incoming: list[Any] | None,
) -> list[dict[str, Any]]:
    """Merge by ``identity`` (or legacy CDN path identity / ``local_path``)."""
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
                for field in ("cdn_url", "cdn_expires_at", "identity", "local_path"):
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


def strip_https_image_embeds(markdown: str) -> str:
    """Remove ``![](https://…)`` embeds (replaced by sidecar pass)."""
    if not markdown:
        return markdown

    def _replace(match: re.Match[str]) -> str:
        return ""

    return HTTPS_IMAGE_EMBED_RE.sub(_replace, markdown)


def strip_untrusted_local_image_embeds(markdown: str, content_root: Path) -> str:
    def _replace(match: re.Match[str]) -> str:
        rel = match.group(1)
        if resolve_trusted_local_rel(rel, content_root):
            return match.group(0)
        return ""

    if not markdown:
        return markdown
    return LOCAL_IMAGE_EMBED_RE.sub(_replace, markdown)


def _append_embeds(body: str, targets: list[str]) -> str:
    body = body.rstrip()
    for target in targets:
        token = f"]({target})"
        if token in body:
            continue
        body = f"{body}\n\n![]({target})" if body else f"![]({target})"
    return body


def build_identity_local_map(
    existing_body: str | None,
    existing_images: list[Any] | None,
    content_root: Path,
) -> dict[str, str]:
    """Map CDN path identity → trusted ``images/…`` path from prior sidecar."""
    local_by_ident: dict[str, str] = {}
    for rec in normalize_image_meta_list(existing_images):
        cdn = _entry_cdn_url(rec)
        local = _entry_local_path(rec)
        if cdn.startswith("http") and local:
            if resolve_trusted_local_rel(local, content_root):
                local_by_ident[cdn_url_identity(cdn)] = local
    return local_by_ident


def apply_post_image_sidecar(
    body: str,
    image_urls: list[str],
    *,
    existing_body: str | None,
    existing_images: list[Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Record CDN images in ``meta.images[]`` and markdown.

    New images: ``![](https://…signed…)`` (no download). Existing trusted local
    files from a prior enrich are kept and preferred for the same identity.
    """
    from linkedin_api.activity_csv import get_data_dir

    content_root = get_data_dir() / "content"
    body = strip_https_image_embeds(body)
    body = strip_untrusted_local_image_embeds(body, content_root)
    local_by_ident = build_identity_local_map(
        existing_body, existing_images, content_root
    )

    unique_urls = filter_post_image_urls(image_urls)
    trusted_prior = find_trusted_local_embeds(existing_body or "", content_root)
    if len(trusted_prior) == 1 and len(unique_urls) == 1:
        local_by_ident.setdefault(cdn_url_identity(unique_urls[0]), trusted_prior[0])

    meta_records: list[dict[str, Any]] = []
    embed_targets: list[str] = []

    for image_url in unique_urls:
        ident = cdn_url_identity(image_url)
        local_rel = local_by_ident.get(ident)
        rec = image_meta_record(image_url)
        if local_rel and resolve_trusted_local_rel(local_rel, content_root):
            rec["local_path"] = local_rel
            embed_targets.append(local_rel)
        else:
            embed_targets.append(image_url)
        meta_records.append(rec)

    for rel in trusted_prior:
        if rel not in embed_targets:
            embed_targets.append(rel)

    if embed_targets:
        body = _append_embeds(body, embed_targets)

    return body, meta_records

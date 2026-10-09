"""Post image sidecars: local ``content/images/`` + CDN metadata (fallback only)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

LOCAL_IMAGE_EMBED_RE = re.compile(r"!\[[^\]]*\]\((images/[^)>\s]+)\)")
CDN_URL_IN_MARKDOWN_RE = re.compile(
    r"!\[[^\]]*\]\((https://media\.licdn\.com/[^)>\s]+)\)"
)

CDN_EXPIRY_CLOCK_SKEW_SECONDS = 60.0


def parse_linkedin_cdn_expires_at_unix(url: str) -> int | None:
    """Return Unix seconds from LinkedIn CDN query param ``e=``, if present."""
    from urllib.parse import parse_qs

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
    """Remove ``![](images/…)`` lines that are not trusted paths under ``content/images/``."""
    if not markdown:
        return markdown
    trusted = set(find_trusted_local_embeds(markdown, content_root))
    lines = []
    for line in markdown.splitlines():
        match = LOCAL_IMAGE_EMBED_RE.search(line)
        if match and match.group(1) not in trusted:
            continue
        lines.append(line)
    return "\n".join(lines).rstrip()


def _append_local_embeds(body: str, local_rels: list[str]) -> str:
    body = body.rstrip()
    for rel in local_rels:
        token = f"]({rel})"
        if token in body:
            continue
        body = f"{body}\n\n![]({rel})" if body else f"![]({rel})"
    return body


def filter_linkedin_cdn_urls(urls: list[str]) -> list[str]:
    """De-duplicated LinkedIn CDN image URLs, order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        url = (raw or "").strip()
        if not url.startswith("https://media.licdn.com/"):
            continue
        ident = cdn_url_identity(url)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(url)
    return out


def apply_post_image_sidecar(
    body: str,
    cdn_urls: list[str],
    *,
    existing_body: str | None,
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
    trusted_unused = find_trusted_local_embeds(existing_body or "", content_root)
    unused_locals = list(trusted_unused)

    unique_cdns = filter_linkedin_cdn_urls(cdn_urls)
    meta_records: list[dict[str, Any]] = []
    local_embeds: list[str] = []

    for cdn in unique_cdns:
        if cdn_url_is_expired(cdn):
            logger.warning(
                "Skipping expired LinkedIn CDN image (e= in the past): %s",
                cdn_url_for_log(cdn),
            )
            rec = image_meta_record(cdn)
            if unused_locals:
                rel = unused_locals.pop(0)
                rec["local_path"] = rel
                local_embeds.append(rel)
            meta_records.append(rec)
            continue

        local_rel = download(cdn)
        if local_rel and resolve_trusted_local_rel(local_rel, content_root):
            meta_records.append(image_meta_record(cdn, local_path=local_rel))
            local_embeds.append(local_rel)
            if local_rel in unused_locals:
                unused_locals.remove(local_rel)
        else:
            logger.warning(
                "LinkedIn CDN image fetch failed; metadata only: %s",
                cdn_url_for_log(cdn),
            )
            rec = image_meta_record(cdn)
            if unused_locals:
                rel = unused_locals.pop(0)
                rec["local_path"] = rel
                local_embeds.append(rel)
            meta_records.append(rec)

    if not unique_cdns and unused_locals:
        for rel in unused_locals:
            meta_records.append({"local_path": rel, "cdn_url": ""})
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

"""Post image sidecars: local ``content/images/`` + CDN metadata (fallback only)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

LOCAL_IMAGE_EMBED_RE = re.compile(r"!\[[^\]]*\]\((images/[^)>\s]+)\)")
CDN_URL_IN_MARKDOWN_RE = re.compile(
    r"!\[[^\]]*\]\((https://media\.licdn\.com/[^)>\s]+)\)"
)

EXPIRED_CDN_FIXTURE_URL = (
    "https://media.licdn.com/dms/image/sync/v2/D4E27AQHREDACTED/articleshare-shrink_480/"
    "B4EZmREDACTED/0/1776442767212?e=1780668000&v=beta&t=REDACTED"
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


def cdn_url_is_expired(url: str, *, now: datetime | None = None) -> bool:
    ts = parse_linkedin_cdn_expires_at_unix(url)
    if ts is None:
        return False
    ref = now or datetime.now(tz=UTC)
    return ref.timestamp() >= float(ts)


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
        return str(entry.get("cdn_url") or entry.get("url") or "").strip()
    return str(entry or "").strip()


def _entry_local_path(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("local_path") or "").strip()
    s = str(entry or "").strip()
    if s.startswith("images/"):
        return s
    return ""


def normalize_image_meta_list(images: Any) -> list[dict[str, Any]]:
    if not isinstance(images, list):
        return []
    out: list[dict[str, Any]] = []
    for item in images:
        cdn = _entry_cdn_url(item)
        local = _entry_local_path(item)
        if isinstance(item, dict) and cdn:
            rec = dict(item)
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
    """Merge by ``cdn_url`` when set, else by ``local_path``."""
    merged: dict[str, dict[str, Any]] = {}
    for group in (previous, incoming):
        for rec in normalize_image_meta_list(group):
            key = _entry_cdn_url(rec) or f"local:{_entry_local_path(rec)}"
            if not key or key == "local:":
                continue
            if key not in merged:
                merged[key] = rec
            else:
                prev = merged[key]
                for field in ("cdn_url", "cdn_expires_at", "local_path"):
                    if rec.get(field) and not prev.get(field):
                        prev[field] = rec[field]
    return list(merged.values())


def find_local_image_embed(markdown: str) -> str | None:
    if not markdown:
        return None
    match = LOCAL_IMAGE_EMBED_RE.search(markdown)
    return match.group(1) if match else None


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


def ensure_local_embed(body: str, local_rel: str) -> str:
    """Keep ``body`` but guarantee a single local ``![](images/…)`` embed."""
    body = strip_cdn_image_embeds(body)
    if local_rel in body and f"]({local_rel})" in body:
        return body
    return body.rstrip() + f"\n\n![]({local_rel})"


def fetch_cdn_image_bytes(url: str) -> bytes | None:
    """One-shot CDN fetch for vault export (no local store write)."""
    import requests as _req

    cdn = (url or "").strip()
    if not cdn:
        return None
    if cdn_url_is_expired(cdn):
        logger.warning("Vault export: skipping expired CDN image %s", cdn)
        return None
    try:
        resp = _req.get(
            cdn,
            timeout=15,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        if resp.status_code == 403:
            logger.warning("Vault export: CDN image 403 %s", cdn)
            return None
        if resp.status_code == 200 and resp.content:
            return resp.content
        logger.warning("Vault export: CDN image HTTP %s %s", resp.status_code, cdn)
    except Exception as exc:
        logger.warning("Vault export: CDN image fetch error %s (%s)", cdn, exc)
    return None


def fetch_image_bytes_for_vault(image_rec: dict[str, Any]) -> bytes | None:
    """Resolve image bytes for catalog export (local store first, then CDN)."""
    from linkedin_api.content_store import get_data_dir

    local = _entry_local_path(image_rec)
    if local:
        path = get_data_dir() / "content" / local
        if path.is_file():
            return path.read_bytes()
    cdn = _entry_cdn_url(image_rec)
    if cdn.startswith("http"):
        return fetch_cdn_image_bytes(cdn)
    return None


def apply_post_image_sidecar(
    body: str,
    cdn_urls: list[str],
    *,
    existing_body: str | None,
    download: Any,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Attach post images to sidecar markdown + metadata.

    *download* is ``download_image_to_store`` (injected for tests).
    """
    from linkedin_api.content_store import get_data_dir

    prior = (existing_body or "").strip()
    local_rel = find_local_image_embed(prior) or find_local_image_embed(body)
    content_root = get_data_dir() / "content"
    if local_rel and (content_root / local_rel).is_file():
        body = ensure_local_embed(strip_cdn_image_embeds(body), local_rel)
        cdn = (cdn_urls[0] if cdn_urls else "").strip()
        if cdn:
            return body, [image_meta_record(cdn, local_path=local_rel)]
        existing_meta = (
            [image_meta_record("", local_path=local_rel)] if local_rel else []
        )
        return body, existing_meta

    body = strip_cdn_image_embeds(body)
    if not cdn_urls:
        return body, []

    cdn = (cdn_urls[0] or "").strip()
    if not cdn:
        return body, []

    if cdn_url_is_expired(cdn):
        logger.warning("Skipping expired LinkedIn CDN image (e= in the past): %s", cdn)
        return body, [image_meta_record(cdn)]

    local_rel = download(cdn)
    if local_rel:
        body = body.rstrip() + f"\n\n![]({local_rel})"
        return body, [image_meta_record(cdn, local_path=local_rel)]

    logger.warning("LinkedIn CDN image fetch failed; metadata only: %s", cdn)
    return body, [image_meta_record(cdn)]

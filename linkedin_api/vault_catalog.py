from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kg_vault.catalog import (
    CATALOG_THIRD_PARTY_PREFIX,
    catalog_filename,
    catalog_rel_path,
    content_hash_for_body,
    emit_intake_record,
    format_frontmatter,
    normalize_url,
    parse_source_id_from_markdown,
)

from linkedin_api.enriched_record import EnrichedRecord
from linkedin_api.fetch_linked_content import FetchResult
from linkedin_api.utils.urls import resolve_redirect

logger = logging.getLogger(__name__)

PRODUCER = "linkedin-api"
_CONTRACT_SHA = "pending"  # pin after amai-lab merge; see kg-ingest-markdown-output.md

_INTERACTION_LABELS = {
    "post": "Post",
    "reaction": "Reaction",
    "repost": "Repost",
    "comment": "Comment",
}


@dataclass(frozen=True)
class CatalogBuild:
    rel_path: str
    markdown: str
    source_id: str
    producer: str
    tldr: str
    title: str


def activity_source_id(activity_id: str) -> str:
    aid = (activity_id or "").strip()
    if not aid:
        raise ValueError("activity_id is required for platform source_id")
    return f"platform:linkedin:activity:{aid}"


def article_source_id(url: str, *, resolve: bool = True) -> str:
    raw = (url or "").strip()
    candidate: str = resolve_redirect(raw) if resolve else raw
    return normalize_url(candidate)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _activity_datetime(rec: EnrichedRecord) -> datetime:
    if rec.timestamp is not None:
        return datetime.fromtimestamp(rec.timestamp / 1000, tz=UTC)
    parsed = _parse_iso_datetime(rec.created_at)
    if parsed is not None:
        return parsed.astimezone(UTC)
    return datetime.now(UTC)


def _title_for_activity(
    rec: EnrichedRecord,
    *,
    summary: str,
    content: str,
) -> str:
    summary_line = (summary or "").strip().splitlines()[0] if summary else ""
    if summary_line:
        base = summary_line[:120]
    else:
        first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
        base = first_line[:120] if first_line else "LinkedIn activity"
    label = _INTERACTION_LABELS.get(rec.interaction_type, "Activity")
    if rec.reaction_type:
        label = f"{label} ({rec.reaction_type})"
    return f"{label}: {base}"


def _summary_section(
    *,
    author: str,
    summary: str,
    topics: list[str],
) -> list[str]:
    lines = ["## Summary", "", f"AUTHOR: {author or 'Unknown'}"]
    summary_text = (summary or "").strip()
    if summary_text:
        for part in re.split(r"(?<=[.!?])\s+", summary_text):
            part = part.strip()
            if part:
                lines.append(f"- {part}")
    elif topics:
        for topic in topics[:6]:
            topic_text = str(topic).strip()
            if topic_text:
                lines.append(f"- **{topic_text}**")
    else:
        lines.append("- LinkedIn activity captured from the Portability export.")
    lines.append("")
    return lines


def _tldr_from_summary(summary: str) -> str:
    text = (summary or "").strip().splitlines()[0] if summary else ""
    if not text:
        return ""
    words = text.split()
    if len(words) > 25:
        return " ".join(words[:25]) + "…"
    return text


def build_activity_catalog_markdown(
    rec: EnrichedRecord,
    *,
    content: str,
    meta: dict[str, Any] | None,
    occupied: set[str] | None = None,
    linked_article_ids: list[str] | None = None,
) -> CatalogBuild:
    """Build catalog markdown for one LinkedIn activity row."""
    meta = meta or {}
    source_id = activity_source_id(rec.activity_id)
    source_ref = rec.activity_id
    published_at = _activity_datetime(rec)
    title = _title_for_activity(
        rec, summary=str(meta.get("summary") or ""), content=content
    )
    filename = catalog_filename(
        title,
        pub_date=published_at.date().isoformat(),
        source_id=source_id,
        occupied=occupied,
    )
    rel_path = catalog_rel_path(filename)

    author = (meta.get("post_author") or "Unknown").strip() or "Unknown"
    raw_topics = meta.get("topics")
    topics: list[str] = []
    if isinstance(raw_topics, list):
        topics = [str(t) for t in raw_topics]
    summary = str(meta.get("summary") or "")

    body_sections: list[str] = []
    if rec.comment_text:
        body_sections.extend(
            [
                "## Interaction",
                "",
                rec.comment_text.strip(),
                "",
            ]
        )
    body_sections.extend(
        _summary_section(author=author, summary=summary, topics=topics)
    )
    if content.strip():
        body_sections.extend(["## Source", "", content.strip(), ""])
    body = "\n".join(body_sections).rstrip() + "\n"
    content_hash = content_hash_for_body(body)
    tldr = _tldr_from_summary(summary)

    tags = [str(t).strip() for t in (meta.get("tags") or []) if str(t).strip()]
    if not tags and topics:
        tags = [str(t).strip() for t in topics[:5] if str(t).strip()]

    frontmatter: dict[str, Any] = {
        "title": title,
        "date": published_at.date().isoformat(),
        "published_at": published_at.isoformat(),
        "lang": "en",
        "producer": PRODUCER,
        "source_id": source_id,
        "tags": tags,
        "source_ref": source_ref,
        "status": "catalog",
        "content_hash": content_hash,
    }
    post_url = (rec.post_url or meta.get("post_url") or "").strip()
    if post_url:
        frontmatter["canonical_url"] = post_url
    if tldr:
        frontmatter["tldr"] = tldr
    related = [rid for rid in (linked_article_ids or []) if rid and rid != source_id]
    if related:
        frontmatter["related_source_ids"] = related

    markdown = format_frontmatter(frontmatter) + "\n" + body
    return CatalogBuild(
        rel_path=rel_path,
        markdown=markdown,
        source_id=source_id,
        producer=PRODUCER,
        tldr=tldr,
        title=title,
    )


def build_article_catalog_markdown(
    result: FetchResult,
    *,
    activity_source_ids: list[str] | None = None,
    occupied: set[str] | None = None,
    resolve: bool = True,
) -> CatalogBuild | None:
    """Build catalog markdown for a linked article URL."""
    url = (result.resolved_url or result.url or "").strip()
    if not url or result.error:
        return None
    body_text = (result.content or "").strip()
    title = (result.title or "").strip() or url
    if not body_text and not title:
        return None

    source_id = article_source_id(url, resolve=resolve)
    source_ref = (result.url or url).strip()
    published_at = datetime.now(UTC)
    fetched_at = _parse_iso_datetime(result.fetched_at)
    if fetched_at is not None:
        published_at = fetched_at.astimezone(UTC)

    filename = catalog_filename(
        title,
        pub_date=published_at.date().isoformat(),
        source_id=source_id,
        occupied=occupied,
    )
    rel_path = catalog_rel_path(filename)

    body_sections = _summary_section(author="Unknown", summary="", topics=[])
    if body_text:
        body_sections.extend(["## Source", "", body_text, ""])
    body = "\n".join(body_sections).rstrip() + "\n"
    content_hash = content_hash_for_body(body)

    frontmatter: dict[str, Any] = {
        "title": title,
        "date": published_at.date().isoformat(),
        "published_at": published_at.isoformat(),
        "lang": "en",
        "producer": PRODUCER,
        "source_id": source_id,
        "tags": [],
        "canonical_url": source_id,
        "source_ref": source_ref,
        "status": "catalog",
        "content_hash": content_hash,
    }
    related = [sid for sid in (activity_source_ids or []) if sid]
    if related:
        frontmatter["related_source_ids"] = related

    markdown = format_frontmatter(frontmatter) + "\n" + body
    return CatalogBuild(
        rel_path=rel_path,
        markdown=markdown,
        source_id=source_id,
        producer=PRODUCER,
        tldr="",
        title=title,
    )


def vault_write_message(*, title: str, created: bool) -> str:
    verb = "Add" if created else "Update"
    return f"{verb} catalog entry: {title}"


def scan_catalog_source_ids(catalog_dir: Path) -> dict[str, str]:
    """Map source_id -> rel_path for idempotent skips (local vault only)."""
    index: dict[str, str] = {}
    if not catalog_dir.is_dir():
        return index
    prefix = f"{CATALOG_THIRD_PARTY_PREFIX}/"
    for path in catalog_dir.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        source_id = parse_source_id_from_markdown(text)
        if source_id:
            index[source_id] = f"{prefix}{path.name}"
    return index


def list_resource_json_paths(resource_dir: Path) -> list[Path]:
    if not resource_dir.is_dir():
        return []
    return sorted(resource_dir.glob("*.json"))


__all__ = [
    "CatalogBuild",
    "activity_source_id",
    "article_source_id",
    "build_activity_catalog_markdown",
    "build_article_catalog_markdown",
    "emit_intake_record",
    "parse_source_id_from_markdown",
    "scan_catalog_source_ids",
    "vault_write_message",
]

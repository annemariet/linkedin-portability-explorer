"""
Pipeline view of one CSV activity row for enrich and report scoping.

``activity_csv.ActivityRecord`` is the persisted row; ``EnrichedRecord`` adds derived
fields (post URN/URL for comments, split body vs comment text) and is mutated during
enrich (``content``, ``urls``, ``enrich_error``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from linkedin_api.activity_csv import ActivityRecord, ActivityType
from linkedin_api.content_keys import (
    resolve_post_id,
    resolve_post_url,
    resolve_post_urn,
)
from linkedin_api.utils.urls import extract_urls_from_text


def _format_timestamp(ts_ms: int | None) -> str:
    if ts_ms is None:
        return ""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S%z"
    )


_TYPE_TO_INTERACTION: dict[str, str] = {
    ActivityType.POST.value: "post",
    ActivityType.REACTION_TO_POST.value: "reaction",
    ActivityType.REACTION_TO_COMMENT.value: "reaction",
    ActivityType.REPOST.value: "repost",
    ActivityType.INSTANT_REPOST.value: "repost",
    ActivityType.COMMENT.value: "comment",
}


@dataclass
class EnrichedRecord:
    """Working row for the enrich step (from CSV, with derived post URL and text split)."""

    post_urn: str
    post_url: str
    content: str
    urls: list[str]
    interaction_type: str
    reaction_type: str | None
    comment_text: str
    post_id: str
    activity_id: str
    timestamp: int | None
    created_at: str
    post_created_at: str | None = None
    enrich_error: str | None = None

    @classmethod
    def from_activity_record(cls, rec: ActivityRecord) -> EnrichedRecord:
        urls = extract_urls_from_text(rec.content) if rec.content else []
        ts = int(rec.time) if rec.time else None
        interaction_type = _TYPE_TO_INTERACTION.get(rec.activity_type, "reaction")
        is_comment = rec.activity_type == ActivityType.COMMENT.value
        post_id = resolve_post_id(rec)
        post_urn = resolve_post_urn(rec)
        post_url = resolve_post_url(rec)
        return cls(
            post_urn=post_urn,
            post_url=post_url,
            content="" if is_comment else (rec.content or ""),
            urls=urls,
            interaction_type=interaction_type,
            reaction_type=rec.reaction_type or None,
            comment_text=rec.content if is_comment else "",
            post_id=post_id,
            activity_id=rec.activity_id,
            timestamp=ts,
            created_at=rec.created_at or _format_timestamp(ts),
        )

"""Fetch and extract LinkedIn Portability API activities to ActivityRecords."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from linkedin_api.activity_csv import (
    ActivityRecord,
    ActivityType,
    make_activity_id,
)
from linkedin_api.utils.changelog import fetch_changelog_data
from linkedin_api.utils.urns import (
    build_comment_urn,
    extract_urn_id,
    parse_comment_urn,
    urn_to_post_url,
)

RESOURCE_REACTIONS = "socialActions/likes"
RESOURCE_COMMENTS = "socialActions/comments"
RESOURCE_POSTS = "ugcPosts"
RESOURCE_POST = "ugcPost"
RESOURCE_INSTANT_REPOSTS = "instantReposts"

POST_RELATED_RESOURCES = [
    RESOURCE_REACTIONS,
    RESOURCE_COMMENTS,
    RESOURCE_POSTS,
    RESOURCE_POST,
    RESOURCE_INSTANT_REPOSTS,
]


def format_timestamp(timestamp: int | None) -> str | None:
    """Format LinkedIn epoch-ms as UTC ISO-8601 (``...+00:00``).

    Must not use naive local time: ``filter_by_date`` treats naive ``created_at``
    as UTC, which drops recent rows for non-UTC machines (local clock ahead).
    """
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()


def _get_activity_timestamp(element: dict, activity: dict) -> int | None:
    created = activity.get("created", {})
    if isinstance(created, dict):
        ts = created.get("time")
        if isinstance(ts, int):
            return ts
    ts = element.get("processedAt")
    if isinstance(ts, int):
        return ts
    return None


def extract_actor(element: dict, activity: dict) -> str:
    actor = element.get("actor", "") or activity.get("actor", "")
    return str(actor) if actor else ""


def _extract_post_urn_for_reaction(element: dict, activity: dict) -> str:
    post_urn = activity.get("root") or activity.get("object", "")
    if isinstance(post_urn, str) and post_urn:
        return post_urn

    resource_id = element.get("resourceId", "")
    if isinstance(resource_id, str) and resource_id.startswith("urn:li:"):
        return resource_id

    resource_uri = element.get("resourceUri", "")
    if isinstance(resource_uri, str) and resource_uri:
        for part in resource_uri.split("/"):
            if part.startswith("urn:li:"):
                return part

    reaction_urn = activity.get("$URN") or activity.get("urn") or ""
    if isinstance(reaction_urn, str) and reaction_urn.startswith("urn:li:reaction:("):
        inner = reaction_urn[len("urn:li:reaction:(") :].rstrip(")")
        parts = [p.strip() for p in inner.split(",")]
        if len(parts) == 2 and parts[1].startswith("urn:li:"):
            return parts[1]

    return ""


def _is_delete_action(element: dict) -> bool:
    method = element.get("method") or element.get("methodName")
    return str(method).upper() == "DELETE"


def _is_comment_like_activity(activity: dict) -> bool:
    if not isinstance(activity, dict):
        return False
    has_message = bool(activity.get("message"))
    has_object = bool(activity.get("object"))
    if not has_message or not has_object:
        return False
    activity_id = activity.get("id", "")
    if isinstance(activity_id, str) and (
        activity_id.startswith("urn:li:share:")
        or activity_id.startswith("urn:li:ugcPost:")
    ):
        return False
    share_content = activity.get("specificContent", {}).get(
        "com.linkedin.ugc.ShareContent", {}
    )
    if share_content:
        return False
    return True


def _maybe_build_parent_comment_urn(post_urn: str, value: object) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        if value.startswith("urn:li:comment:"):
            return value
        if value.isdigit():
            return build_comment_urn(post_urn, value)
    return None


def _extract_parent_comment_urn(
    activity: dict, response_context: dict, post_urn: str
) -> str | None:
    for container in (response_context, activity):
        if not isinstance(container, dict):
            continue
        for key in (
            "parent",
            "parentComment",
            "parentCommentUrn",
            "parentCommentURN",
            "parentCommentId",
            "parentCommentID",
        ):
            if key in container:
                parent_urn = _maybe_build_parent_comment_urn(post_urn, container[key])
                if parent_urn:
                    return parent_urn
    return None


def _reaction_to_record(
    element: dict, activity: dict, owner: str = ""
) -> Optional[ActivityRecord]:
    post_urn = _extract_post_urn_for_reaction(element, activity)
    actor = extract_actor(element, activity)
    if not post_urn or not actor or _is_delete_action(element):
        return None

    reaction_type = activity.get("reactionType", "UNKNOWN")
    timestamp = _get_activity_timestamp(element, activity)

    if post_urn.startswith("urn:li:comment:"):
        activity_type = ActivityType.REACTION_TO_COMMENT.value
        parsed = parse_comment_urn(post_urn)
        post_id = (parsed.get("parent_id") or "") if parsed else ""
    else:
        activity_type = ActivityType.REACTION_TO_POST.value
        post_id = extract_urn_id(post_urn) or ""

    time_str = str(timestamp or "")
    return ActivityRecord(
        owner=owner,
        activity_type=activity_type,
        time=time_str,
        reaction_type=reaction_type,
        author_urn=actor,
        activity_urn=post_urn,
        post_id=post_id,
        post_url=urn_to_post_url(post_urn) or "",
        content="",
        parent_urn="",
        original_post_urn="",
        activity_id=make_activity_id(post_id, activity_type, time_str, post_urn),
        created_at=format_timestamp(timestamp) or "",
    )


def _post_to_record(
    element: dict, activity: dict, owner: str = ""
) -> Optional[ActivityRecord]:
    post_urn = activity.get("id", "")
    if not post_urn or not (
        post_urn.startswith("urn:li:share:") or post_urn.startswith("urn:li:ugcPost:")
    ):
        return None

    timestamp = _get_activity_timestamp(element, activity)
    actor = extract_actor(element, activity)
    is_repost = activity.get("ugcOrigin") == "RESHARE" or bool(
        activity.get("responseContext", {}).get("parent")
    )

    if is_repost:
        author = actor
    else:
        author = (
            activity.get("author")
            or activity.get("firstPublishedActor", {}).get("member", "")
            or actor
        )

    share_content = activity.get("specificContent", {}).get(
        "com.linkedin.ugc.ShareContent", {}
    )
    content = share_content.get("shareCommentary", {}).get("text", "")
    original_post_urn = ""

    if is_repost:
        original_post_urn = activity.get("responseContext", {}).get(
            "parent"
        ) or activity.get("responseContext", {}).get("root", "")
        activity_type = ActivityType.REPOST.value
        post_id = extract_urn_id(original_post_urn) or ""
    else:
        activity_type = ActivityType.POST.value
        post_id = extract_urn_id(post_urn) or ""

    time_str = str(timestamp or "")
    return ActivityRecord(
        owner=owner,
        activity_type=activity_type,
        time=time_str,
        reaction_type="",
        author_urn=author,
        activity_urn=post_urn,
        post_id=post_id,
        post_url=urn_to_post_url(post_urn) or "",
        content=content,
        parent_urn=original_post_urn,
        original_post_urn=original_post_urn,
        activity_id=make_activity_id(post_id, activity_type, time_str, post_urn),
        created_at=format_timestamp(timestamp) or "",
    )


def _comment_to_record(
    element: dict, activity: dict, owner: str = ""
) -> Optional[ActivityRecord]:
    comment_id = activity.get("id", "")
    post_urn = activity.get("object", "")
    actor = extract_actor(element, activity)
    if not comment_id or not post_urn or not actor:
        return None

    timestamp = _get_activity_timestamp(element, activity)
    comment_text = activity.get("message", {}).get("text", "")
    comment_urn = build_comment_urn(post_urn, comment_id)
    if not comment_urn:
        return None

    response_context = activity.get("responseContext", {})
    parent_comment_urn = _extract_parent_comment_urn(
        activity, response_context, post_urn
    )

    post_id = extract_urn_id(post_urn) or ""
    time_str = str(timestamp or "")
    return ActivityRecord(
        owner=owner,
        activity_type=ActivityType.COMMENT.value,
        time=time_str,
        reaction_type="",
        author_urn=actor,
        activity_urn=comment_urn,
        post_id=post_id,
        post_url=urn_to_post_url(post_urn) or "",
        content=comment_text,
        parent_urn=parent_comment_urn or post_urn,
        original_post_urn="",
        activity_id=make_activity_id(
            post_id, ActivityType.COMMENT.value, time_str, comment_urn
        ),
        created_at=format_timestamp(timestamp) or "",
    )


def _instant_repost_to_record(
    element: dict, activity: dict, owner: str = ""
) -> Optional[ActivityRecord]:
    reposted_share = activity.get("repostedContent", {}).get("share", "")
    actor = extract_actor(element, activity)
    if not reposted_share or not actor:
        return None

    timestamp = _get_activity_timestamp(element, activity)
    post_id = extract_urn_id(reposted_share) or ""
    time_str = str(timestamp or "")

    return ActivityRecord(
        owner=owner,
        activity_type=ActivityType.INSTANT_REPOST.value,
        time=time_str,
        reaction_type="",
        author_urn=actor,
        activity_urn=reposted_share,
        post_id=post_id,
        post_url=urn_to_post_url(reposted_share) or "",
        content="",
        parent_urn=reposted_share,
        original_post_urn=reposted_share,
        activity_id=make_activity_id(
            post_id, ActivityType.INSTANT_REPOST.value, time_str, reposted_share
        ),
        created_at=format_timestamp(timestamp) or "",
    )


def extract_activity_records(elements: list, owner: str = "") -> List[ActivityRecord]:
    """Extract ActivityRecords from changelog elements."""
    records: List[ActivityRecord] = []
    for element in elements:
        resource_name = element.get("resourceName", "")
        activity = element.get("activity", {})
        record: Optional[ActivityRecord] = None

        if RESOURCE_REACTIONS in resource_name:
            record = _reaction_to_record(element, activity, owner)
        elif (
            RESOURCE_POST in resource_name.lower() or RESOURCE_POSTS in resource_name
        ) and _is_comment_like_activity(activity):
            record = _comment_to_record(element, activity, owner)
        elif RESOURCE_POST in resource_name.lower() or RESOURCE_POSTS in resource_name:
            record = _post_to_record(element, activity, owner)
        elif RESOURCE_COMMENTS in resource_name:
            record = _comment_to_record(element, activity, owner)
        elif RESOURCE_INSTANT_REPOSTS in resource_name:
            record = _instant_repost_to_record(element, activity, owner)

        if record is not None:
            records.append(record)

    return records


def get_all_post_activities(
    start_time: int | None = None, verbose: bool = True
) -> list:
    """Fetch all changelog data related to public posts."""
    return fetch_changelog_data(
        resource_filter=POST_RELATED_RESOURCES,
        start_time=start_time,
        verbose=verbose,
    )

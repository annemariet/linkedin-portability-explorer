"""Resolve canonical post identity from CSV activity rows for content-store keys."""

from __future__ import annotations

import hashlib
import re

from linkedin_api.activity_csv import ActivityRecord, ActivityType
from linkedin_api.utils.urls import is_comment_feed_url
from linkedin_api.utils.urns import (
    comment_urn_to_post_url,
    extract_parent_post_urn_from_comment,
    extract_urn_id,
    parse_comment_urn,
    urn_to_post_url,
)

_POST_URN_PREFIXES = ("urn:li:ugcPost:", "urn:li:share:", "urn:li:activity:")
_POST_ID_RE = re.compile(r"^\d+$")


def resolve_post_id(rec: ActivityRecord) -> str:
    """Return the LinkedIn post snowflake id for *rec*, deriving from URNs when needed."""
    pid = (rec.post_id or "").strip()
    if pid:
        return pid

    if rec.activity_urn.startswith("urn:li:comment:"):
        parsed = parse_comment_urn(rec.activity_urn)
        if parsed and parsed.get("parent_id"):
            return str(parsed["parent_id"])

    if rec.original_post_urn:
        eid = extract_urn_id(rec.original_post_urn) or ""
        if _POST_ID_RE.match(eid):
            return eid

    for urn in (rec.parent_urn, rec.activity_urn):
        if not urn:
            continue
        if urn.startswith("urn:li:comment:"):
            parent_post = extract_parent_post_urn_from_comment(urn)
            if parent_post:
                eid = extract_urn_id(parent_post) or ""
                if _POST_ID_RE.match(eid):
                    return eid
        elif urn.startswith(_POST_URN_PREFIXES):
            eid = extract_urn_id(urn) or ""
            if _POST_ID_RE.match(eid):
                return eid
    return ""


def resolve_post_urn(rec: ActivityRecord) -> str:
    """Return the parent **post** URN (never a comment URN)."""
    if rec.activity_type in (
        ActivityType.REPOST.value,
        ActivityType.INSTANT_REPOST.value,
    ):
        orig = (rec.original_post_urn or rec.parent_urn or "").strip()
        if orig and not orig.startswith("urn:li:comment:"):
            return orig

    if rec.activity_urn.startswith("urn:li:comment:"):
        parent_post = extract_parent_post_urn_from_comment(rec.activity_urn)
        if parent_post:
            return parent_post

    if rec.activity_type == ActivityType.COMMENT.value:
        parent = (rec.parent_urn or "").strip()
        if parent.startswith("urn:li:comment:"):
            parent_post = extract_parent_post_urn_from_comment(parent)
            if parent_post:
                return parent_post
        if parent.startswith(_POST_URN_PREFIXES):
            return parent

    if rec.activity_urn.startswith(_POST_URN_PREFIXES):
        return rec.activity_urn

    pid = resolve_post_id(rec)
    if pid:
        return f"urn:li:ugcPost:{pid}"
    return ""


def resolve_post_url(rec: ActivityRecord) -> str:
    """Return the feed URL of the original post (not a comment thread URL)."""
    post_urn = resolve_post_urn(rec)
    if post_urn:
        url = urn_to_post_url(post_urn)
        if url:
            return url
    raw = (rec.post_url or "").strip()
    if raw and not is_comment_feed_url(raw):
        return raw
    if rec.activity_urn.startswith("urn:li:comment:"):
        return comment_urn_to_post_url(rec.activity_urn) or raw
    return raw


def content_stem(post_id: str, *, fallback_urn: str = "") -> str:
    """Filesystem stem for content-store files (``{stem}.md``, etc.)."""
    pid = (post_id or "").strip()
    if _POST_ID_RE.match(pid):
        return pid
    urn = (fallback_urn or "").strip()
    if urn:
        return hashlib.sha256(urn.encode()).hexdigest()
    return ""


def storage_key(post_id: str = "", *, post_urn: str = "") -> tuple[str, str]:
    """
    Return ``(stem, post_urn)`` for content-store I/O.

    When *post_id* is missing, derive it from *post_urn* when possible.
    """
    pid = (post_id or "").strip()
    pu = (post_urn or "").strip()
    if not pid and pu:
        if pu.startswith("urn:li:comment:"):
            parent_post = extract_parent_post_urn_from_comment(pu)
            if parent_post:
                pu = parent_post
        eid = extract_urn_id(pu) or ""
        if _POST_ID_RE.match(eid):
            pid = eid
    if not pu and pid:
        pu = f"urn:li:ugcPost:{pid}"
    stem = content_stem(pid, fallback_urn=pu)
    return stem, pu

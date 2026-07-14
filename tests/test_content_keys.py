"""Tests for post_id-based content store keys."""

import hashlib

from linkedin_api.activity_csv import (
    ActivityRecord,
    ActivityType,
    make_activity_id,
)
from linkedin_api.content_keys import (
    content_stem,
    resolve_post_id,
    resolve_post_url,
    resolve_post_urn,
    storage_key,
)
from linkedin_api.enriched_record import EnrichedRecord


POST_ID = "7482038400523575296"
POST_URN = f"urn:li:ugcPost:{POST_ID}"
POST_URL = f"https://www.linkedin.com/feed/update/{POST_URN}"
COMMENT_URN = f"urn:li:comment:(ugcPost:{POST_ID},7482058165480562688)"
REPLY_PARENT = f"urn:li:comment:(urn:li:ugcPost:{POST_ID},7482058165480562688)"
REPLY_URN = f"urn:li:comment:(ugcPost:{POST_ID},7482177894232956928)"


def _comment_row(
    *,
    activity_urn: str,
    parent_urn: str,
    activity_type: str = ActivityType.COMMENT.value,
    reaction_type: str = "",
    post_url: str = POST_URL,
) -> ActivityRecord:
    time_str = "1783890222128"
    return ActivityRecord(
        owner="urn:li:person:me",
        activity_type=activity_type,
        time=time_str,
        reaction_type=reaction_type,
        author_urn="urn:li:person:me",
        activity_urn=activity_urn,
        post_id=POST_ID,
        post_url=post_url,
        content="Hello",
        parent_urn=parent_urn,
        original_post_urn="",
        activity_id=make_activity_id(POST_ID, activity_type, time_str, activity_urn),
        created_at="2026-07-12T23:03:42.128000",
    )


class TestContentKeysSamePost:
    def test_comment_on_post(self):
        rec = _comment_row(activity_urn=COMMENT_URN, parent_urn=POST_URN)
        assert resolve_post_id(rec) == POST_ID
        assert resolve_post_urn(rec) == POST_URN
        assert resolve_post_url(rec) == POST_URL

    def test_reaction_to_comment(self):
        rec = _comment_row(
            activity_urn=COMMENT_URN,
            parent_urn="",
            activity_type=ActivityType.REACTION_TO_COMMENT.value,
            reaction_type="EMPATHY",
            post_url=(
                "https://www.linkedin.com/feed/update/"
                f"urn:li:comment:(ugcPost:{POST_ID},7482058165480562688)"
            ),
        )
        assert resolve_post_id(rec) == POST_ID
        assert resolve_post_urn(rec) == POST_URN
        assert "ugcPost" in resolve_post_url(rec)
        assert "comment:" not in resolve_post_url(rec)

    def test_reply_with_nested_parent_comment_urn(self):
        rec = _comment_row(activity_urn=REPLY_URN, parent_urn=REPLY_PARENT)
        assert resolve_post_id(rec) == POST_ID
        assert resolve_post_urn(rec) == POST_URN

    def test_all_three_rows_share_content_stem(self):
        rows = [
            _comment_row(activity_urn=COMMENT_URN, parent_urn=POST_URN),
            _comment_row(
                activity_urn=COMMENT_URN,
                parent_urn="",
                activity_type=ActivityType.REACTION_TO_COMMENT.value,
                reaction_type="EMPATHY",
            ),
            _comment_row(activity_urn=REPLY_URN, parent_urn=REPLY_PARENT),
        ]
        stems = {
            content_stem(EnrichedRecord.from_activity_record(r).post_id) for r in rows
        }
        assert stems == {POST_ID}

    def test_legacy_nested_parent_does_not_match_old_hash(self):
        """Old bug: nested parent comment URN produced a different hash."""
        old_nested = hashlib.sha256(REPLY_PARENT.encode()).hexdigest()
        assert content_stem(POST_ID) != old_nested


class TestStorageKey:
    def test_post_id_stem_is_numeric(self):
        stem, urn = storage_key(POST_ID, post_urn=POST_URN)
        assert stem == POST_ID
        assert urn == POST_URN

    def test_fallback_urn_hash_when_no_post_id(self):
        legacy_urn = "urn:li:person:unknown"
        stem, _ = storage_key("", post_urn=legacy_urn)
        assert stem == hashlib.sha256(legacy_urn.encode()).hexdigest()

"""Tests for comment vs post routing (_is_comment_like_activity and extract_entities_and_relationships)."""

from linkedin_api.activity_extract import (
    _is_comment_like_activity,
    extract_activity_records,
)


def test_is_comment_like_activity_comment_shape_returns_true():
    activity = {
        "id": "7410301301244284929",
        "object": "urn:li:ugcPost:7409540812340097024",
        "message": {"text": "A comment"},
    }
    assert _is_comment_like_activity(activity) is True


def test_is_comment_like_activity_post_share_returns_false():
    activity = {
        "id": "urn:li:share:123",
        "specificContent": {"com.linkedin.ugc.ShareContent": {"shareCommentary": {}}},
    }
    assert _is_comment_like_activity(activity) is False


def test_is_comment_like_activity_no_message_returns_false():
    activity = {"object": "urn:li:ugcPost:123"}
    assert _is_comment_like_activity(activity) is False


def test_comment_like_under_post_resource_routes_to_comment():
    """When resourceName is ugcPosts but activity is comment-like, type is comment."""
    element = {
        "resourceName": "ugcPosts",
        "actor": "urn:li:person:abc",
        "activity": {
            "id": "7410301301244284929",
            "object": "urn:li:ugcPost:7409540812340097024",
            "message": {"text": "A comment"},
            "created": {"time": 1766750428159},
        },
    }
    records = extract_activity_records([element])
    assert len(records) == 1
    assert records[0].activity_type == "comment"

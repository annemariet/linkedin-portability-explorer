"""
Utility modules for LinkedIn data collection.

This package contains reusable utilities organized by concern:
- auth: Authentication and session management
- changelog: LinkedIn API changelog fetching
- urns: URN to URL conversion utilities
- summaries: Data summarization and statistics
- activities: Activity element analysis
"""

from linkedin_api.utils.auth import get_access_token, build_linkedin_session
from linkedin_api.utils.changelog import (
    BASE_URL,
    fetch_changelog_data,
    get_last_processed_timestamp,
    get_max_processed_at,
    save_last_processed_timestamp,
)
from linkedin_api.utils.urns import (
    extract_urn_id,
    urn_to_post_url,
)
from linkedin_api.utils.summaries import summarize_resources, print_resource_summary
from linkedin_api.utils.activities import (
    extract_element_fields,
    determine_post_type,
    extract_reaction_type,
    extract_timestamp,
    is_reaction_element,
    is_post_element,
    is_comment_element,
    is_message_element,
    is_invitation_element,
)

__all__ = [
    # Auth
    "get_access_token",
    "build_linkedin_session",
    # Changelog
    "BASE_URL",
    "fetch_changelog_data",
    "get_last_processed_timestamp",
    "save_last_processed_timestamp",
    "get_max_processed_at",
    # URNs
    "extract_urn_id",
    "urn_to_post_url",
    # Summaries
    "summarize_resources",
    "print_resource_summary",
    # Activities
    "extract_element_fields",
    "determine_post_type",
    "extract_reaction_type",
    "extract_timestamp",
    "is_reaction_element",
    "is_post_element",
    "is_comment_element",
    "is_message_element",
    "is_invitation_element",
]

"""
Utilities for fetching LinkedIn changelog data with pagination support.

This module provides shared functions for fetching changelog data from the
LinkedIn Member Data Portability API, handling pagination, filtering, and errors.
"""

import logging
from time import time
from typing import Callable, List, Optional

from linkedin_api.activity_csv import get_data_dir
from linkedin_api.utils.auth import build_linkedin_session, get_access_token

logger = logging.getLogger(__name__)


BASE_URL = "https://api.linkedin.com/rest"
API_MAX_BATCH_SIZE = (
    50  # LinkedIn API max; smaller values cause unnecessary calls and rate limiting
)
# LinkedIn retains changelog data for ~28 days; default fetch window matches that.
_DEFAULT_LOOKBACK_MS = 28 * 24 * 60 * 60 * 1000


def _default_start_time() -> int:
    return int(time() * 1000) - _DEFAULT_LOOKBACK_MS


def _last_run_file():
    return get_data_dir() / ".last_run"


class TokenExpiredError(Exception):
    """Raised when the LinkedIn API returns 401 EXPIRED_ACCESS_TOKEN."""

    pass


def get_last_processed_timestamp() -> Optional[int]:
    """
    Read the last processed timestamp from .last_run file.

    Returns:
        Timestamp in epoch milliseconds, or None if file doesn't exist or is invalid.
    """
    last_run_file = _last_run_file()
    if not last_run_file.exists():
        return None

    try:
        content = last_run_file.read_text().strip()
        timestamp = int(content)
        min_valid = _default_start_time()
        max_valid = int(time() * 1000) + (30 * 24 * 60 * 60 * 1000)

        if timestamp < min_valid or timestamp > max_valid:
            return None

        return timestamp
    except (ValueError, OSError):
        return None


def save_last_processed_timestamp(timestamp: int) -> None:
    """
    Save the last processed timestamp to .last_run file.

    Args:
        timestamp: Timestamp in epoch milliseconds.
    """
    try:
        _last_run_file().write_text(str(timestamp))
    except OSError:
        pass  # Silently fail if we can't write


def get_max_processed_at(elements: List[dict]) -> Optional[int]:
    """
    Extract the maximum processedAt timestamp from changelog elements.

    Args:
        elements: List of changelog element dictionaries.

    Returns:
        Maximum processedAt timestamp in epoch milliseconds, or None if no valid timestamps found.
    """
    timestamps = [
        val for elem in elements if isinstance((val := elem.get("processedAt")), int)
    ]
    return max(timestamps) if timestamps else None


def fetch_changelog_data(
    resource_filter: Optional[List[str]] = None,
    filter_func: Optional[Callable[[dict], bool]] = None,
    start_time: Optional[int] = None,
    verbose: bool = True,
) -> List[dict]:
    """
    Fetch all changelog data by paginating through all results.

    Args:
        resource_filter: Optional list of resource names to filter by.
                        Elements are included if any filter string is in resourceName.
        filter_func: Optional custom filter function that takes an element dict
                    and returns True to include it.
        start_time: Optional start time in epoch milliseconds. Returns events
                   created after this time. LinkedIn keeps data for 28 days.
                   If None, automatically loads from .last_run file, or falls back
                   to DEFAULT_START_TIME if .last_run doesn't exist.
        verbose: If True, print progress messages (default: True)

    Returns:
        List of changelog elements. Empty list if token is missing or on error.
    """
    access_token = get_access_token()
    if not access_token:
        if verbose:
            print("❌ LINKEDIN_ACCESS_TOKEN not found")
            print(
                "   Run 'uv run python scripts/setup_token.py' to store it in Keychain,"
                " or set it as an environment variable"
            )
        return []

    session = build_linkedin_session(access_token)

    # Auto-load saved timestamp if start_time not explicitly provided
    if start_time is None:
        start_time = get_last_processed_timestamp() or _default_start_time()

    if verbose:
        print("🔍 Fetching all changelog data...")
        if start_time:
            from datetime import datetime

            start_date = datetime.fromtimestamp(start_time / 1000)
            print(
                f"   📅 Fetching events from: {start_date.strftime('%Y-%m-%d %H:%M:%S')}"
            )

    all_elements = []
    start = 0

    while True:
        try:
            if verbose:
                print(f"   📡 Fetching batch starting at {start}...")
            else:
                logger.info("Fetching changelog batch start=%s...", start)

            params = {
                "q": "memberAndApplication",
                "start": start,
                "count": API_MAX_BATCH_SIZE,
            }

            if start_time:
                params["startTime"] = start_time

            response = session.get(
                f"{BASE_URL}/memberChangeLogs",
                params=params,
            )

            if response.status_code != 200:
                if verbose:
                    print(f"❌ Error: {response.status_code}")
                    print(f"Response: {response.text[:200]}...")
                if (
                    response.status_code == 401
                    and "EXPIRED_ACCESS_TOKEN" in response.text
                ):
                    raise TokenExpiredError("LinkedIn access token has expired")
                break

            data = response.json()
            elements = data.get("elements", [])

            if not elements:
                if verbose:
                    print("✅ No more data to fetch")
                break

            # Apply filters if provided
            if resource_filter:
                elements = [
                    e
                    for e in elements
                    if any(
                        resource.lower() in e.get("resourceName", "").lower()
                        for resource in resource_filter
                    )
                ]

            if filter_func:
                elements = [e for e in elements if filter_func(e)]

            all_elements.extend(elements)

            if verbose:
                total_filtered = len(all_elements)
                print(f"   ✅ Got {len(elements)} elements (total: {total_filtered})")
            else:
                logger.info(
                    "Changelog batch: got %s elements (total: %s)",
                    len(elements),
                    len(all_elements),
                )

            # Check for more pages
            paging = data.get("paging", {})
            links = paging.get("links", [])
            next_link = None

            for link in links:
                if link.get("rel") == "next":
                    next_link = link.get("href")
                    break

            if not next_link:
                break

            start += API_MAX_BATCH_SIZE

        except TokenExpiredError:
            raise
        except Exception as e:
            if verbose:
                print(f"❌ Exception: {str(e)}")
            break

    if verbose:
        print(f"✅ Total elements fetched: {len(all_elements)}")

    return all_elements

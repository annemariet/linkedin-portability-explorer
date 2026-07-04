"""Tests for changelog_utils module."""

from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from unittest.mock import MagicMock, patch

from linkedin_api.utils.changelog import (
    BASE_URL,
    fetch_changelog_data,
    get_last_processed_timestamp,
    get_max_processed_at,
    save_last_processed_timestamp,
)


class TestFetchChangelogData:
    """Test fetch_changelog_data function."""

    @patch("linkedin_api.utils.changelog.get_access_token")
    @patch("linkedin_api.utils.changelog.build_linkedin_session")
    def test_fetch_all_data_with_pagination(self, mock_build_session, mock_get_token):
        """Test fetching all changelog data with pagination."""
        mock_get_token.return_value = "test_token"
        mock_session = MagicMock()
        mock_build_session.return_value = mock_session

        # Mock first page
        mock_response_1 = MagicMock()
        mock_response_1.status_code = 200
        mock_response_1.json.return_value = {
            "elements": [{"id": 1}, {"id": 2}],
            "paging": {"links": [{"rel": "next", "href": "next_page"}]},
        }

        # Mock second page
        mock_response_2 = MagicMock()
        mock_response_2.status_code = 200
        mock_response_2.json.return_value = {
            "elements": [{"id": 3}],
            "paging": {"links": []},
        }

        mock_session.get.side_effect = [mock_response_1, mock_response_2]

        result = fetch_changelog_data()

        assert len(result) == 3
        assert result[0]["id"] == 1
        assert result[1]["id"] == 2
        assert result[2]["id"] == 3
        assert mock_session.get.call_count == 2

    @patch("linkedin_api.utils.changelog.get_access_token")
    @patch("linkedin_api.utils.changelog.build_linkedin_session")
    def test_fetch_with_resource_filter(self, mock_build_session, mock_get_token):
        """Test fetching with resource name filtering."""
        mock_get_token.return_value = "test_token"
        mock_session = MagicMock()
        mock_build_session.return_value = mock_session

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "elements": [
                {"resourceName": "ugcPosts", "id": 1},
                {"resourceName": "messages", "id": 2},
                {"resourceName": "ugcPosts", "id": 3},
            ],
            "paging": {"links": []},
        }

        mock_session.get.return_value = mock_response

        result = fetch_changelog_data(resource_filter=["ugcPosts"])

        assert len(result) == 2
        assert all(e["resourceName"] == "ugcPosts" for e in result)

    @patch("linkedin_api.utils.changelog.get_access_token")
    def test_fetch_no_token(self, mock_get_token):
        """Test that missing token returns empty list."""
        mock_get_token.return_value = None

        result = fetch_changelog_data()

        assert result == []

    @patch("linkedin_api.utils.changelog.get_access_token")
    @patch("linkedin_api.utils.changelog.build_linkedin_session")
    def test_fetch_handles_api_error(self, mock_build_session, mock_get_token):
        """Test handling of API errors."""
        mock_get_token.return_value = "test_token"
        mock_session = MagicMock()
        mock_build_session.return_value = mock_session

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"
        mock_session.get.return_value = mock_response

        result = fetch_changelog_data()

        assert result == []

    @patch("linkedin_api.utils.changelog.get_access_token")
    @patch("linkedin_api.utils.changelog.build_linkedin_session")
    def test_fetch_handles_exception(self, mock_build_session, mock_get_token):
        """Test handling of exceptions during fetch."""
        mock_get_token.return_value = "test_token"
        mock_session = MagicMock()
        mock_build_session.return_value = mock_session
        mock_session.get.side_effect = Exception("Network error")

        result = fetch_changelog_data()

        assert result == []


class TestBaseUrl:
    """Test BASE_URL constant."""

    def test_base_url_constant(self):
        """Test that BASE_URL is correctly defined."""
        assert BASE_URL == "https://api.linkedin.com/rest"


class TestTimestampPersistence:
    """Test timestamp persistence functions."""

    def test_get_max_processed_at_with_valid_timestamps(self):
        """Test extracting max processedAt from elements."""
        elements = [
            {"processedAt": 1000, "id": 1},
            {"processedAt": 3000, "id": 2},
            {"processedAt": 2000, "id": 3},
        ]
        assert get_max_processed_at(elements) == 3000

    def test_get_max_processed_at_with_missing_fields(self):
        """Test handling missing processedAt fields gracefully."""
        elements = [
            {"id": 1},
            {"processedAt": 2000, "id": 2},
            {"processedAt": "invalid", "id": 3},
        ]
        assert get_max_processed_at(elements) == 2000

    def test_get_max_processed_at_empty_list(self):
        """Test empty elements list returns None."""
        assert get_max_processed_at([]) is None

    def test_invalid_timestamp_falls_back_to_default(self):
        """Test that invalid timestamp file falls back to default (don't lose data)."""
        with TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / ".last_run"
            with patch(
                "linkedin_api.utils.changelog._last_run_file", return_value=test_file
            ):
                # Write invalid timestamp (too old)
                test_file.write_text("1000000000000")
                assert get_last_processed_timestamp() is None

                # Write invalid timestamp (corrupted)
                test_file.write_text("not-a-number")
                assert get_last_processed_timestamp() is None

                # Write invalid timestamp (too far in future)
                # Use now + 31 days (validation allows up to now + 30 days)
                future_timestamp = int(time() * 1000) + (31 * 24 * 60 * 60 * 1000)
                test_file.write_text(str(future_timestamp))
                assert get_last_processed_timestamp() is None

    def test_save_and_load_timestamp(self):
        """Test saving and loading valid timestamp."""
        with TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / ".last_run"
            with patch(
                "linkedin_api.utils.changelog._last_run_file", return_value=test_file
            ):
                timestamp = int(time() * 1000) - (2 * 24 * 60 * 60 * 1000)
                save_last_processed_timestamp(timestamp)
                assert get_last_processed_timestamp() == timestamp

    @patch("linkedin_api.utils.changelog.get_access_token")
    @patch("linkedin_api.utils.changelog.get_last_processed_timestamp")
    def test_fetch_auto_loads_saved_timestamp(self, mock_get_timestamp, mock_get_token):
        """Test that fetch_changelog_data auto-loads saved timestamp when start_time is None."""
        mock_get_token.return_value = "test_token"
        saved_timestamp = 1765906726844
        mock_get_timestamp.return_value = saved_timestamp

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"elements": [], "paging": {"links": []}}

        with patch(
            "linkedin_api.utils.changelog.build_linkedin_session",
            return_value=mock_session,
        ):
            mock_session.get.return_value = mock_response
            fetch_changelog_data(start_time=None)

            # Verify startTime parameter was used
            call_args = mock_session.get.call_args
            assert "startTime" in call_args.kwargs["params"]
            assert call_args.kwargs["params"]["startTime"] == saved_timestamp
